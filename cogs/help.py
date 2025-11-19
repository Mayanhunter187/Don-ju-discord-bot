import discord
from discord import app_commands
from discord.ext import commands

class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.commands_info = {
            "play": {
                "description": "Plays a song from YouTube.",
                "usage": "/play <search>",
                "example": "/play search: lofi hip hop"
            },
            "pause": {
                "description": "Pauses the currently playing song.",
                "usage": "/pause",
                "example": "/pause"
            },
            "resume": {
                "description": "Resumes playback if paused.",
                "usage": "/resume",
                "example": "/resume"
            },
            "skip": {
                "description": "Skips the current song.",
                "usage": "/skip",
                "example": "/skip"
            },
            "stop": {
                "description": "Stops playback and clears the queue.",
                "usage": "/stop",
                "example": "/stop"
            },
            "queue": {
                "description": "Shows the upcoming songs in the queue.",
                "usage": "/queue",
                "example": "/queue"
            },
            "help": {
                "description": "Shows this help message.",
                "usage": "/help [command]",
                "example": "/help play"
            }
        }

    async def help_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        choices = [
            app_commands.Choice(name=cmd, value=cmd)
            for cmd in self.commands_info.keys()
            if current.lower() in cmd.lower()
        ]
        return choices[:25]

    @app_commands.command(name="help", description="Shows help for commands")
    @app_commands.describe(command="The command to get help for")
    @app_commands.autocomplete(command=help_autocomplete)
    async def help(self, interaction: discord.Interaction, command: str = None):
        """Shows help for commands."""
        if command:
            info = self.commands_info.get(command.lower())
            if info:
                embed = discord.Embed(title=f"Help: /{command}", color=discord.Color.blue())
                embed.add_field(name="Description", value=info['description'], inline=False)
                embed.add_field(name="Usage", value=f"`{info['usage']}`", inline=False)
                embed.add_field(name="Example", value=f"`{info['example']}`", inline=False)
                await interaction.response.send_message(embed=embed)
            else:
                await interaction.response.send_message(f"Command `{command}` not found.", ephemeral=True)
        else:
            embed = discord.Embed(title="🤖 Bot Commands", description="Here are the available commands:", color=discord.Color.blue())
            for cmd, info in self.commands_info.items():
                embed.add_field(name=f"/{cmd}", value=info['description'], inline=False)
            embed.set_footer(text="Type /help <command> for more details.")
            await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(Help(bot))
