from state import StateStore
from tests.fakes import FakeKube


async def test_file_store_round_trip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'songs').mkdir()
    store = StateStore(None, 'unused')
    assert await store.load() == {}
    store.save({'1': {'queue': [{'id': 'abc'}]}})
    assert await store.load() == {'1': {'queue': [{'id': 'abc'}]}}
    assert not (tmp_path / 'songs' / 'state.json.tmp').exists()


async def test_configmap_store_round_trip():
    store = StateStore(FakeKube(), 'don-ju-state')
    assert await store.load() == {}
    store.save({'1': {'queue': []}})
    await store.flush()
    assert await store.load() == {'1': {'queue': []}}
    store.save({'2': {'queue': []}})
    await store.flush()
    assert await store.load() == {'2': {'queue': []}}
