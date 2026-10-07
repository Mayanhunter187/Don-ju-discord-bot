import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import logging
import math
import os
import signal
import socket
from dotenv import load_dotenv
from prometheus_client import start_http_server

import metrics
from kube import KubeClient
from leader import LeaderElector
from state import StateStore

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
LEASE_NAME = os.getenv('LEASE_NAME', 'don-ju-leader')
STATE_CONFIGMAP = os.getenv('STATE_CONFIGMAP', 'don-ju-state')
METRICS_PORT = int(os.getenv('METRICS_PORT', 8000))
# Touched every few seconds; the pod's probes restart it if this goes stale
HEARTBEAT_FILE = '/tmp/heartbeat'

logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO').upper(),
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
log = logging.getLogger('don-ju')

class MusicBot(commands.Bot):
    def __init__(self, state_store):
        # Slash commands only, so no message content intent and no text prefix
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.state_store = state_store

    async def setup_hook(self):
        # Load extensions
        if os.path.exists('./cogs'):
            for filename in os.listdir('./cogs'):
                if filename.endswith('.py'):
                    try:
                        await self.load_extension(f'cogs.{filename[:-3]}')
                        log.info(f'Loaded extension: cogs.{filename[:-3]}')
                    except Exception as e:
                        log.error(f'Failed to load extension cogs.{filename[:-3]}: {e}')
        else:
            log.error("Error: ./cogs directory not found!")
        # Sync commands globally ONLY
        try:
            synced = await self.tree.sync()
            log.info(f"Synced {len(synced)} command(s) globally")
        except Exception as e:
            log.error(f"Failed to sync commands: {e}")
    async def on_ready(self):
        log.info(f'Logged in as {self.user} (ID: {self.user.id})')

    async def on_app_command_completion(self, interaction, command):
        metrics.COMMANDS.labels(command.qualified_name).inc()

kube = KubeClient.from_cluster()
bot = MusicBot(StateStore(kube, STATE_CONFIGMAP))

@bot.tree.error
async def on_app_command_error(interaction, error):
    name = interaction.command.qualified_name if interaction.command else 'unknown'
    metrics.COMMAND_ERRORS.labels(name).inc()
    log.error(f"/{name} failed", exc_info=error)

@bot.tree.command(name="sync", description="Clear and resync commands (Admin only)")
@app_commands.default_permissions(administrator=True)
async def sync_command(interaction: discord.Interaction):
    """Clear guild commands and wait for global commands to propagate."""
    await interaction.response.defer(ephemeral=True)
    
    # Clear guild-specific commands to remove duplicates
    interaction.client.tree.clear_commands(guild=interaction.guild)
    await interaction.client.tree.sync(guild=interaction.guild)
    
    await interaction.followup.send(
        "✅ Cleared guild commands. Global commands will appear in ~1 hour.\n"
        "**Tip:** Restart Discord to see them immediately.",
        ephemeral=True
    )

async def heartbeat():
    while True:
        with open(HEARTBEAT_FILE, 'w'):
            pass
        await asyncio.sleep(5)

async def main():
    """Run the bot. In the cluster, only the replica holding the lease connects
    to Discord; the others wait as hot standbys."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    heartbeat_task = asyncio.create_task(heartbeat())
    start_http_server(METRICS_PORT)
    metrics.DISCORD_LATENCY.set_function(lambda: bot.latency if math.isfinite(bot.latency) else 0)

    elector = None
    try:
        if kube:
            elector = LeaderElector(kube, LEASE_NAME, socket.gethostname())
            log.info(f"Standby: waiting for lease {LEASE_NAME}")
            acquire = asyncio.create_task(elector.acquire())
            stopped = asyncio.create_task(stop.wait())
            await asyncio.wait({acquire, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if stop.is_set():
                acquire.cancel()
                return
            stopped.cancel()
            log.info("Became leader, connecting to Discord")
        metrics.LEADER.set(1)
        async with bot:
            running = {asyncio.create_task(bot.start(TOKEN)), asyncio.create_task(stop.wait())}
            if elector:
                running.add(asyncio.create_task(elector.hold()))
            done, pending = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)

            # Shutting down, lost the lease, or the bot stopped: hand over cleanly
            music = bot.get_cog('Music')
            if music:
                await music.shutdown()
            for task in pending:
                task.cancel()
            await bot.close()
            for task in done:
                if not task.cancelled() and task.exception():
                    raise task.exception()
    finally:
        metrics.LEADER.set(0)
        if elector:
            await elector.release()
        if kube:
            await kube.close()
        heartbeat_task.cancel()

if __name__ == "__main__":
    if not TOKEN:
        log.error("Error: DISCORD_TOKEN not found in .env file.")
    else:
        asyncio.run(main())
