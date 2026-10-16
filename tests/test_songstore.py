import asyncio
import datetime
import os

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

import songstore
from cogs import music


def not_found(op):
    return ClientError({'Error': {'Code': '404'}}, op)


class FakeS3:
    """Just enough of a boto3 S3 client, kept in a dict."""

    def __init__(self):
        self.objects = {}  # key -> (bytes, last modified)
        self.clock = 0

    def _now(self):
        self.clock += 1
        return datetime.datetime.fromtimestamp(self.clock, datetime.UTC)

    def put(self, key, data):
        self.objects[key] = (data, self._now())

    def download_file(self, bucket, key, filename):
        if key not in self.objects:
            raise not_found('HeadObject')
        with open(filename, 'wb') as f:
            f.write(self.objects[key][0])

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise not_found('HeadObject')
        return {}

    def upload_file(self, filename, bucket, key):
        with open(filename, 'rb') as f:
            self.put(key, f.read())

    def copy_object(self, Bucket, Key, CopySource, MetadataDirective):
        if CopySource['Key'] not in self.objects:
            raise not_found('CopyObject')
        self.put(Key, self.objects[CopySource['Key']][0])

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def get_paginator(self, name):
        fake = self

        class Paginator:
            def paginate(self, Bucket):
                yield {'Contents': [{'Key': k, 'Size': len(v[0]), 'LastModified': v[1]}
                                    for k, v in fake.objects.items()]}
        return Paginator()


class DownStore(FakeS3):
    def download_file(self, *args, **kwargs):
        raise EndpointConnectionError(endpoint_url='http://garage')

    head_object = download_file


@pytest.fixture
def songs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / 'songs'
    path.mkdir()
    return path


def run(coro):
    return asyncio.run(coro)


def test_fetch_copies_song_and_info(songs):
    s3 = FakeS3()
    s3.put('a.webm', b'audio')
    s3.put('a.info.json', b'{}')
    store = songstore.SongStore(s3, 'b', 10**9)
    assert run(store.fetch('songs/a.webm'))
    assert (songs / 'a.webm').read_bytes() == b'audio'
    assert (songs / 'a.info.json').exists()
    # No partial files left behind
    assert sorted(os.listdir(songs)) == ['a.info.json', 'a.webm']


def test_fetch_miss_leaves_nothing(songs):
    store = songstore.SongStore(FakeS3(), 'b', 10**9)
    assert not run(store.fetch('songs/a.webm'))
    assert os.listdir(songs) == []


def test_store_errors_count_as_misses(songs):
    store = songstore.SongStore(DownStore(), 'b', 10**9)
    assert not run(store.fetch('songs/a.webm'))
    assert not run(store.exists('songs/a.webm'))
    assert os.listdir(songs) == []


def test_upload_shares_song_and_info(songs):
    (songs / 'a.webm').write_bytes(b'audio')
    (songs / 'a.info.json').write_text('{}')
    s3 = FakeS3()
    store = songstore.SongStore(s3, 'b', 10**9)

    async def go():
        store.upload_later('songs/a.webm')
        await asyncio.gather(*store.tasks)
    run(go())
    assert sorted(s3.objects) == ['a.info.json', 'a.webm']


def test_eviction_drops_least_recently_played(songs):
    s3 = FakeS3()
    for video_id in 'abc':
        s3.put(f'{video_id}.webm', b'x' * 100)
        s3.put(f'{video_id}.info.json', b'{}')
    store = songstore.SongStore(s3, 'b', 250)
    # Playing "a" makes it the most recent
    (songs / 'a.webm').write_bytes(b'x' * 100)
    store._touch('songs/a.webm')
    store._evict()
    assert sorted(s3.objects) == ['a.info.json', 'a.webm', 'c.info.json', 'c.webm']


def test_touch_uploads_a_song_the_store_lacks(songs):
    (songs / 'a.webm').write_bytes(b'audio')
    s3 = FakeS3()
    songstore.SongStore(s3, 'b', 10**9)._touch('songs/a.webm')
    assert 'a.webm' in s3.objects


def test_store_off_without_endpoint(monkeypatch):
    monkeypatch.delenv('SONG_STORE_ENDPOINT', raising=False)
    assert songstore.SongStore.from_env() is None


def cog(store):
    music_cog = object.__new__(music.Music)
    music_cog.players = {}
    music_cog.song_store = store
    return music_cog


def test_fetch_song_prefers_local_then_store_then_download(songs, monkeypatch):
    downloads = []

    async def fake_download(info):
        downloads.append(info['id'])
        (songs / f"{info['id']}.webm").write_bytes(b'new')
    monkeypatch.setattr(music, 'download', fake_download)

    s3 = FakeS3()
    s3.put('b.webm', b'stored')
    store = songstore.SongStore(s3, 'bk', 10**9)
    (songs / 'a.webm').write_bytes(b'local')
    music_cog = cog(store)

    async def go():
        origins = [await music_cog.fetch_song({'id': v, 'ext': 'webm', 'title': v}) for v in 'abc']
        await asyncio.gather(*store.tasks)
        return origins
    assert run(go()) == ['cache', 'store', 'download']
    assert downloads == ['c']
    # The fresh download was shared
    assert s3.objects['c.webm'][0] == b'new'
