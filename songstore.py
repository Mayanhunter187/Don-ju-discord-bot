"""The song store: an S3 bucket shared by every replica of both bots.

Each replica still plays from its own cache in songs/ (see cogs/music.py). The
store sits behind it: a song missing locally is copied from the store, and a
fresh download is uploaded to it, so a song comes from YouTube once instead of
once per replica and bot. In the cluster the store is Garage.

Off unless SONG_STORE_ENDPOINT and AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY are
set. Store errors are logged and count as misses, so playback never depends on
the store being up.
"""
import asyncio
import contextlib
import logging
import os
import tempfile

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

import metrics

log = logging.getLogger(__name__)

NOT_FOUND = ('404', 'NoSuchKey', 'NotFound')


def info_path(path):
    """The info.json written next to a song's audio file."""
    return os.path.join(os.path.dirname(path), os.path.basename(path).rsplit('.', 1)[0] + '.info.json')


def _missing(error):
    return isinstance(error, ClientError) and error.response.get('Error', {}).get('Code') in NOT_FOUND


class SongStore:
    def __init__(self, client, bucket, max_bytes):
        self.client = client
        self.bucket = bucket
        self.max_bytes = max_bytes
        self.tasks = set()  # background uploads and touches, kept until done

    @classmethod
    def from_env(cls):
        endpoint = os.getenv('SONG_STORE_ENDPOINT')
        if not endpoint or not os.getenv('AWS_ACCESS_KEY_ID'):
            log.info("Song store off: SONG_STORE_ENDPOINT or its access key is not set")
            return None
        client = boto3.client(
            's3',
            endpoint_url=endpoint,
            region_name=os.getenv('SONG_STORE_REGION', 'garage'),
            config=Config(s3={'addressing_style': 'path'}, connect_timeout=3, read_timeout=30,
                          retries={'max_attempts': 2, 'mode': 'standard'}),
        )
        bucket = os.getenv('SONG_STORE_BUCKET', 'don-ju-songs')
        max_bytes = int(os.getenv('SONG_STORE_MAX_BYTES', 20 * 1024**3))
        log.info(f"Song store on: bucket {bucket} at {endpoint}")
        return cls(client, bucket, max_bytes)

    # Blocking S3 calls; the async methods below run them in a thread

    def _fetch(self, path):
        """Copy the object named after path's file into path. False if the store hasn't got it."""
        # .temp: the music cog treats it as a partial download until the rename
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or '.', suffix='.temp')
        os.close(fd)
        try:
            self.client.download_file(self.bucket, os.path.basename(path), tmp)
            os.replace(tmp, path)
            return True
        except ClientError as e:
            if _missing(e):
                return False
            raise
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp)

    def _exists(self, key):
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if _missing(e):
                return False
            raise

    def _upload(self, path):
        """Upload a song and its info.json, then trim the store back under its limit."""
        for file in (path, info_path(path)):
            if os.path.exists(file):
                self.client.upload_file(file, self.bucket, os.path.basename(file))
        self._evict()
        return True

    def _touch(self, path):
        """Mark a song as just played, so the store keeps it longest.

        Copying an object onto itself renews its last-modified time. A song the
        store hasn't got (cached here before the store existed) is uploaded instead.
        """
        key = os.path.basename(path)
        try:
            self.client.copy_object(Bucket=self.bucket, Key=key, MetadataDirective='REPLACE',
                                    CopySource={'Bucket': self.bucket, 'Key': key})
        except ClientError as e:
            if not _missing(e):
                raise
            return self._upload(path)
        return True

    def _evict(self):
        """Delete the least recently played songs until the store fits max_bytes."""
        objects = []
        for page in self.client.get_paginator('list_objects_v2').paginate(Bucket=self.bucket):
            objects.extend(page.get('Contents', []))
        sizes = {o['Key']: o['Size'] for o in objects}
        total = sum(sizes.values())
        audio = sorted((o for o in objects if not o['Key'].endswith('.json')), key=lambda o: o['LastModified'])
        for song in audio:
            if total <= self.max_bytes:
                break
            for key in (song['Key'], info_path(song['Key'])):
                if key in sizes:
                    self.client.delete_object(Bucket=self.bucket, Key=key)
                    total -= sizes.pop(key)
            log.info(f"Evicted {song['Key']} from the song store")
            metrics.SONG_STORE.labels('evict', 'ok').inc()

    async def _call(self, op, func, *args):
        try:
            return await asyncio.to_thread(func, *args)
        except (BotoCoreError, ClientError, OSError) as e:
            log.warning(f"Song store {op} failed: {e}")
            metrics.SONG_STORE.labels(op, 'error').inc()
            return None

    async def fetch(self, path, with_info=True):
        """Copy a file from the store into path. True on a hit.

        For a song's audio file, its info.json comes along too (if stored).
        """
        hit = await self._call('fetch', self._fetch, path)
        if hit and with_info:
            await self._call('fetch', self._fetch, info_path(path))
        metrics.SONG_STORE.labels('fetch', 'hit' if hit else 'miss').inc()
        return bool(hit)

    async def exists(self, path):
        """True if the store has the song saved at path."""
        found = await self._call('exists', self._exists, os.path.basename(path))
        return bool(found)

    def upload_later(self, path):
        """Share a fresh download with the other replicas and bot, in the background."""
        self._background('upload', self._upload, path)

    def touch_later(self, path):
        """Mark a song as just played, in the background."""
        self._background('touch', self._touch, path)

    def _background(self, op, func, path):
        async def run():
            if await self._call(op, func, path):
                metrics.SONG_STORE.labels(op, 'ok').inc()
        task = asyncio.create_task(run())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
