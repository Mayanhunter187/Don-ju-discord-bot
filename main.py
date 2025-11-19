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

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')
    print('------')
    
    # Load extensions
    await load_extensions()
    
    # Sync commands globally
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s) globally")
    except Exception as e:
        print("Error: DISCORD_TOKEN not found in .env file.")
    else:
        bot.run(TOKEN)
