"""Minimal Kubernetes API client, used when the bot runs inside the cluster."""
import json
import os
import ssl

import aiohttp

SA_DIR = '/var/run/secrets/kubernetes.io/serviceaccount'


class KubeClient:
    def __init__(self):
        host = os.environ['KUBERNETES_SERVICE_HOST']
        port = os.environ['KUBERNETES_SERVICE_PORT']
        self.base = f'https://{host}:{port}'
        with open(f'{SA_DIR}/namespace') as f:
            self.namespace = f.read().strip()
        self.ssl = ssl.create_default_context(cafile=f'{SA_DIR}/ca.crt')
        self.session = None

    @classmethod
    def from_cluster(cls):
        """Return a client when running in a pod, otherwise None."""
        if 'KUBERNETES_SERVICE_HOST' in os.environ and os.path.exists(f'{SA_DIR}/token'):
            return cls()
        return None

    async def request(self, method, path, body=None, content_type='application/json'):
        """Return (status, parsed JSON body). Raises on network errors and timeouts."""
        if self.session is None:
            # etcd on the cluster's disks can stall for several seconds
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        # Read the token every time; the kubelet rotates it
        with open(f'{SA_DIR}/token') as f:
            token = f.read().strip()
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': content_type}
        data = json.dumps(body) if body is not None else None
        async with self.session.request(method, self.base + path, data=data,
                                        headers=headers, ssl=self.ssl) as resp:
            text = await resp.text()
            return resp.status, (json.loads(text) if text else None)

    async def close(self):
        if self.session:
            await self.session.close()
