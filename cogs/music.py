import discord
from discord import app_commands, ui
from discord.ext import commands
import asyncio
import contextlib
import logging
import shutil
import tempfile
import yt_dlp
import os
import json
import re
import random
import time

import metrics
import songstore

log = logging.getLogger(__name__)

# yt-dlp options
ytdl_format_options = {
    'format': 'ba/b',
    'outtmpl': 'songs/%(id)s.%(ext)s',
    'writeinfojson': True,
    'updatetime': False,  # mtime = download time, which cache eviction relies on
    'restrictfilenames': True,
    'noplaylist': True,
    'quiet': True,
    'noprogress': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    # No player_client override: yt-dlp picks the YouTube clients. Forcing
    # android,web with cookies skipped android and left web only format 18, a
    # 360p video with 96 kbps audio, instead of 130 kbps Opus.
    # JS challenges are solved with Node.js and the yt-dlp-ejs package
    'js_runtimes': {
        'node': {}
    },
}

# A Netscape cookie file from a logged-in YouTube session. In the cluster it
# is a read-only Secret mount that External Secrets keeps up to date.
COOKIES_FILE = os.getenv('COOKIES_FILE_PATH', '/app/cookies.txt')

# Songs always play from a file in the cache
ffmpeg_options = {
    'options': '-vn'
}

# Only used to work out file paths; extraction gets its own instance per call
# because YoutubeDL is not thread-safe
ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

# Each extraction can start a Node.js process (~200MB) to solve YouTube's JS
# challenges. Cap how many run at once so overlapping /play requests can't
# push the pod past its memory limit.
ytdl_slots = asyncio.Semaphore(2)

# Keep the song cache under this size; least recently played songs go first
CACHE_MAX_BYTES = int(os.getenv('CACHE_MAX_BYTES', 4 * 1024**3))
CACHE_TEMP_SUFFIXES = ('.part', '.ytdl', '.temp')

# Songs longer than this are refused
MAX_SONG_SECONDS = 600

# Fields of a song's info dict kept in the saved queue. The full dict is
# hundreds of KB, too big for the ConfigMap the queue is saved to.
STATE_SONG_KEYS = ('id', 'ext', 'title', 'webpage_url', 'duration', 'thumbnail',
                   'uploader', 'requested_by', '_resume_position')

FLAVOR_TEXTS = {
    "download": [
        "⬇️ **Intercepting transmission...** Downloading `{query}`...",
        "📡 **Acquiring signal...** Fetching `{query}`...",
        "👾 **Decoding matrix...** Downloading `{query}`...",
        "⚡ **Charging capacitors...** Getting `{query}` ready...",
    ],
    "cache": [
        "💿 **Dusting off the vinyl...** Found `{query}` in cache!",
        "💾 **Loading from memory banks...** `{query}` is ready!",
        "📼 **Rewinding tape...** `{query}` found locally!",
        "📦 **Unboxing archives...** `{query}` is cached!",
    ],
}

def trim_song(song):
    return {k: song[k] for k in STATE_SONG_KEYS if k in song}

def cached_first(results):
    """Search results with cached songs first, each group in YouTube's order (best match first)."""
    return sorted(results, key=lambda song: not song['is_cached'])

def fmt_duration(seconds):
    """m:ss, or ? when the length is unknown (live streams have none)."""
    if seconds is None:
        return "?"
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"

def shorten(text, limit):
    return text if len(text) <= limit else text[:limit - 3] + "..."

def link(title, url):
    return f"[{title}]({url})" if url else title

def check_length(song):
    """Refuse songs longer than MAX_SONG_SECONDS."""
    duration = song.get('duration')
    if duration and duration > MAX_SONG_SECONDS:
        raise ValueError(f"❌ **Song Too Long**: This video is {int(duration // 60)}m {int(duration % 60):02d}s, "
                         f"but the limit is {MAX_SONG_SECONDS // 60} minutes. Please choose a shorter song.")

def cache_stats():
    """Return (total bytes, number of songs) in the cache."""
    total = songs = 0
    if os.path.isdir('songs'):
        for entry in os.scandir('songs'):
            if entry.is_file():
                total += entry.stat().st_size
                if not entry.name.endswith(('.json',) + CACHE_TEMP_SUFFIXES):
                    songs += 1
    return total, songs

