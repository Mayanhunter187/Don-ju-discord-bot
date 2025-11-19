import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import yt_dlp
import os

# YouTube DL options
# YouTube DL options
ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': 'songs/%(id)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': False,
    'no_warnings': False,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'cookiefile': '/app/cookies.txt',
    'verbose': True,
    'extractor_args': {
        'youtube': {
            'player_client': ['tv']
        }
    },
    'js_runtimes': {
        'node': {}
    },
    'remote_components': ['ejs:github']
}

ffmpeg_options_stream = {
    'options': '-vn',
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5'
}

ffmpeg_options_local = {
    'options': '-vn'
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.thumbnail = data.get('thumbnail')
        self.duration = data.get('duration')
        self.uploader = data.get('uploader')
        self.requested_by = data.get('requested_by')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        # This method is now a wrapper that does both extraction and creation
        # Useful for the player loop if it encounters a raw string
        loop = loop or asyncio.get_event_loop()
        data = await cls.get_info(url, loop=loop, stream=stream)
        return cls.create_from_data(data, stream=stream)

    @classmethod
    async def get_info(cls, url, *, loop=None, stream=False):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))

        if 'entries' in data:
            # take first item from a playlist
            data = data['entries'][0]
        return data

    @classmethod
    def create_from_data(cls, data, stream=False):
        # Max length check (10 minutes = 600 seconds)
        duration = data.get('duration')
        if duration and duration > 600:
            raise ValueError(f"❌ **Song Too Long**: This video is {int(duration//60)}m {int(duration%60)}s, but the limit is 10 minutes. Please choose a shorter song.")

        filename = data['url'] if stream else ytdl.prepare_filename(data)
        options = ffmpeg_options_stream if stream else ffmpeg_options_local
        return cls(discord.FFmpegPCMAudio(filename, **options), data=data)

