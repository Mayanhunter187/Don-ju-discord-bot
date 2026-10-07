"""Leader election on a Kubernetes Lease, so only one replica talks to Discord.

Works like client-go's leader election: a standby takes the lease once the
holder has gone LEASE_DURATION without renewing it, timed on the standby's own
clock. A leader that shuts down cleanly releases the lease so a standby can
take over straight away.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

import aiohttp

import metrics

log = logging.getLogger(__name__)

# A standby takes over this long after the leader stops renewing. Kept well
# above the multi-second etcd stalls seen on the cluster's disks.
LEASE_DURATION = 30
# The leader steps down if it can't renew for this long, before anyone else
# could have taken the lease
RENEW_DEADLINE = 20
RETRY_PERIOD = 2


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


class LeaderElector:
    def __init__(self, kube, name, identity, clock=time.monotonic):
        self.kube = kube
        self.clock = clock
        self.name = name
        self.identity = identity
        self.collection = f'/apis/coordination.k8s.io/v1/namespaces/{kube.namespace}/leases'
        self.path = f'{self.collection}/{name}'
        self.holder = None
        self.observed = None  # (resourceVersion, monotonic time it was first seen)

    async def try_acquire(self):
        """Take or renew the lease. Returns True if we hold it afterwards."""
        try:
            status, lease = await self.kube.request('GET', self.path)
            if status == 404:
                now = _now()
                status, _ = await self.kube.request('POST', self.collection, {
                    'apiVersion': 'coordination.k8s.io/v1',
                    'kind': 'Lease',
                    'metadata': {'name': self.name},
                    'spec': {
                        'holderIdentity': self.identity,
                        'leaseDurationSeconds': LEASE_DURATION,
                        'acquireTime': now,
                        'renewTime': now,
                        'leaseTransitions': 0,
                    },
                })
                self.holder = self.identity if status == 201 else None
                return status == 201
            if status != 200:
                log.warning(f"Lease read failed: HTTP {status}")
                return False

            spec = lease.get('spec') or {}
            self.holder = spec.get('holderIdentity') or ''
            version = lease['metadata']['resourceVersion']
            if self.observed is None or self.observed[0] != version:
                self.observed = (version, self.clock())

            if self.holder and self.holder != self.identity:
                duration = spec.get('leaseDurationSeconds') or LEASE_DURATION
                if self.clock() - self.observed[1] < duration:
                    return False
                log.warning(f"Lease held by {self.holder} has expired")
            now = _now()
            if self.holder != self.identity:
                spec['acquireTime'] = now
                spec['leaseTransitions'] = (spec.get('leaseTransitions') or 0) + 1
            spec.update(holderIdentity=self.identity, leaseDurationSeconds=LEASE_DURATION, renewTime=now)
            lease['spec'] = spec
            # The resourceVersion in metadata makes this fail with 409 if anyone
            # else changed the lease since we read it
            status, updated = await self.kube.request('PUT', self.path, lease)
            if status == 200:
                self.holder = self.identity
                self.observed = (updated['metadata']['resourceVersion'], self.clock())
                return True
            return False
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            metrics.LEASE_ERRORS.inc()
            log.warning(f"Lease request failed: {e!r}")
            return False

    async def acquire(self):
        """Wait until we are the leader."""
        while not await self.try_acquire():
            await asyncio.sleep(RETRY_PERIOD)

    async def hold(self):
        """Keep renewing the lease. Returns once leadership is lost."""
        last_renewed = self.clock()
        while True:
            await asyncio.sleep(RETRY_PERIOD)
            if await self.try_acquire():
                last_renewed = self.clock()
            elif self.holder and self.holder != self.identity:
                log.warning(f"Lost the lease to {self.holder}")
                return
            elif self.clock() - last_renewed > RENEW_DEADLINE:
                log.warning("Could not renew the lease in time")
                return

    async def release(self):
        """Give up the lease so a standby can take over immediately."""
        try:
            status, lease = await self.kube.request('GET', self.path)
            if status != 200 or (lease.get('spec') or {}).get('holderIdentity') != self.identity:
                return
            lease['spec'].update(holderIdentity='', leaseDurationSeconds=1, renewTime=_now())
            await self.kube.request('PUT', self.path, lease)
            log.info("Released the lease")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            log.warning(f"Lease release failed: {e!r}")