def cached_audio():
    """(video id, file name, size) of each song file in the cache."""
    if not os.path.isdir('songs'):
        return []
    return [(entry.name.rsplit('.', 1)[0], entry.name, entry.stat().st_size)
            for entry in os.scandir('songs')
            if entry.is_file() and not entry.name.endswith(('.json',) + CACHE_TEMP_SUFFIXES)]

def info_file(video_id):
    return os.path.join('songs', f'{video_id}.info.json')

def read_info(video_id):
    """A cached song's info.json, or None if it's missing or unreadable."""
    try:
        with open(info_file(video_id)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None

@contextlib.contextmanager
def _ytdl():
    """A YoutubeDL with its own copy of the cookie file.

    yt-dlp writes cookies back when it closes, which a read-only mount can't
    take, and a fresh copy per call picks up refreshed cookies without a restart.
    """
    opts = dict(ytdl_format_options)
    cookies = None
    if os.path.exists(COOKIES_FILE):
        fd, cookies = tempfile.mkstemp(suffix='.txt')
        os.close(fd)
        shutil.copyfile(COOKIES_FILE, cookies)
        opts['cookiefile'] = cookies
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            yield ydl
    finally:
        if cookies:
            os.unlink(cookies)

def _run_ytdl(url, kwargs):
    with _ytdl() as ydl:
        data = ydl.extract_info(url, **kwargs)
        # Search results can be a lazy generator; resolve it while ydl is open
        if data and 'entries' in data:
            data['entries'] = list(data['entries'])
        return data

def _download_from_info(info):
    with _ytdl() as ydl:
        ydl.process_ie_result(info, download=True)

async def _in_slot(kind, func, *args):
    """Run a yt-dlp call off the event loop, at most ytdl_slots at a time."""
    async with ytdl_slots:
        start = time.monotonic()
        try:
            result = await asyncio.get_running_loop().run_in_executor(None, func, *args)
        except Exception:
            metrics.YTDL_CALLS.labels(kind, 'error').inc()
            raise
        finally:
            metrics.YTDL_SECONDS.labels(kind).observe(time.monotonic() - start)
        metrics.YTDL_CALLS.labels(kind, 'ok').inc()
        return result

async def extract_info(url, **kwargs):
    if kwargs.get('process') is False:
        kind = 'search'
    elif kwargs.get('download', True):
        kind = 'extract_download'
    else:
        kind = 'extract'
    return await _in_slot(kind, _run_ytdl, url, kwargs)

async def song_info(url):
    """Look a song up without downloading it."""
    data = await extract_info(url, download=False)
    if 'entries' in data:
        # A playlist or search: take the first item
        data = data['entries'][0]
    return data

async def download(info):
    """Download a song into the cache.

    Reuses the stream URLs resolved when the song was queued, which skips a
    second page fetch and JS challenge. They expire after a few hours, and a
    queue restored after a restart doesn't keep them, so fall back to a fresh
    extraction when they can't be used.
    """
    if info.get('formats'):
        try:
            await _in_slot('download', _download_from_info, dict(info))
            return
        except Exception as e:
            log.info("Download from queued info failed (%s), extracting again", e)
    await extract_info(info['webpage_url'], download=True)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5, is_cached=False, origin=None):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.thumbnail = data.get('thumbnail')
        self.duration = data.get('duration')
        self.requested_by = data.get('requested_by')
        self.is_cached = is_cached
        # cache | store | download: where the audio file came from
        self.origin = origin or ('cache' if is_cached else 'download')
        self.webpage_url = data.get('webpage_url')

    @classmethod
    def create_from_data(cls, data, is_cached=False, seek_offset=0, origin=None):
        """An audio source for a song in the cache, starting seek_offset seconds in."""
        check_length(data)
        options = dict(ffmpeg_options)
        if seek_offset > 0:
            options['before_options'] = f"-ss {int(seek_offset)}"
        return cls(discord.FFmpegPCMAudio(ytdl.prepare_filename(data), **options),
                   data=data, is_cached=is_cached, origin=origin)

