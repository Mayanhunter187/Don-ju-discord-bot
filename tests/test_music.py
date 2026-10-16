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


def test_search_results_keep_youtube_order_with_cached_first():
    results = [{'title': t, 'is_cached': cached} for t, cached in
               [('Zebra', False), ('Apple', False), ('Mango', True), ('Banana', True)]]
    assert [r['title'] for r in music.cached_first(results)] == ['Mango', 'Banana', 'Zebra', 'Apple']


def test_fmt_duration():
    assert [music.fmt_duration(s) for s in (None, 0, 61, 599.6)] == ['?', '0:00', '1:01', '9:59']


def test_shorten():
    assert music.shorten('abcdef', 6) == 'abcdef'
    assert music.shorten('abcdefg', 6) == 'abc...'


def test_check_length_refuses_songs_over_ten_minutes():
    music.check_length({'duration': 600})
    music.check_length({})
    with pytest.raises(ValueError, match='10m 01s'):
        music.check_length({'duration': 601})


def test_cached_audio_and_read_info(songs):
    add_song(songs, 'a', 1000, 1000)
    (songs / 'b.webm.part').write_bytes(b'x' * 10)
    (songs / 'c.info.json').write_text('not json')
    assert music.cached_audio() == [('a', 'a.webm', 1000)]
    assert music.read_info('a') == {}
    assert music.read_info('c') is None
    assert music.read_info('missing') is None
