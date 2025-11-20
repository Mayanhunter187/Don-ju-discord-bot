import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import shutil
import subprocess

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# Debug: Check environment and node availability
# Explicitly add common paths to PATH to ensure node is found
os.environ['PATH'] = os.environ.get('PATH', '') + ':/usr/bin:/usr/local/bin'
print(f"DEBUG: PATH={os.environ.get('PATH')}", flush=True)
print(f"DEBUG: node path={shutil.which('node')}", flush=True)
try:
    node_version = subprocess.check_output(['node', '-v'], stderr=subprocess.STDOUT).decode().strip()
    print(f"DEBUG: node version={node_version}", flush=True)
except Exception as e:
    print(f"DEBUG: node execution failed: {e}", flush=True)

class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)

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

        # Sync commands globally
        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} command(s) globally", flush=True)
        except Exception as e:
            print(f"Failed to sync commands: {e}", flush=True)

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})', flush=True)
        print('------', flush=True)

    async def on_message(self, message):
        # Allow !sync command to work
        if message.content == "!sync" and message.author.id == 184405311681986560: # Optional: Restrict to owner if needed, or just allow for testing
             await self.tree.sync(guild=message.guild)
             await message.channel.send(f"✅ Synced commands to this guild!")
             return
        
        await self.process_commands(message)

bot = MusicBot()

@bot.command()
async def sync(ctx):
    """Syncs commands to the current guild for instant updates."""
    fmt = await ctx.bot.tree.sync(guild=ctx.guild)
    await ctx.send(f"Synced {len(fmt)} commands to the current guild.")

if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN not found in .env file.", flush=True)
    else:
        bot.run(TOKEN)
