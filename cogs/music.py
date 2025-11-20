import discord
from discord import app_commands, ui
from discord.ext import commands
import asyncio
import yt_dlp
import os
import json
import re
import random

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
    def __init__(self, bot, guild, channel):
        self.bot = bot
        self.guild = guild
        self.channel = channel
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

            # Save state when song starts
            self.bot.get_cog("Music").save_state()

            try:
                print(f"DEBUG: Playing {source.title}", flush=True)
                
                def after_callback(error):
                    if error:
                        print(f"DEBUG: Player error: {error}", flush=True)
                    print("DEBUG: Song finished/stopped, triggering next...", flush=True)
                    self.bot.loop.call_soon_threadsafe(self.next.set)

                self.guild.voice_client.play(source, after=after_callback)
                
                # Create Embed for Now Playing (Purple, Large Image)
                embed = discord.Embed(title="Now Playing", description=f"[{source.title}]({source.webpage_url})", color=discord.Color.purple())
                if source.thumbnail:
                    embed.set_image(url=source.thumbnail)
                if source.duration:
                    embed.add_field(name="Duration", value=f"{int(source.duration//60)}:{int(source.duration%60):02d}", inline=True)
                
                # Add Cache Status
                if source.is_cached:
                    embed.add_field(name="Source", value="💾 Cached", inline=True)
                else:
                    embed.add_field(name="Source", value="☁️ New", inline=True)
                
                if source.requested_by:
                    embed.add_field(name="Requested By", value=source.requested_by, inline=True)
                
                # Check if this is a resumed playback after bot restart
                if hasattr(self, '_resumed_from_state') and self._resumed_from_state:
                    embed.set_footer(text="🔄 Resumed after bot restart", icon_url=None)
                    self._resumed_from_state = False  # Reset flag
                
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
            # Save state when song ends
            self.bot.get_cog("Music").save_state()

    def destroy(self, guild):
        # Cleanup via the Cog
        cog = self.bot.get_cog("Music")
        if cog:
            return self.bot.loop.create_task(cog.cleanup(guild))

