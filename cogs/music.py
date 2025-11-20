import discord
from discord import app_commands, ui
from discord.ext import commands
import asyncio
import yt_dlp
import os
import json
import json
import re
import random

# YouTube DL options
# YouTube DL options
ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': 'songs/%(id)s.%(ext)s',
    'writeinfojson': True,
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
    def __init__(self, source, *, data, volume=0.5, is_cached=False):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.thumbnail = data.get('thumbnail')
        self.duration = data.get('duration')
        self.uploader = data.get('uploader')
        self.requested_by = data.get('requested_by')
        self.is_cached = is_cached
        self.webpage_url = data.get('webpage_url')

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
    def create_from_data(cls, data, stream=False, is_cached=False):
        # Max length check (10 minutes = 600 seconds)
        duration = data.get('duration')
        if duration and duration > 600:
            raise ValueError(f"❌ **Song Too Long**: This video is {int(duration//60)}m {int(duration%60)}s, but the limit is 10 minutes. Please choose a shorter song.")

        filename = data['url'] if stream else ytdl.prepare_filename(data)
        options = ffmpeg_options_stream if stream else ffmpeg_options_local
        return cls(discord.FFmpegPCMAudio(filename, **options), data=data, is_cached=is_cached)

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
                    is_cached = os.path.exists(filename)
                    
                    if not is_cached:
                        # Cleanup cache if needed
                        self.bot.get_cog("Music").cleanup_cache()
                        
                        # Download
                        await self.bot.loop.run_in_executor(None, lambda: ytdl.extract_info(source['webpage_url'], download=True))
                    
                    # Create source from local file (stream=False)
                    source = YTDLSource.create_from_data(source, stream=False, is_cached=is_cached)
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
            source.volume = self.volume
            self.current = source

            try:
                print(f"DEBUG: Playing {source.title}", flush=True)
                
                def after_callback(error):
                    if error:
                        print(f"DEBUG: Player error: {error}", flush=True)
                    print("DEBUG: Song finished/stopped, triggering next...", flush=True)
                    self.bot.loop.call_soon_threadsafe(self.next.set)

                self.guild.voice_client.play(source, after=after_callback)
                
                # Create Embed for Now Playing
                embed = discord.Embed(title="Now Playing", description=f"[{source.title}]({source.webpage_url})", color=discord.Color.blurple())
                if source.thumbnail:
                    embed.set_thumbnail(url=source.thumbnail)
                if source.duration:
                    embed.add_field(name="Duration", value=f"{int(source.duration//60)}:{int(source.duration%60):02d}")
                if source.requested_by:
                    embed.set_footer(text=f"Requested by {source.requested_by}")
                
                # Add Cache Status
                if source.is_cached:
                    embed.add_field(name="Source", value="💾 Played from Cache", inline=True)
                else:
                    embed.add_field(name="Source", value="☁️ Downloaded from YouTube", inline=True)
                
                self.np = await self.channel.send(embed=embed)
            except Exception as e:
                print(f"DEBUG: Exception in play: {e}", flush=True)
                await self.channel.send(f"Error starting playback: {e}")
                self.next.set() # Ensure we don't get stuck

            await self.next.wait()
            print("DEBUG: Wait finished, cleaning up...", flush=True)

            # Make sure the FFmpeg process is cleaned up.
            try:
                source.cleanup()
            except ValueError:
                print("DEBUG: Source already cleaned up (ValueError ignored)", flush=True)
            except Exception as e:
                print(f"DEBUG: Error cleaning up source: {e}", flush=True)
            
            self.current = None

    def destroy(self, guild):
        # Cleanup via the Cog
        cog = self.bot.get_cog("Music")
        if cog:
            return self.bot.loop.create_task(cog.cleanup(guild))

