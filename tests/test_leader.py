import leader
from leader import LeaderElector
from tests.fakes import FakeKube


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def test_first_candidate_takes_the_lease():
    kube, clock = FakeKube(), Clock()
    a = LeaderElector(kube, 'lock', 'a', clock)
    assert await a.try_acquire()
    assert await a.try_acquire()  # renewing


async def test_standby_waits_while_the_leader_renews():
    kube, clock = FakeKube(), Clock()
    a = LeaderElector(kube, 'lock', 'a', clock)
    b = LeaderElector(kube, 'lock', 'b', clock)
    assert await a.try_acquire()
    for _ in range(10):
        clock.now += leader.LEASE_DURATION / 2
        assert await a.try_acquire()
        assert not await b.try_acquire()
    assert b.holder == 'a'


async def test_standby_takes_over_once_the_lease_expires():
    kube, clock = FakeKube(), Clock()
    a = LeaderElector(kube, 'lock', 'a', clock)
    b = LeaderElector(kube, 'lock', 'b', clock)
    assert await a.try_acquire()
    assert not await b.try_acquire()  # b starts timing the lease from here
    clock.now += leader.LEASE_DURATION - 1
    assert not await b.try_acquire()
    clock.now += 2
    assert await b.try_acquire()
    lease = kube.objects['/apis/coordination.k8s.io/v1/namespaces/don-ju/leases/lock']
    assert lease['spec']['holderIdentity'] == 'b'
    assert lease['spec']['leaseTransitions'] == 1


async def test_release_hands_over_immediately():
    kube, clock = FakeKube(), Clock()
    a = LeaderElector(kube, 'lock', 'a', clock)
    b = LeaderElector(kube, 'lock', 'b', clock)
    assert await a.try_acquire()
    assert not await b.try_acquire()
    await a.release()
    assert await b.try_acquire()


async def test_api_errors_count_as_not_leading():
    kube, clock = FakeKube(), Clock()
    a = LeaderElector(kube, 'lock', 'a', clock)
    kube.fail = True
    assert not await a.try_acquire()
