# cogs/youtube_commands.py
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

class YouTubeCommands(commands.Cog):
    """Commands to control YouTube monitoring: start / stop / status"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="start", description="Start monitoring a YouTube live video")
    @app_commands.describe(video_id="YouTube video id (from the watch URL)")
    async def start_monitoring(self, interaction: discord.Interaction, video_id: str):
        """Start monitoring YouTube chat (safe, non-blocking)"""
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(thinking=True)
        except Exception:
            logger.exception("Failed to defer in /start; continuing")

        if getattr(self.bot, "chat_monitor", None) is None or getattr(self.bot, "youtube_service", None) is None:
            await interaction.followup.send("❌ Chat monitor or YouTube service not configured on the bot.", ephemeral=True)
            return

        if self.bot.chat_monitor.is_running:
            await interaction.followup.send("⚠️ Bot is already running. Use /stop to stop first.", ephemeral=True)
            return

        # Authenticate YouTube (blocking -> run in thread)
        ok = await asyncio.to_thread(self.bot.youtube_service.authenticate)
        if not ok:
            await interaction.followup.send("❌ YouTube authentication failed. Check credentials / TOKEN_JSON / CLIENT_SECRET_JSON.", ephemeral=True)
            return

        # Get live chat id in thread
        live_chat_id = await asyncio.to_thread(self.bot.youtube_service.get_live_chat_id, video_id)
        if not live_chat_id:
            await interaction.followup.send(f"❌ Could not find an active live chat for video id `{video_id}`. Is the stream live?", ephemeral=True)
            return

        # Start monitoring
        self.bot.chat_monitor.start_monitoring(live_chat_id, video_id)

        embed = discord.Embed(title="🚀 Monitoring Started", color=discord.Color.green())
        embed.add_field(name="Video ID", value=f"`{video_id}`", inline=True)
        embed.add_field(name="Chat ID", value=f"`{live_chat_id}`", inline=True)
        embed.add_field(name="Status", value="🟢 Monitoring", inline=True)

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="stop", description="Stop monitoring YouTube chat")
    async def stop_monitoring(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        if getattr(self.bot, "chat_monitor", None) is None:
            await interaction.followup.send("❌ Chat monitor not configured.", ephemeral=True)
            return

        if not self.bot.chat_monitor.is_running:
            await interaction.followup.send("⚠️ Bot is not running.", ephemeral=True)
            return

        self.bot.chat_monitor.stop_monitoring()
        await interaction.followup.send("🛑 Monitoring stopped.", ephemeral=True)

    @app_commands.command(name="yt_status", description="Get basic YouTube monitoring status")
    async def yt_status(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        cm = getattr(self.bot, "chat_monitor", None)
        if not cm:
            await interaction.followup.send("❌ Chat monitor missing.", ephemeral=True)
            return

        st = cm.get_status()
        embed = discord.Embed(title="YouTube Monitor Status", color=discord.Color.blue())
        embed.add_field(name="Running", value=str(st.get("is_running")), inline=True)
        embed.add_field(name="Video ID", value=st.get("video_id") or "N/A", inline=True)
        embed.add_field(name="Processed messages", value=str(st.get("processed_count")), inline=True)
        embed.add_field(name="AI cooldown remaining", value=f"{st.get('ai_cooldown_remaining'):.1f}s", inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(YouTubeCommands(bot))
