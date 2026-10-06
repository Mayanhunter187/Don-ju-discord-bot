"""Where the queue is saved between restarts.

In the cluster it goes in a ConfigMap, so whichever replica takes over can
resume it. Run locally, it goes in songs/state.json.
"""
import asyncio
import json
import os

import aiohttp

STATE_FILE = 'songs/state.json'
# Writes to the ConfigMap are at most this often; save() can be called far more
WRITE_INTERVAL = 2


class StateStore:
    def __init__(self, kube, configmap):
        self.kube = kube
        self.configmap = configmap
        self.latest = None
        self.dirty = asyncio.Event()
        if kube:
            self.collection = f'/api/v1/namespaces/{kube.namespace}/configmaps'
            self.path = f'{self.collection}/{configmap}'

    def save(self, state):
        """Record the latest state. In the cluster it is written in the background."""
        self.latest = state
        if self.kube is None:
            self._write_file(state)
        else:
            self.dirty.set()

    async def run(self):
        """Background writer for the ConfigMap."""
        while True:
            await self.dirty.wait()
            self.dirty.clear()
            await self._write_configmap(self.latest)
            await asyncio.sleep(WRITE_INTERVAL)

    async def flush(self):
        """Write the latest state now (used on shutdown)."""
        if self.kube is not None and self.latest is not None:
            await self._write_configmap(self.latest)

    async def load(self):
        if self.kube is None:
            if not os.path.exists(STATE_FILE):
                return {}
            with open(STATE_FILE) as f:
                return json.load(f)
        status, cm = await self.kube.request('GET', self.path)
        if status == 404:
            return {}
        if status != 200:
            raise RuntimeError(f"reading ConfigMap {self.configmap}: HTTP {status}")
        return json.loads((cm.get('data') or {}).get('state.json') or '{}')

    def _write_file(self, state):
        try:
            # Write then rename, so a crash mid-write can't leave a corrupt file
            with open(STATE_FILE + '.tmp', 'w') as f:
                json.dump(state, f)
            os.replace(STATE_FILE + '.tmp', STATE_FILE)
        except Exception as e:
            print(f"Error saving state: {e}", flush=True)

    async def _write_configmap(self, state):
        data = {'state.json': json.dumps(state)}
        try:
            status, _ = await self.kube.request('PATCH', self.path, {'data': data},
                                                content_type='application/merge-patch+json')
            if status == 404:
                status, _ = await self.kube.request('POST', self.collection, {
                    'apiVersion': 'v1',
                    'kind': 'ConfigMap',
                    'metadata': {'name': self.configmap},
                    'data': data,
                })
            if status not in (200, 201):
                print(f"Error saving state: HTTP {status}", flush=True)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            print(f"Error saving state: {e!r}", flush=True)