class MusicPlayer:
    def __init__(self, bot, guild, channel):
        self.bot = bot
        self.guild = guild
        self.channel = channel
        self.queue = asyncio.Queue()  # info dicts of the songs waiting
        self.next = asyncio.Event()

        self.volume = .5
        self.current = None
        self.playback_start_time = None  # Track when playback started
        self.seek_position = 0  # Position to seek to when resuming (in seconds)
        self.resumed_from_state = False  # set when the queue was restored after a restart
        self.save_task = None  # Saves state every 10s while a song plays

        self.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            try:
                # Wait for the next song; after 5 minutes with nothing queued, leave
                async with asyncio.timeout(300):
                    song = await self.queue.get()
            except TimeoutError:
                return self.destroy(self.guild)

            try:
                # Local cache, then the shared song store, then YouTube
                origin = await self.bot.get_cog("Music").fetch_song(song)
                source = YTDLSource.create_from_data(song, is_cached=origin != 'download',
                                                     seek_offset=self.seek_position, origin=origin)
                self.seek_position = 0
            except ValueError as e:
                await self.channel.send(f"{e}")
                continue
            except Exception as e:
                log.warning(f"Error converting data: {e}")
                await self.channel.send(f'Error creating audio source: {e}')
                continue

            self.current = source
            source.volume = self.volume

            try:
                def after_callback(error):
                    if error:
                        log.debug(f"Player error: {error}")
                    self.bot.loop.call_soon_threadsafe(self.next.set)

                self.playback_start_time = time.time()
                self.guild.voice_client.play(source, after=after_callback)
                metrics.SONGS_PLAYED.labels(source.origin).inc()

                # Set bot status to "Listening to [Song Name]"
                await self.bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name=source.title))

                embed = discord.Embed(title="Now Playing", description=f"[{source.title}]({source.webpage_url})", color=discord.Color.purple())
                if source.thumbnail:
                    embed.set_image(url=source.thumbnail)
                if source.duration:
                    embed.add_field(name="Duration", value=fmt_duration(source.duration), inline=True)
                embed.add_field(name="Source", value="💾 Cached" if source.is_cached else "☁️ New", inline=True)
                if source.requested_by:
                    embed.add_field(name="Requested By", value=source.requested_by, inline=True)

                # The first song after a restart says where it picked up, if not from the start
                resume_position = song.get('_resume_position', 0)
                if self.resumed_from_state and resume_position > 0:
                    embed.set_footer(text=f"🔄 Resumed after bot restart at {fmt_duration(resume_position)}")
                self.resumed_from_state = False

                await self.channel.send(embed=embed)

                # Save the queue and position every 10 seconds while the song plays
                async def periodic_save():
                    await asyncio.sleep(10)
                    while self.guild.voice_client and self.guild.voice_client.is_playing():
                        self.bot.get_cog("Music").save_state()
                        await asyncio.sleep(10)

                # A skip within 10s would otherwise leave the old loop running too
                if self.save_task:
                    self.save_task.cancel()
                self.save_task = self.bot.loop.create_task(periodic_save())
            except Exception as e:
                log.warning(f"Error starting playback: {e}")
                await self.channel.send(f"Error starting playback: {e}")
                self.next.set()  # Ensure we don't get stuck

            await self.next.wait()
            # Make sure the FFmpeg process is cleaned up
            try:
                source.cleanup()
            except Exception as e:
                log.debug(f"Error cleaning up source: {e}")
            self.current = None
            await self.bot.get_cog("Music").set_default_status()
            self.bot.get_cog("Music").save_state()

    def destroy(self, guild):
        # Cleanup via the Cog
        cog = self.bot.get_cog("Music")
        if cog:
            return self.bot.loop.create_task(cog.cleanup(guild))

class SearchButton(ui.Button):
    def __init__(self, title, url, is_cached, cog, interaction_user):
        # Cached songs are green, new downloads blue; labels hold 80 characters
        style = discord.ButtonStyle.green if is_cached else discord.ButtonStyle.blurple
        emoji = "💾" if is_cached else "☁️"
        super().__init__(style=style, label=shorten(title, 80), emoji=emoji)
        self.video_url = url
        self.cog = cog
        self.interaction_user = interaction_user

    async def callback(self, interaction: discord.Interaction):
        # Only the requester can click
        if interaction.user != self.interaction_user:
            return await interaction.response.send_message("This search menu is not for you!", ephemeral=True)
        await interaction.response.defer()
        await self.cog.queue_song(interaction, self.video_url)

