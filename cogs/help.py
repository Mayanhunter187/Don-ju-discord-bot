import discord
from discord import app_commands
from discord.ext import commands

class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="Shows help for commands")
    @app_commands.describe(command="Optional: Get help for a specific command")
    async def help(self, interaction: discord.Interaction, command: str = None):
        """Shows help information."""
        
        if command:
            # Show specific command help
            cmd = self.bot.tree.get_command(command)
            if cmd:
                embed = discord.Embed(
                    title=f"📖 Help: /{cmd.name}",
                    description=cmd.description or "No description available.",
                    color=discord.Color.blue()
                )
                
                if hasattr(cmd, 'parameters') and cmd.parameters:
                    params_text = ""
                    for param in cmd.parameters:
                        params_text += f"**{param.name}**: {param.description or 'No description'}\n"
                    embed.add_field(name="Parameters", value=params_text, inline=False)
                
                await interaction.response.send_message(embed=embed)
            else:
                await interaction.response.send_message(f"Command `{command}` not found.", ephemeral=True)
        else:
            # Show all commands organized by category
            embed = discord.Embed(
                title="🎵 Don'Ju Music Bot - Command Guide",
                description="Here are all available commands, organized by category:",
                color=discord.Color.gold()
            )
            
            # Playback Controls
            playback_cmds = [
                ("play", "Play a song from YouTube (URL or search)"),
                ("pause", "Pause the current song"),
                ("resume", "Resume playback"),
                ("skip", "Skip to the next song"),
                ("stop", "Stop playback and clear the queue")
            ]
            playback_text = "\n".join([f"`/{cmd}` - {desc}" for cmd, desc in playback_cmds])
            embed.add_field(name="🎮 Playback Controls", value=playback_text, inline=False)
            
            # Queue Management
            queue_cmds = [
                ("queue", "View the current song queue"),
                ("cache", "View cache statistics and recent downloads")
            ]
            queue_text = "\n".join([f"`/{cmd}` - {desc}" for cmd, desc in queue_cmds])
            embed.add_field(name="📋 Queue & Cache", value=queue_text, inline=False)
            
            # Utility
            utility_cmds = [
                ("sync", "Sync commands to the server (Admin only)"),
                ("help", "Show this help message")
            ]
            utility_text = "\n".join([f"`/{cmd}` - {desc}" for cmd, desc in utility_cmds])
            embed.add_field(name="🛠️ Utility", value=utility_text, inline=False)
            
            embed.set_footer(text="Use /help <command> for detailed information about a specific command")
            
            await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(Help(bot))
