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

    @app_commands.command(name="help", description="Show all available commands and features")
    async def help_command(self, interaction: discord.Interaction):
        """Display an overview of all available slash and chat commands."""
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        embed = discord.Embed(
            title="🗡️ Rukiya Bot — Commands & Guide",
            description=(
                "I am **Kuchiki Rukiya**. You can call me by name (`rukiya ...`) or tag me (`@Rukiya ...`) "
                "in chat anytime, or use the slash commands below!\n"
            ),
            color=discord.Color.dark_red()
        )

        embed.add_field(
            name="🗡️ AI & Chat",
            value=(
                "`/ask <question> [post_to_yt]` — Ask Rukiya a question\n"
                "`@Rukiya <message>` — Chat directly by tagging me\n"
                "`rukiya <message>` — Talk to me by calling my name\n"
                "`/rukiya_info` — View persona details & active model"
            ),
            inline=False
        )

        embed.add_field(
            name="🔴 YouTube Live Chat",
            value=(
                "`/start <video_id>` — Start monitoring YouTube live chat\n"
                "`/stop` — Stop monitoring YouTube chat\n"
                "`/yt_status` — View YouTube chat monitor status\n"
                "`/say <text>` — Send a message to YouTube live chat\n"
                "`/auto_reply <action>` — Enable/disable/check YouTube auto-responder"
            ),
            inline=False
        )

        embed.add_field(
            name="📜 Fun & Community",
            value=(
                "`/shayari_send [index]` — Send a poetic shayari line\n"
                "`/shayari_list` — List all available shayaris\n"
                "`/welcome_send [text]` — Send a welcome message"
            ),
            inline=False
        )

        embed.add_field(
            name="⚙️ Utilities & Diagnostics",
            value=(
                "`/ping` — Check bot latency\n"
                "`/uptime` — View how long the bot has been running\n"
                "`/bot_status` — Full system & service diagnostic dashboard\n"
                "`/test_ai <msg>` — Direct raw test of OpenRouter API\n"
                "`/test_trigger <msg>` — Dry-run trigger check on a message"
            ),
            inline=False
        )

        embed.set_footer(text="Bleach Soul Reaper • Kuchiki Rukiya")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
