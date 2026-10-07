import asyncio
import os
import types

import pytest

from cogs import music


@pytest.fixture
def songs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / 'songs'
    path.mkdir()
    return path


def add_song(songs, video_id, size, mtime):
    (songs / f'{video_id}.webm').write_bytes(b'x' * size)
    (songs / f'{video_id}.info.json').write_text('{}')
    os.utime(songs / f'{video_id}.webm', (mtime, mtime))


def cog(queued=(), playing=None):
    music_cog = object.__new__(music.Music)
    queue = asyncio.Queue()
    for video_id in queued:
        queue.put_nowait({'id': video_id})
    music_cog.players = {1: types.SimpleNamespace(current=playing, queue=queue)}
    return music_cog


def test_eviction_removes_oldest_songs_first_and_skips_queued(songs, monkeypatch):
    monkeypatch.setattr(music, 'CACHE_MAX_BYTES', 3000)
    for i, video_id in enumerate('abcd'):
        add_song(songs, video_id, 1000, 1000 + i)
    (songs / 'e.webm.part').write_bytes(b'x' * 10)
    cog(queued=['b']).cleanup_cache()
    assert sorted(os.listdir(songs)) == ['b.info.json', 'b.webm', 'd.info.json', 'd.webm', 'e.webm.part']


def test_no_eviction_under_the_limit(songs, monkeypatch):
    monkeypatch.setattr(music, 'CACHE_MAX_BYTES', 10_000)
    add_song(songs, 'a', 1000, 1000)
    cog().cleanup_cache()
    assert sorted(os.listdir(songs)) == ['a.info.json', 'a.webm']


def test_cache_stats(songs):
    add_song(songs, 'a', 1000, 1000)
    (songs / 'b.webm.part').write_bytes(b'x' * 10)
    assert music.cache_stats() == (1000 + 2 + 10, 1)


def test_trim_song_keeps_only_resume_fields():
    song = {'id': 'a', 'ext': 'webm', 'title': 't', 'formats': [{}] * 50, 'requested_by': 'me'}
    assert music.trim_song(song) == {'id': 'a', 'ext': 'webm', 'title': 't', 'requested_by': 'me'}


def test_music_cog_commands():
    names = {command.name for command in music.Music.__cog_app_commands__}
    assert names == {'play', 'skip', 'stop', 'queue', 'cache', 'playing'}