class SearchButton(ui.Button):
    def __init__(self, title, url, is_cached, cog, interaction_user):
        # Button labels can be max 80 chars, truncate smartly
        # Format: "Song Title Here..."
        if len(title) > 77:
            label = title[:74] + "..."
        else:
            label = title
        
        # Use different colors for cached vs new
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

    @ui.button(label="Cancel", style=discord.ButtonStyle.red, emoji="❌")
    async def cancel_button(self, interaction: discord.Interaction, button: ui.Button):
        # Only the requester can cancel
        if interaction.user != self.interaction_user:
            return await interaction.response.send_message("❌ This search menu is not for you!", ephemeral=True)
        
        # Acknowledge the interaction and delete the message
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except discord.NotFound:
            # Message already deleted, that's fine
            pass
        except Exception as e:
            # Fallback: just edit the message
            try:
                cancel_embed = discord.Embed(
                    title="❌ Search Cancelled",
                    description="Search has been cancelled.",
                    color=discord.Color.red()
                )
                await interaction.response.send_message(embed=cancel_embed, ephemeral=True)
            except:
                pass

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players = {}
        self.cleanup_partial_files()
        self.bot.loop.create_task(self.load_state())
    
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

    def save_state(self):
        """Saves the current queue and playing song to a file."""
        state = {}
        for guild_id, player in self.players.items():
            queue_list = []
            # Add current song if playing (so it restarts)
            if player.current:
                 if isinstance(player.current, YTDLSource):
                     queue_list.append(player.current.data)
                 elif isinstance(player.current, dict):
                     queue_list.append(player.current)
            
            # Add rest of queue
            for item in player.queue._queue:
                queue_list.append(item)

            if queue_list:
                # Only save if there's something to play
                voice_channel_id = player.guild.voice_client.channel.id if player.guild.voice_client else None
                if voice_channel_id:
                    state[str(guild_id)] = {
                        'voice_channel': voice_channel_id,
                        'text_channel': player.channel.id,
                        'queue': queue_list
                    }
        
        try:
            with open('songs/state.json', 'w') as f:
                json.dump(state, f)
            print("DEBUG: State saved.", flush=True)
        except Exception as e:
            print(f"Error saving state: {e}", flush=True)

    async def load_state(self):
        """Loads the queue from file on startup."""
        await self.bot.wait_until_ready()
        if not os.path.exists('songs/state.json'):
            return
            
        print("DEBUG: Loading state...", flush=True)
        try:
            with open('songs/state.json', 'r') as f:
                state = json.load(f)
                
            for guild_id_str, data in state.items():
                guild_id = int(guild_id_str)
                guild = self.bot.get_guild(guild_id)
                if not guild:
                    continue
                    
                voice_channel = guild.get_channel(data['voice_channel'])
                text_channel = guild.get_channel(data['text_channel'])
                
                if voice_channel and text_channel:
                    # Connect
                    if not guild.voice_client or not guild.voice_client.is_connected():
                        try:
                            await voice_channel.connect()
                            print(f"DEBUG: Reconnected to voice channel {voice_channel.name}", flush=True)
                        except Exception as e:
                            print(f"Failed to reconnect voice: {e}", flush=True)
                            continue
                    
                    # Get player
                    if guild.id not in self.players:
                         player = MusicPlayer(self.bot, guild, text_channel)
                         self.players[guild.id] = player
                    else:
                        player = self.players[guild.id]

                    # Populate queue
                    for song_data in data['queue']:
                        await player.queue.put(song_data)
                    
                    # Set flag to indicate this is a resumed session
                    player._resumed_from_state = True
                    
                    # Build queue preview (up to 10 songs)
                    queue_preview = ""
                    songs_to_show = min(10, len(data['queue']))
                    for i, song in enumerate(data['queue'][:songs_to_show], 1):
                        title = song.get('title', 'Unknown')
                        # Truncate long titles
                        if len(title) > 50:
                            title = title[:47] + "..."
                        queue_preview += f"`{i}.` {title}\n"
                    
                    if len(data['queue']) > 10:
                        queue_preview += f"\n*...and {len(data['queue']) - 10} more songs*"
                    
                    # Send resume notification
                    resume_embed = discord.Embed(
                        title="🔄 Bot Resumed",
                        description="I'm back! Resuming playback from where we left off...",
                        color=discord.Color.blue()
                    )
                    resume_embed.add_field(name="📋 Queue Status", value=f"**{len(data['queue'])}** song(s) queued", inline=False)
                    
                    if queue_preview:
                        resume_embed.add_field(name="🎵 Up Next", value=queue_preview, inline=False)
                    
                    resume_embed.set_footer(text="▶️ Starting playback now")
                    await text_channel.send(embed=resume_embed)
                    
                    print(f"DEBUG: Restored queue for guild {guild.name}", flush=True)
                        
        except Exception as e:
            print(f"Error loading state: {e}", flush=True)

    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass

        try:
            del self.players[guild.id]
        except KeyError:
            pass
        
        self.save_state()

    def get_player(self, interaction):
        try:
            player = self.players[interaction.guild.id]
        except KeyError:
            player = MusicPlayer(interaction.client, interaction.guild, interaction.channel)
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
            
            # Check if this will play immediately or be queued
            vc = interaction.guild.voice_client
            will_play_immediately = (player.queue.empty() and (not vc or not vc.is_playing()))
            
            await player.queue.put(data)
            
            # Start background download if not cached and not playing immediately
            if not is_cache_hit and not will_play_immediately:
                # Download in background without blocking
                async def background_download():
                    try:
                        print(f"DEBUG: Starting background download for {data.get('title', 'Unknown')}", flush=True)
                        # This will download and cache the file
                        await self.bot.loop.run_in_executor(
                            None,
                            lambda: ytdl.extract_info(data['webpage_url'], download=True)
                        )
                        print(f"DEBUG: Background download complete for {data.get('title', 'Unknown')}", flush=True)
                    except Exception as e:
                        print(f"DEBUG: Background download failed: {e}", flush=True)
                
                # Start download task without awaiting (fire and forget)
                self.bot.loop.create_task(background_download())
            
            # Only show "Queued" message if song won't play immediately
            if not will_play_immediately:
                # Create Embed for Public Queue Log
                embed = discord.Embed(title="Queued", description=f"[{data['title']}]({data['webpage_url']})", color=discord.Color.green())
                if data.get('thumbnail'):
                    embed.set_image(url=data['thumbnail'])
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

            # Close Ephemeral Interaction (Delete it so it vanishes)
            try:
                await interaction.delete_original_response()
            except:
                # Fallback if delete fails (e.g. too old), just edit to empty
                await interaction.edit_original_response(content="✅ Queued", embed=None, view=None)
            
            # Save state
            self.save_state()
            
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
                # Send modern connection message
                connecting_embed = discord.Embed(
                    title="🔗 Establishing Connection",
                    description=f"**Joining:** {interaction.user.voice.channel.name}\n\n🎵 Getting ready to play music...",
                    color=discord.Color.green()
                )
                connecting_embed.set_footer(text="✅ Connected! Ready to play")
                await interaction.followup.send(embed=connecting_embed, ephemeral=True)
                await interaction.user.voice.channel.connect()
            else:
                await interaction.followup.send("❌ You need to be in a voice channel to play music!")
                return

        # If URL, queue directly
        if is_url:
            await self.queue_song(interaction, search)
            return

        # If Search Query, show menu
        search_query = f"ytsearch5:{search}"
        
        # Send modern scanning message with blue theme
        embed = discord.Embed(
            title="🔍 Searching YouTube",
            description=f"**Query:** {search}\n\n🔄 Scanning YouTube's library...",
            color=discord.Color.blue()
        )
        embed.set_footer(text="⚡ This usually takes just a few seconds")
        scan_msg = await interaction.followup.send(embed=embed)

        try:
            data = await self.bot.loop.run_in_executor(
                None, 
                lambda: ytdl.extract_info(search_query, download=False, process=False)
            )
            
            if 'entries' not in data or not data['entries']:
                error_embed = discord.Embed(
                    title="❌ No Results Found",
                    description=f"Couldn't find anything for: **{search}**\n\n💡 Try a different search term!",
                    color=discord.Color.red()
                )
                await scan_msg.edit(embed=error_embed)
                return

            view = SearchView(self, interaction.user)
            
            # Process top 5 results and add buttons (no redundant list)
            entries = list(data['entries'])
            cached_count = 0
            new_count = 0
            
            for i, entry in enumerate(entries[:5], 1):
                title = entry.get('title', 'Unknown Title')
                url = entry.get('url', '')
                video_id = entry.get('id')
                
                # Check cache status
                is_cached = False
                if video_id:
                     if os.path.exists(f'songs/{video_id}.info.json'):
                         is_cached = True
                         cached_count += 1
                     else:
                         new_count += 1
                
                # Add button
                view.add_item(SearchButton(title, url, is_cached, self, interaction.user))

            # Edit the scanning message to show clean bubble list
            results_embed = discord.Embed(
                title="🎵 Select a Track",
                description="Choose from the options below:",
                color=discord.Color.blue()
            )
            
            # Optionally add thumbnail of first result for visual appeal
            if entries and entries[0].get('thumbnail'):
                results_embed.set_thumbnail(url=entries[0]['thumbnail'])
            
            results_embed.set_footer(text="🟢 Cached | 🔵 New")
            await scan_msg.edit(embed=results_embed, view=view)

        except Exception as e:
            error_embed = discord.Embed(
                title="⚠️ Search Error",
                description=f"Something went wrong while searching.\n\n💡 Try again in a moment!",
                color=discord.Color.orange()
            )
            error_embed.add_field(name="🔍 Error Details", value=f"```{str(e)[:200]}```", inline=False)
            await scan_msg.edit(embed=error_embed)
    @app_commands.command(name="skip", description="Skips the song")
    async def skip(self, interaction: discord.Interaction):
        """Skip the song."""
        print(f"DEBUG: Skip requested by {interaction.user}", flush=True)
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message('❌ I\'m not currently playing anything!', ephemeral=True)

        if vc.is_paused():
            pass
        elif not vc.is_playing():
            print("DEBUG: Skip called but not playing", flush=True)
            return await interaction.response.send_message('❌ Nothing is playing right now!', ephemeral=True)

        # Get player and current song info
        player = self.get_player(interaction)
        current_song = player.current
        queue_size = player.queue.qsize()
        
        # Get song details
        if current_song:
            if isinstance(current_song, YTDLSource):
                song_title = current_song.title
                song_url = current_song.webpage_url
                song_thumbnail = current_song.thumbnail
                song_duration = current_song.duration
            else:
                song_title = "Unknown"
                song_url = None
                song_thumbnail = None
                song_duration = None
        else:
            song_title = "Unknown"
            song_url = None
            song_thumbnail = None
            song_duration = None

        print("DEBUG: Calling vc.stop()", flush=True)
        vc.stop()
        self.save_state()
        
        # Send enhanced skip embed with thumbnail and details
        embed = discord.Embed(
            title="⏭️ Song Skipped",
            description=f"**{song_title}**" if not song_url else f"**[{song_title}]({song_url})**",
            color=discord.Color.orange()
        )
        
        # Add thumbnail
        if song_thumbnail:
            embed.set_thumbnail(url=song_thumbnail)
        
        # Add duration if available
        if song_duration:
            duration_str = f"{int(song_duration//60)}:{int(song_duration%60):02d}"
            embed.add_field(name="⏱️ Duration", value=duration_str, inline=True)
        
        embed.add_field(name="👤 Skipped By", value=interaction.user.mention, inline=True)
        embed.add_field(name="📋 Songs in Queue", value=f"{queue_size} remaining", inline=True)
        
        if queue_size > 0:
            embed.set_footer(text="▶️ Playing next song now")
        else:
            embed.set_footer(text="Queue is now empty")
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="stop", description="Stops the song and clears the queue")
    async def stop(self, interaction: discord.Interaction):
        """Stops playing song and clears the queue."""
        vc = interaction.guild.voice_client

        if not vc or not vc.is_connected():
            return await interaction.response.send_message('❌ I\'m not currently connected to a voice channel!', ephemeral=True)

        player = self.get_player(interaction)
        
        # Clear the queue
        while not player.queue.empty():
            try:
                player.queue.get_nowait()
            except:
                break
        
        vc.stop()
        
        # Send styled stop embed (red)
        embed = discord.Embed(
            title="⏹️ Playback Stopped",
            description=f"Stopped by {interaction.user.mention}\nQueue cleared.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="queue", description="Shows the queue")
    async def queue_info(self, interaction: discord.Interaction):
        """Retrieve a basic queue of upcoming songs."""
        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return await interaction.response.send_message('❌ I\'m not currently connected to a voice channel!', ephemeral=True)

        player = self.get_player(interaction)
        if not player.queue._queue:
            empty_embed = discord.Embed(
                title="📭 Queue is Empty",
                description="No songs are currently queued. Use `/play` to add some!",
                color=discord.Color.light_gray()
            )
            return await interaction.response.send_message(embed=empty_embed)

        upcoming = list(player.queue._queue)
        
        # Build formatted queue list
        fmt = ""
        for i, song in enumerate(upcoming):
            # Handle both dict (pre-download) and YTDLSource (legacy)
            if isinstance(song, dict):
                title = song.get('title', 'Unknown Title')
                url = song.get('webpage_url', '')
                duration = song.get('duration', 0)
            else:
                title = song.title
                url = song.webpage_url
                duration = song.duration
            
            # Format duration
            duration_str = f"{int(duration//60)}:{int(duration%60):02d}" if duration else "?"
            
            # Truncate long titles
            display_title = title[:60] + "..." if len(title) > 60 else title
            
            line = f"`{i + 1}.` [{display_title}]({url}) `[{duration_str}]`\n"
            if len(fmt) + len(line) > 3800:  # Leave room for footer
                fmt += f"\n*...and {len(upcoming) - i} more songs*"
                break
            fmt += line

        embed = discord.Embed(
            title=f'📜 Queue ({len(upcoming)} song{"s" if len(upcoming) != 1 else ""})',
            description=fmt or "*Queue is empty*",
            color=discord.Color.blurple()
        )
        
        # Calculate total duration
        total_duration = 0
        for song in upcoming:
            if isinstance(song, dict):
                total_duration += song.get('duration', 0)
            else:
                total_duration += song.duration if hasattr(song, 'duration') else 0
        
        if total_duration > 0:
            total_mins = int(total_duration // 60)
            embed.set_footer(text=f"Total Duration: {total_mins} minute{('s' if total_mins != 1 else '')}")
        
        await interaction.response.send_message(embed=embed)

async def setup(bot):
    if not os.path.exists('songs'):
        os.makedirs('songs')
    await bot.add_cog(Music(bot))