class MusicPlayer:
    def __init__(self, interaction):
        self.bot = interaction.client
        self.guild = interaction.guild
        self.channel = interaction.channel
        self.queue = asyncio.Queue()
        self.next = asyncio.Event()

        self.np = None  # Now playing message
        self.volume = .5
        self.current = None

        self.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            try:
                # Wait for the next song. If we timeout cancel the player and disconnect...
                async with asyncio.timeout(300):  # 5 minutes
                    source = await self.queue.get()
            except asyncio.TimeoutError:
                return self.destroy(self.guild)

            if isinstance(source, dict):
                # It's pre-fetched data
                try:
                    # Check if we need to download (Cache Logic)
                    filename = ytdl.prepare_filename(source)
                    if not os.path.exists(filename):
                        # Cleanup cache if needed
                        self.bot.get_cog("Music").cleanup_cache()
                        
                        # Download
                        await self.bot.loop.run_in_executor(None, lambda: ytdl.extract_info(source['webpage_url'], download=True))
                    
                    # Create source from local file (stream=False)
                    source = YTDLSource.create_from_data(source, stream=False)
                except ValueError as e:
                    await self.channel.send(f"{e}")
                    continue
                except Exception as e:
                    await self.channel.send(f'Error creating audio source: {e}')
                    continue
            elif not isinstance(source, YTDLSource):
                # Source was probably a stream (not downloaded) and not a dict
                try:
                    source = await YTDLSource.from_url(source, loop=self.bot.loop, stream=True)
                except ValueError as e:
                    await self.channel.send(f"{e}")
                    continue
                except Exception as e:
                    await self.channel.send(f'There was an error processing your song.\n'
                                            f'```css\n[{e}]\n```')
                    continue

            source.volume = self.volume
            self.current = source

            try:
                self.guild.voice_client.play(source, after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set))
                
                # Create Embed for Now Playing
                embed = discord.Embed(title="Now Playing", description=f"[{source.title}]({source.url})", color=discord.Color.blurple())
                if source.thumbnail:
                    embed.set_thumbnail(url=source.thumbnail)
                if source.duration:
                    embed.add_field(name="Duration", value=f"{int(source.duration//60)}:{int(source.duration%60):02d}")
                if source.requested_by:
                    embed.set_footer(text=f"Requested by {source.requested_by}")
                
                self.np = await self.channel.send(embed=embed)
            except Exception as e:
                await self.channel.send(f"Error starting playback: {e}")
                self.next.set() # Ensure we don't get stuck

            await self.next.wait()

            # Make sure the FFmpeg process is cleaned up.
            source.cleanup()
            self.current = None

    def destroy(self, guild):
        # Cleanup via the Cog
        cog = self.bot.get_cog("Music")
        if cog:
            return self.bot.loop.create_task(cog.cleanup(guild))

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    def cleanup_cache(self):
        cache_dir = 'songs'
        max_size = 10 * 1024 * 1024 * 1024 # 10GB
        
        if not os.path.exists(cache_dir):
            return

        total_size = 0
        files = []

        for f in os.listdir(cache_dir):
            path = os.path.join(cache_dir, f)
            if os.path.isfile(path):
                size = os.path.getsize(path)
                total_size += size
                files.append((path, os.path.getmtime(path), size))
        
        if total_size > max_size:
            # Sort by modification time (oldest first)
            files.sort(key=lambda x: x[1])
            
            for path, _, size in files:
                try:
                    os.remove(path)
                    total_size -= size
                    print(f"Deleted {path} to free space.")
                    if total_size <= max_size:
                        break
                except Exception as e:
                    print(f"Error deleting {path}: {e}")

    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass

        try:
            del self.players[guild.id]
        except KeyError:
            pass

    def get_player(self, interaction):
        try:
            player = self.players[interaction.guild.id]
        except KeyError:
            player = MusicPlayer(interaction)
            self.players[interaction.guild.id] = player
        return player

    async def play_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not current:
            return []
        
        # Use ytsearch5 to get top 5 results, flat extraction for speed
        query = f"ytsearch5:{current}"
        
        # We need to run this in an executor because extract_info is blocking
        # and we don't want to freeze the bot during autocomplete
        try:
            data = await self.bot.loop.run_in_executor(
                None, 
                lambda: ytdl.extract_info(query, download=False, process=False)
            )
            
            choices = []
            if 'entries' in data:
                for entry in data['entries']:
                    # entry is a dict with 'title', 'url', etc.
                    # Note: process=False means we might get raw objects, but usually for ytsearch it returns a playlist dict
                    # If process=False, entries might be objects we need to resolve, but for ytsearch it usually gives minimal info.
                    # Let's try to be safe.
                    title = entry.get('title', 'Unknown Title')
                    url = entry.get('url', '')
                    
                    # Discord limits choice names to 100 chars
                    if len(title) > 100:
                        title = title[:97] + "..."
                        
                    if url:
                        choices.append(app_commands.Choice(name=title, value=url))
            
            return choices
        except Exception:
            # Autocomplete must not crash
            return []

    @app_commands.command(name="play", description="Plays a song from YouTube")
    @app_commands.describe(search="The YouTube URL or search query")
    @app_commands.autocomplete(search=play_autocomplete)
    async def play(self, interaction: discord.Interaction, search: str):
        """Plays a song."""
        await interaction.response.defer() # Defer interaction
        
        player = self.get_player(interaction)

        if interaction.guild.voice_client is None:
            if interaction.user.voice:
                await interaction.user.voice.channel.connect()
            else:
                await interaction.followup.send("You are not connected to a voice channel.")
                return

        # If not a URL, treat as a search query
        if not search.startswith(('http://', 'https://')):
            search = f'ytsearch:{search}'

        # Notify user we are working on it
        await interaction.followup.send(f"Searching and downloading metadata for `{search.replace('ytsearch:', '')}`...")

        try:
            # Extract info immediately
            data = await YTDLSource.get_info(search, loop=self.bot.loop, stream=True)
            
            # Check duration before queueing
            duration = data.get('duration')
            if duration and duration > 600:
                raise ValueError(f"❌ **Song Too Long**: This video is {int(duration//60)}m {int(duration%60)}s, but the limit is 10 minutes. Please choose a shorter song.")
            
            # Add requester info
            data['requested_by'] = interaction.user.name
            
            await player.queue.put(data)
            
            # Update with the result (Embed)
            embed = discord.Embed(title="Queued", description=f"[{data['title']}]({data['webpage_url']})", color=discord.Color.green())
            if data.get('thumbnail'):
                embed.set_thumbnail(url=data['thumbnail'])
            if data.get('duration'):
                embed.add_field(name="Duration", value=f"{int(data['duration']//60)}:{int(data['duration']%60):02d}")
            
            await interaction.edit_original_response(content=None, embed=embed)
            
        except ValueError as e:
             await interaction.edit_original_response(content=f"{e}")
        except Exception as e:
            await interaction.edit_original_response(content=f"Error finding song: {e}")

    @app_commands.command(name="pause", description="Pauses the song")
    async def pause(self, interaction: discord.Interaction):
        """Pauses the currently played song."""
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            return await interaction.response.send_message('I am not currently playing anything!', ephemeral=True)
        elif vc.is_paused():
            return await interaction.response.send_message('Already paused.', ephemeral=True)

        vc.pause()
        await interaction.response.send_message(f'**`{interaction.user}`**: Paused the song!')

    @app_commands.command(name="resume", description="Resumes the song")
    async def resume(self, interaction: discord.Interaction):
        """Resumes the currently played song."""
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message('I am not currently playing anything!', ephemeral=True)
        elif not vc.is_paused():
            return await interaction.response.send_message('Already playing.', ephemeral=True)

        vc.resume()
        await interaction.response.send_message(f'**`{interaction.user}`**: Resumed the song!')

    @app_commands.command(name="skip", description="Skips the song")
    async def skip(self, interaction: discord.Interaction):
        """Skip the song."""
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message('I am not currently playing anything!', ephemeral=True)

        if vc.is_paused():
            pass
        elif not vc.is_playing():
            return

        vc.stop()
        await interaction.response.send_message(f'**`{interaction.user}`**: Skipped the song!')

    @app_commands.command(name="stop", description="Stops the song and clears the queue")
    async def stop(self, interaction: discord.Interaction):
        """Stops playing song and clears the queue."""
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message('I am not currently playing anything!', ephemeral=True)

        await self.cleanup(interaction.guild)
        await interaction.response.send_message(f'**`{interaction.user}`**: Stopped and disconnected!')

    @app_commands.command(name="queue", description="Shows the queue")
    async def queue_info(self, interaction: discord.Interaction):
        """Retrieve a basic queue of upcoming songs."""
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message('I am not currently connected to voice!', ephemeral=True)

        player = self.get_player(interaction)
        if player.queue.empty():
            return await interaction.response.send_message('There are currently no more queued songs.')

        upcoming = list(player.queue._queue)
        fmt = '\n'.join(f'**{i + 1}.** {str(song)}' for i, song in enumerate(upcoming))
        embed = discord.Embed(title=f'Upcoming - Next {len(upcoming)}', description=fmt)
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    if not os.path.exists('songs'):
        os.makedirs('songs')
    await bot.add_cog(Music(bot))
