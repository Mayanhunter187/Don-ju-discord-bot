import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import signal
import socket
from dotenv import load_dotenv
import shutil
import subprocess

from kube import KubeClient
from leader import LeaderElector
from state import StateStore

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
LEASE_NAME = os.getenv('LEASE_NAME', 'don-ju-leader')
STATE_CONFIGMAP = os.getenv('STATE_CONFIGMAP', 'don-ju-state')
# Touched every few seconds; the pod's probes restart it if this goes stale
HEARTBEAT_FILE = '/tmp/heartbeat'

# Debug: Check environment and node availability
os.environ['PATH'] = os.environ.get('PATH', '') + ':/usr/bin:/usr/local/bin'
print(f"DEBUG: PATH={os.environ.get('PATH')}", flush=True)
print(f"DEBUG: node path={shutil.which('node')}", flush=True)
try:
    node_version = subprocess.check_output(['node', '-v'], stderr=subprocess.STDOUT).decode().strip()
    print(f"DEBUG: node version={node_version}", flush=True)
except Exception as e:
    print(f"DEBUG: node execution failed: {e}", flush=True)

class MusicBot(commands.Bot):
    def __init__(self, state_store):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)
        self.state_store = state_store

    async def setup_hook(self):
        # Load extensions
        print(f"Current working directory: {os.getcwd()}", flush=True)
        if os.path.exists('./cogs'):
            print(f"Contents of ./cogs: {os.listdir('./cogs')}", flush=True)
            for filename in os.listdir('./cogs'):
                if filename.endswith('.py'):
                    try:
                        await self.load_extension(f'cogs.{filename[:-3]}')
                        print(f'Loaded extension: cogs.{filename[:-3]}', flush=True)
                    except Exception as e:
                        print(f'Failed to load extension cogs.{filename[:-3]}: {e}', flush=True)
        else:
            print("Error: ./cogs directory not found!", flush=True)

        # Sync commands globally ONLY
        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} command(s) globally", flush=True)
        except Exception as e:
            print(f"Failed to sync commands: {e}", flush=True)

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})', flush=True)
        print('------', flush=True)

kube = KubeClient.from_cluster()
bot = MusicBot(StateStore(kube, STATE_CONFIGMAP))

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

    elector = None
    try:
        if kube:
            elector = LeaderElector(kube, LEASE_NAME, socket.gethostname())
            print(f"Standby: waiting for lease {LEASE_NAME}", flush=True)
            acquire = asyncio.create_task(elector.acquire())
            stopped = asyncio.create_task(stop.wait())
            await asyncio.wait({acquire, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if stop.is_set():
                acquire.cancel()
                return
            stopped.cancel()
            print("Became leader, connecting to Discord", flush=True)

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
        if elector:
            await elector.release()
        if kube:
            await kube.close()
        heartbeat_task.cancel()

if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN not found in .env file.", flush=True)
    else:
        asyncio.run(main())
