import time
import logging

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

class Utility(commands.Cog):
    """Utility commands"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.start_time = time.time()

    @app_commands.command(name="ping", description="Check bot latency")
    async def ping(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            return

        latency_ms = round(self.bot.latency * 1000) if hasattr(self.bot, "latency") else 0
        embed = discord.Embed(
            title="🏓 Pong!", 
            description=f"Latency: {latency_ms} ms", 
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="uptime", description="Show bot uptime")
    async def uptime(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            return

        uptime_s = time.time() - self.start_time
        hours = int(uptime_s // 3600)
        minutes = int((uptime_s % 3600) // 60)
        seconds = int(uptime_s % 60)
        
        embed = discord.Embed(
            title="⏱️ Bot Uptime",
            description=f"{hours}h {minutes}m {seconds}s",
            color=discord.Color.blue()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