class SearchView(ui.View):
    def __init__(self, cog, interaction_user):
        super().__init__(timeout=60)
        self.cog = cog
        self.interaction_user = interaction_user

    @ui.button(label="Cancel", style=discord.ButtonStyle.red, emoji="❌")
    async def cancel_button(self, interaction: discord.Interaction, button: ui.Button):
        # Only the requester can cancel
        if interaction.user != self.interaction_user:
            return await interaction.response.send_message("❌ This search menu is not for you!", ephemeral=True)
        # Remove the search menu; it may be gone already
        with contextlib.suppress(discord.NotFound):
            await interaction.response.defer()
            await interaction.delete_original_response()

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players = {}
        self.state_loaded = False
        self.store = bot.state_store
        self.store_task = None
        self.frozen = False  # set on shutdown so the final saved state isn't overwritten
        self.song_store = songstore.SongStore.from_env()  # None when off
        self.cleanup_partial_files()
        metrics.QUEUE_LENGTH.set_function(lambda: sum(p.queue.qsize() for p in list(self.players.values())))
        metrics.VOICE_CONNECTIONS.set_function(lambda: len(self.bot.voice_clients))
        metrics.CACHE_BYTES.set_function(lambda: cache_stats()[0])
        metrics.CACHE_SONGS.set_function(lambda: cache_stats()[1])

    async def cog_load(self):
        if self.store.kube:
            self.store_task = asyncio.create_task(self.store.run())

    async def cog_unload(self):
        if self.store_task:
            self.store_task.cancel()

    async def shutdown(self):
        """Save the queue for whichever replica takes over, then stop saving."""
        self.save_state()
        self.frozen = True
        await self.store.flush()

    async def fetch_song(self, info):
        """Make sure a song's audio is in the local cache, and say where it came from.

        'cache' if it was already here, 'store' if it was copied from the shared
        song store, 'download' if it came from YouTube (it's then shared to the store).
        """
        filename = ytdl.prepare_filename(info)
        if os.path.exists(filename):
            # Mark as recently played so cache eviction keeps it, here and in the store
            os.utime(filename)
            if self.song_store:
                self.song_store.touch_later(filename)
            return 'cache'

        self.cleanup_cache()
        if self.song_store and await self.song_store.fetch(filename):
            return 'store'
        await download(info)
        if info.get('vcodec') not in (None, 'none'):
            log.warning(f"No audio-only format for {info.get('id')}; got video format {info.get('format_id')}, "
                        "so the sound is lower quality")
        if self.song_store:
            self.song_store.upload_later(filename)
        return 'download'

    async def prefetch(self, song):
        """Download a queued song before its turn comes."""
        try:
            await self.fetch_song(song)
        except Exception as e:
            log.warning(f"Background download of {song.get('id')} failed, retrying when it plays: {e}")

    def cleanup_cache(self):
        """Evict least recently played songs until the cache fits CACHE_MAX_BYTES."""
        if not os.path.exists('songs'):
            return

        # Never evict a song that is playing or queued
        in_use = set()
        for player in self.players.values():
            if player.current:
                in_use.add(player.current.data.get('id'))
            in_use.update(song.get('id') for song in list(player.queue._queue))

        total = 0
        audio = []  # (mtime, video_id, path)
        for filename in os.listdir('songs'):
            path = os.path.join('songs', filename)
            if not os.path.isfile(path):
                continue
            total += os.path.getsize(path)
            if filename.endswith(('.json',) + CACHE_TEMP_SUFFIXES):
                continue
            audio.append((os.path.getmtime(path), filename.rsplit('.', 1)[0], path))

        for _, video_id, path in sorted(audio):
            if total <= CACHE_MAX_BYTES:
                break
            if video_id in in_use:
                continue
            for victim in (path, info_file(video_id)):
                try:
                    total -= os.path.getsize(victim)
                    os.remove(victim)
                except FileNotFoundError:
                    pass
            log.info(f"Evicted {video_id} from cache")
            metrics.CACHE_EVICTIONS.inc()

    def cleanup_partial_files(self):
        """Clean up .part, .ytdl, and .temp files on startup."""
        if not os.path.exists('songs'):
            return

        for filename in os.listdir('songs'):
            if filename.endswith(CACHE_TEMP_SUFFIXES):
                try:
                    os.remove(os.path.join('songs', filename))
                except Exception as e:
                    log.warning(f"Failed to delete {filename}: {e}")

    def save_state(self):
        """Save each guild's queue, the playing song first with how far into it we are."""
        if self.frozen:
            return
        state = {}
        for guild_id, player in self.players.items():
            queue_list = []
            if player.current:
                current = trim_song(player.current.data)
                elapsed = time.time() - player.playback_start_time if player.playback_start_time else 0
                current['_resume_position'] = int(elapsed)
                queue_list.append(current)
            queue_list.extend(trim_song(song) for song in player.queue._queue)

            # Only save if there's something in the queue
            if queue_list:
                state[guild_id] = {
                    'voice_channel': player.guild.voice_client.channel.id if player.guild.voice_client else None,
                    'text_channel': player.channel.id,
                    'queue': queue_list
                }

        self.store.save(state)

    async def load_state(self):
        """Restore the saved queues after a restart, and say so in each guild's channel."""
        await self.bot.wait_until_ready()
        try:
            state = await self.store.load()

            for guild_id, data in state.items():
                guild = self.bot.get_guild(int(guild_id))
                queue = data['queue']
                if not guild or not queue:
                    continue
                voice_channel = guild.get_channel(data['voice_channel'])
                text_channel = guild.get_channel(data['text_channel'])
                if not (voice_channel and text_channel):
                    continue

                if not guild.voice_client or not guild.voice_client.is_connected():
                    try:
                        await voice_channel.connect()
                    except Exception as e:
                        log.warning(f"Failed to reconnect voice: {e}")
                        continue

                player = self.players.get(guild.id)
                if player is None:
                    player = self.players[guild.id] = MusicPlayer(self.bot, guild, text_channel)
                for song in queue:
                    await player.queue.put(song)
                # The first song was playing; pick it up where it was
                player.seek_position = queue[0].get('_resume_position', 0)
                player.resumed_from_state = True

                # Up to 10 songs, the first with where it resumes
                lines = []
                for i, song in enumerate(queue[:10], 1):
                    title = shorten(song.get('title', 'Unknown'), 50)
                    if i == 1 and '_resume_position' in song:
                        lines.append(f"`{i}.` {title} `(resuming at {fmt_duration(song['_resume_position'])})`")
                    else:
                        lines.append(f"`{i}.` {title}")
                if len(queue) > 10:
                    lines.append(f"\n*...and {len(queue) - 10} more songs*")

                resume_embed = discord.Embed(
                    title="🔄 Bot Resumed",
                    description="I'm back! Resuming playback from where we left off...",
                    color=discord.Color.blue()
                )
                resume_embed.add_field(name="📋 Queue Status", value=f"**{len(queue)}** song(s) queued", inline=False)
                resume_embed.add_field(name="🎵 Up Next", value="\n".join(lines), inline=False)
                resume_embed.set_footer(text="▶️ Starting playback now")
                await text_channel.send(embed=resume_embed)

                log.info(f"Restored {len(queue)} queued song(s) in {guild.name}")
        except Exception as e:
            log.warning(f"Error loading state: {e}")

    async def cleanup(self, guild):
        """Leave voice in a guild and drop its player."""
        if guild.voice_client:
            await guild.voice_client.disconnect()
        self.players.pop(guild.id, None)

        # Reset status if no other guilds are playing
        if not any(p.guild.voice_client and p.guild.voice_client.is_playing() for p in self.players.values()):
            await self.set_default_status()

    async def set_default_status(self):
        """Sets the bot's status to the default 'The Don'ju'."""
        await self.bot.change_presence(activity=discord.Game(name="The Don'ju | /help"))

    @commands.Cog.listener()
    async def on_ready(self):
        """Sets default status when cog is ready."""
        await self.set_default_status()
        # on_ready fires again on every gateway reconnect; only restore once
        if not self.state_loaded:
            self.state_loaded = True
            await self.load_state()

    def get_player(self, interaction):
        """The guild's player, created (bound to this text channel) if there isn't one."""
        try:
            player = self.players[interaction.guild.id]
        except KeyError:
            player = MusicPlayer(interaction.client, interaction.guild, interaction.channel)
            self.players[interaction.guild.id] = player
        return player

    async def queue_song(self, interaction: discord.Interaction, query: str):
        """Look a song up by URL and add it to the guild's queue."""
        # A song cached here or in the song store needs no YouTube lookup
        data = None
        match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', query)
        if match:
            video_id = match.group(1)
            # Another replica or the other bot may have stored it already
            if not os.path.exists(info_file(video_id)) and self.song_store:
                await self.song_store.fetch(info_file(video_id), with_info=False)
            if os.path.exists(info_file(video_id)):
                data = read_info(video_id)
                if not data:
                    log.warning(f"Failed to load cache for {video_id}")
        is_cache_hit = bool(data)

        if is_cache_hit:
            initial_msg = random.choice(FLAVOR_TEXTS["cache"]).format(query=data.get('title', query))
        else:
            initial_msg = f"🔗 **Establishing Connection...**\n\nAccessing: `{query}`"

        # Works for both deferred commands and button clicks
        try:
            await interaction.edit_original_response(content=initial_msg, view=None, embed=None)
        except discord.NotFound:
            await interaction.followup.send(initial_msg)

        if not is_cache_hit:
            try:
                data = await song_info(query)
            except Exception as e:
                await interaction.edit_original_response(content=f"Error finding song: {e}")
                return

            # The lookup names the song's file, which may be cached after all
            filename = ytdl.prepare_filename(data)
            is_cache_hit = os.path.exists(filename) or bool(self.song_store and await self.song_store.exists(filename))
            texts = FLAVOR_TEXTS["cache" if is_cache_hit else "download"]
            await interaction.edit_original_response(content=random.choice(texts).format(query=data.get('title', query)))

        try:
            check_length(data)
            player = self.get_player(interaction)
            data['requested_by'] = interaction.user.name

            # Check if this will play immediately or be queued
            vc = interaction.guild.voice_client
            will_play_immediately = player.queue.empty() and (not vc or not vc.is_playing())

            await player.queue.put(data)

            if not will_play_immediately:
                # Download it while it waits its turn
                if not is_cache_hit:
                    self.bot.loop.create_task(self.prefetch(data))

                embed = discord.Embed(title="Queued", description=f"[{data['title']}]({data['webpage_url']})", color=discord.Color.green())
                if data.get('thumbnail'):
                    embed.set_image(url=data['thumbnail'])
                if data.get('duration'):
                    embed.add_field(name="Duration", value=fmt_duration(data['duration']))
                embed.add_field(name="Position in Queue", value=f"#{player.queue.qsize()}", inline=True)
                embed.add_field(name="Requested By", value=interaction.user.mention, inline=True)
                embed.set_footer(text="💾 Instant Load (Cached)" if is_cache_hit else "☁️ New Download")
                await interaction.channel.send(embed=embed)

            # The status message was only for the requester; remove it
            try:
                await interaction.delete_original_response()
            except Exception:
                # Fallback if delete fails (e.g. too old), just edit to empty
                await interaction.edit_original_response(content="✅ Queued", embed=None, view=None)

            self.save_state()

        except ValueError as e:
            await interaction.edit_original_response(content=f"{e}")
        except Exception as e:
            await interaction.edit_original_response(content=f"An error occurred: {e}")

    @app_commands.command(name="play", description="Plays a song from YouTube")
    @app_commands.describe(search="The YouTube URL or search query (use 'random' for a random cached song)")
    async def play(self, interaction: discord.Interaction, search: str):
        """Plays a song."""
        if not interaction.user.voice:
            return await interaction.response.send_message('❌ You need to be in a voice channel to play music!', ephemeral=True)

        # "random": a song from this replica's cache
        if search.lower() == 'random':
            cached = [video_id for video_id, _, _ in cached_audio() if os.path.exists(info_file(video_id))]
            random.shuffle(cached)
            info = next((info for info in map(read_info, cached) if info and info.get('webpage_url')), None)
            if not info:
                return await interaction.response.send_message('❌ No cached songs available!', ephemeral=True)
            search = info['webpage_url']

        # Always reply privately; the public messages come later
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException as e:
            # 40060: already acknowledged, which is fine
            if e.code != 40060:
                raise

        # Creates the guild's player, bound to this text channel
        self.get_player(interaction)

        if interaction.guild.voice_client is None:
            # Checked again: the user may have left since the command started
            if not interaction.user.voice:
                await interaction.followup.send("❌ You need to be in a voice channel to play music!")
                return
            connecting_embed = discord.Embed(
                title="🔗 Establishing Connection",
                description=f"**Joining:** {interaction.user.voice.channel.name}\n\n🎵 Getting ready to play music...",
                color=discord.Color.green()
            )
            connecting_embed.set_footer(text="✅ Connected! Ready to play")
            await interaction.followup.send(embed=connecting_embed, ephemeral=True)
            await interaction.user.voice.channel.connect()

        # A URL is queued directly; anything else is a search
        if search.startswith(('http://', 'https://')):
            await self.queue_song(interaction, search)
            return

        embed = discord.Embed(
            title="🔍 Searching YouTube",
            description=f"**Query:** {search}\n\n🔄 Scanning YouTube's library...",
            color=discord.Color.blue()
        )
        embed.set_footer(text="⚡ This usually takes just a few seconds")
        scan_msg = await interaction.followup.send(embed=embed)

        try:
            data = await extract_info(f"ytsearch5:{search}", download=False, process=False)
            entries = data.get('entries') or []
            if not entries:
                error_embed = discord.Embed(
                    title="❌ No Results Found",
                    description=f"Couldn't find anything for: **{search}**\n\n💡 Try a different search term!",
                    color=discord.Color.red()
                )
                await scan_msg.edit(embed=error_embed)
                return

            results = cached_first([{
                'title': entry.get('title', 'Unknown Title'),
                'url': entry.get('url', ''),
                'is_cached': bool(entry.get('id')) and os.path.exists(info_file(entry['id'])),
            } for entry in entries[:5]])

            # One button per row, so the results read top to bottom
            view = SearchView(self, interaction.user)
            for row, song in enumerate(results):
                button = SearchButton(song['title'], song['url'], song['is_cached'], self, interaction.user)
                button.row = row
                view.add_item(button)

            results_embed = discord.Embed(
                title="🎵 Select a Song",
                description="Cached songs are shown first:",
                color=discord.Color.blue()
            )
            if entries[0].get('thumbnail'):
                results_embed.set_thumbnail(url=entries[0]['thumbnail'])
            results_embed.set_footer(text="🟢 Cached (Instant) | 🔵 New Download")
            await scan_msg.edit(embed=results_embed, view=view)

        except Exception as e:
            error_embed = discord.Embed(
                title="⚠️ Search Error",
                description="Something went wrong while searching.\n\n💡 Try again in a moment!",
                color=discord.Color.orange()
            )
            error_embed.add_field(name="🔍 Error Details", value=f"```{str(e)[:200]}```", inline=False)
            await scan_msg.edit(embed=error_embed)

    @app_commands.command(name="skip", description="Skips the song")
    async def skip(self, interaction: discord.Interaction):
        """Skip the song."""
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message('❌ I\'m not currently playing anything!', ephemeral=True)
        if not vc.is_playing() and not vc.is_paused():
            return await interaction.response.send_message('❌ Nothing is playing right now!', ephemeral=True)

        player = self.get_player(interaction)
        current = player.current
        queue_size = player.queue.qsize()

        vc.stop()
        self.save_state()

        embed = discord.Embed(
            title="⏭️ Song Skipped",
            description=f"**{link(current.title, current.webpage_url)}**" if current else "**Unknown**",
            color=discord.Color.orange()
        )
        if current and current.thumbnail:
            embed.set_thumbnail(url=current.thumbnail)
        if current and current.duration:
            embed.add_field(name="⏱️ Duration", value=fmt_duration(current.duration), inline=True)
        embed.add_field(name="👤 Skipped By", value=interaction.user.mention, inline=True)
        embed.add_field(name="📋 Songs in Queue", value=f"{queue_size} remaining", inline=True)
        embed.set_footer(text="▶️ Playing next song now" if queue_size > 0 else "Queue is now empty")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="stop", description="Stops the song and clears the queue")
    async def stop(self, interaction: discord.Interaction):
        """Stops playing song and clears the queue."""
        vc = interaction.guild.voice_client

        if not vc or not vc.is_connected():
            return await interaction.response.send_message('❌ I\'m not currently connected to a voice channel!', ephemeral=True)

        player = self.get_player(interaction)
        while not player.queue.empty():
            player.queue.get_nowait()
        vc.stop()

        embed = discord.Embed(
            title="⏹️ Playback Stopped",
            description=f"Stopped by {interaction.user.mention}\nQueue cleared.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="queue", description="Shows the queue")
    async def queue_info(self, interaction: discord.Interaction):
        """Show the playing song and the ones waiting after it."""
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message('❌ I\'m not currently connected to a voice channel!', ephemeral=True)

        player = self.players.get(interaction.guild.id)
        current = player.current if player else None
        upcoming = list(player.queue._queue) if player else []
        if not current and not upcoming:
            empty_embed = discord.Embed(
                title="📭 Queue is Empty",
                description="No songs are currently queued.\n\n💡 Use `/play` to add some tracks!",
                color=discord.Color.light_gray()
            )
            return await interaction.response.send_message(embed=empty_embed)

        fmt = ""
        total_duration = 0
        if current:
            total_duration += current.duration or 0
            title = link(shorten(current.title, 48), current.webpage_url)
            fmt += f"▶️ **Now Playing:** {title} • `{fmt_duration(current.duration)}`\n\n"

        if upcoming:
            fmt += "**Up Next:**\n"
        for i, song in enumerate(upcoming):
            total_duration += song.get('duration') or 0
            title = shorten(song.get('title', 'Unknown Title'), 48)
            line = f"`{i + 1}.` [{title}]({song.get('webpage_url', '')}) • `{fmt_duration(song.get('duration'))}`\n"
            if len(fmt) + len(line) > 3800:  # Leave room for footer
                fmt += f"\n*...and {len(upcoming) - i} more*"
                break
            fmt += line

        total_tracks = len(upcoming) + (1 if current else 0)
        embed = discord.Embed(
            title=f'📜 Queue — {total_tracks} Track{"s" if total_tracks != 1 else ""}',
            description=fmt,
            color=discord.Color.blue()
        )
        if total_duration > 0:
            mins, secs = divmod(int(total_duration), 60)
            time_str = f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m {secs}s"
            embed.set_footer(text=f"⏱️ Total Duration: {time_str}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="cache", description="Shows cache statistics")
    async def cache_info(self, interaction: discord.Interaction):
        """Show how many songs this replica has cached, and the largest ones."""
        if not os.path.exists('songs'):
            return await interaction.response.send_message('❌ Cache directory not found!', ephemeral=True)

        total_size, total_songs = cache_stats()
        if total_size >= 1024**3:
            size_str = f"{total_size / 1024**3:.2f} GB"
        elif total_size >= 1024**2:
            size_str = f"{total_size / 1024**2:.2f} MB"
        else:
            size_str = f"{total_size / 1024:.2f} KB"

        embed = discord.Embed(
            title="💾 Cache Statistics",
            description=f"**{total_songs}** songs cached\n**{size_str}** total size",
            color=discord.Color.green()
        )

        largest = sorted(cached_audio(), key=lambda song: song[2], reverse=True)[:5]
        if largest:
            lines = []
            for i, (video_id, filename, size) in enumerate(largest, 1):
                info = read_info(video_id) or {}
                title = shorten(info.get('title', filename), 43)
                lines.append(f"`{i}.` {title} • `{fmt_duration(info.get('duration'))}` • {size / 1024**2:.1f} MB")
            embed.add_field(name="📊 Largest Files", value="\n".join(lines), inline=False)

        embed.set_footer(text="💡 Use /play to cache more songs automatically")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="playing", description="Shows the currently playing song")
    async def now_playing(self, interaction: discord.Interaction):
        """Shows the currently playing song."""
        if not interaction.user.voice:
            return await interaction.response.send_message('❌ You need to be in a voice channel to see what\'s playing!', ephemeral=True)

        player = self.players.get(interaction.guild.id)
        if not player or not player.current:
            return await interaction.response.send_message('❌ Nothing is currently playing!', ephemeral=True)

        source = player.current
        embed = discord.Embed(title="Now Playing", description=f"[{source.title}]({source.webpage_url})", color=discord.Color.purple())
        if source.thumbnail:
            embed.set_image(url=source.thumbnail)
        if source.duration:
            position = int(time.time() - player.playback_start_time) if player.playback_start_time else 0
            embed.add_field(name="Duration", value=f"{fmt_duration(position)} / {fmt_duration(source.duration)}", inline=True)
        if source.requested_by:
            embed.add_field(name="Requested By", value=source.requested_by, inline=True)
        embed.set_footer(text="💾 Playing from Cache" if source.is_cached else "☁️ New Download")
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    os.makedirs('songs', exist_ok=True)
    await bot.add_cog(Music(bot))