class SearchButton(ui.Button):
    def __init__(self, title, url, is_cached, cog, interaction_user):
        # Truncate title for button label (max 80 chars, keep it safe at 40)
        label = title[:37] + "..." if len(title) > 37 else title
        style = discord.ButtonStyle.green if is_cached else discord.ButtonStyle.blurple
        emoji = "💾" if is_cached else "☁️"
        
        super().__init__(style=style, label=label, emoji=emoji)
        self.video_url = url
        self.cog = cog
        self.interaction_user = interaction_user

    async def callback(self, interaction: discord.Interaction):
        # Only the requester can click
        if interaction.user != self.interaction_user:
            return await interaction.response.send_message("This search menu is not for you!", ephemeral=True)
        
        # Defer the interaction (acknowledges it)
        await interaction.response.defer()
        
        # Queue the song
        await self.cog.queue_song(interaction, self.video_url)

class SearchView(ui.View):
    def __init__(self, cog, interaction_user):
        super().__init__(timeout=60)
        self.cog = cog
        self.interaction_user = interaction_user

    @ui.button(label="Cancel", style=discord.ButtonStyle.red, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user != self.interaction_user:
            return await interaction.response.send_message("This search menu is not for you!", ephemeral=True)
        
        await interaction.response.edit_message(content="Search cancelled.", view=None, embed=None)

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players = {}
        self.cleanup_partial_files()
    
    def cleanup_cache(self):
        self.cleanup_partial_files()

    def cleanup_partial_files(self):
        """Clean up .part, .ytdl, and .temp files on startup."""
        if not os.path.exists('songs'):
            return
            
        for filename in os.listdir('songs'):
            if filename.endswith(('.part', '.ytdl', '.temp')):
                try:
                    os.remove(os.path.join('songs', filename))
                except Exception as e:
                    print(f"Failed to delete {filename}: {e}")

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


    async def queue_song(self, interaction: discord.Interaction, query: str):
        """Helper to queue a song from URL."""
        # Flavor Messages
        flavor_texts = {
            "download": [
                "⬇️ **Intercepting transmission...** Downloading `{query}`...",
                "📡 **Acquiring signal...** Fetching `{query}`...",
                "👾 **Decoding matrix...** Downloading `{query}`...",
                "⚡ **Charging capacitors...** Getting `{query}` ready..."
            ],
            "cache": [
                "💿 **Dusting off the vinyl...** Found `{query}` in cache!",
                "💾 **Loading from memory banks...** `{query}` is ready!",
                "📼 **Rewinding tape...** `{query}` found locally!",
                "📦 **Unboxing archives...** `{query}` is cached!"
            ]
        }

        # Try to find in cache first (Optimization)
        video_id = None
        cached_data = None
        is_cache_hit = False

        # Extract Video ID
        match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', query)
        if match:
            video_id = match.group(1)
            info_path = f'songs/{video_id}.info.json'
            if os.path.exists(info_path):
                try:
                    with open(info_path, 'r') as f:
                        cached_data = json.load(f)
                    is_cache_hit = True
                except Exception as e:
                    print(f"Failed to load cache for {video_id}: {e}")

        # Determine initial message content
        initial_msg = ""
        if is_cache_hit and cached_data:
            initial_msg = random.choice(flavor_texts["cache"]).format(query=cached_data.get('title', query))
            data = cached_data
        else:
            initial_msg = f"📡 **Establishing Connection...** Accessing `{query}`..."

        # Send/Update status using edit_original_response (works for both deferred commands and button interactions)
        try:
            await interaction.edit_original_response(content=initial_msg, view=None, embed=None)
        except discord.NotFound:
            # Fallback if original response is gone (rare)
            await interaction.followup.send(initial_msg)

        if not (is_cache_hit and cached_data):
            # Fetch info
            try:
                data = await YTDLSource.get_info(query, loop=self.bot.loop, stream=True)
            except Exception as e:
                await interaction.edit_original_response(content=f"Error finding song: {e}")
                return
            
            # Now check if audio file exists (Legacy Cache Check)
            filename = ytdl.prepare_filename(data)
            if os.path.exists(filename):
                is_cache_hit = True
                # Update message to Cache Hit
                new_msg = random.choice(flavor_texts["cache"]).format(query=data.get('title', query))
                await interaction.edit_original_response(content=new_msg)
            else:
                # Update message to Downloading
                new_msg = random.choice(flavor_texts["download"]).format(query=data.get('title', query))
                await interaction.edit_original_response(content=new_msg)

        try:
            player = self.get_player(interaction)
            
            # Check duration before queueing
            duration = data.get('duration')
            if duration and duration > 600:
                raise ValueError(f"❌ **Song Too Long**: This video is {int(duration//60)}m {int(duration%60):02d}s, but the limit is 10 minutes. Please choose a shorter song.")
            
            # Add requester info
            data['requested_by'] = interaction.user.name
            
            await player.queue.put(data)
            
            # Create Embed for Public Queue Log
            embed = discord.Embed(title="Queued", description=f"[{data['title']}]({data['webpage_url']})", color=discord.Color.green())
            if data.get('thumbnail'):
                embed.set_thumbnail(url=data['thumbnail'])
            if data.get('duration'):
                embed.add_field(name="Duration", value=f"{int(data['duration']//60)}:{int(data['duration']%60):02d}")
            
            # Add position info
            queue_pos = player.queue.qsize()
            embed.add_field(name="Position in Queue", value=f"#{queue_pos}", inline=True)
            embed.add_field(name="Requested By", value=interaction.user.mention, inline=True)

            if is_cache_hit:
                embed.set_footer(text="💾 Instant Load (Cached)")
            else:
                embed.set_footer(text="☁️ New Download")

            # Send Public Embed
            await interaction.channel.send(embed=embed)

            # Close Ephemeral Interaction
            await interaction.edit_original_response(content="✅ Request sent to queue!", embed=None, view=None)
            
        except ValueError as e:
             await interaction.edit_original_response(content=f"{e}")
        except Exception as e:
             await interaction.edit_original_response(content=f"An error occurred: {e}")

    @app_commands.command(name="play", description="Plays a song from YouTube")
    @app_commands.describe(search="The YouTube URL or search query")
    async def play(self, interaction: discord.Interaction, search: str):
        """Plays a song."""
        # Determine visibility based on input type
        is_url = search.startswith(('http://', 'https://'))
        
        # Defer immediately so we have time to process
        try:
            # Always make the response private (Ephemeral)
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException as e:
            # If interaction is already acknowledged, we can proceed
            if e.code == 40060:
                pass
            else:
                raise
        
        player = self.get_player(interaction)

        if interaction.guild.voice_client is None:
            if interaction.user.voice:
                await interaction.user.voice.channel.connect()
            else:
                await interaction.followup.send("You are not connected to a voice channel.")
                return

        # If URL, queue directly
        if is_url:
            await self.queue_song(interaction, search)
            return

        # If Search Query, show menu
        search_query = f"ytsearch5:{search}"
        
        # Send initial scanning message
        scan_msg = await interaction.followup.send(f"🔎 **Scanning frequencies...** Searching for `{search}`...")

        try:
            data = await self.bot.loop.run_in_executor(
                None, 
                lambda: ytdl.extract_info(search_query, download=False, process=False)
            )
            
            if 'entries' not in data or not data['entries']:
                await scan_msg.edit(content="No results found.")
                return

            view = SearchView(self, interaction.user)
            
            # Process top 5 results (Fix: Convert to list to avoid islice error)
            entries = list(data['entries'])
            for entry in entries[:5]:
                title = entry.get('title', 'Unknown Title')
                url = entry.get('url', '')
                video_id = entry.get('id') # ytsearch usually provides ID
                
                # Check cache status
                is_cached = False
                if video_id:
                     if os.path.exists(f'songs/{video_id}.info.json'):
                         is_cached = True
                
                # Add button
                view.add_item(SearchButton(title, url, is_cached, self, interaction.user))

            # Edit the scanning message to show the menu
            await scan_msg.edit(content="Select a track:", view=view)

        except Exception as e:
            await scan_msg.edit(content=f"Error searching: {e}")
    @app_commands.command(name="skip", description="Skips the song")
    async def skip(self, interaction: discord.Interaction):
        """Skip the song."""
        print(f"DEBUG: Skip requested by {interaction.user}", flush=True)
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message('I am not currently playing anything!', ephemeral=True)

        if vc.is_paused():
            pass
        elif not vc.is_playing():
            print("DEBUG: Skip called but not playing", flush=True)
            return await interaction.response.send_message('I am not playing anything to skip!', ephemeral=True)

        print("DEBUG: Calling vc.stop()", flush=True)
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
