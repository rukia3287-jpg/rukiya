from services.discord_ui import defer_response, send_response, StatusView
# cogs/youtube_commands.py
import asyncio
from services.chat_monitor import ChatMonitor
import logging

import discord
from discord import app_commands
from discord.ext import commands
from services.access import operator_only, operator_callback

logger = logging.getLogger(__name__)

class YouTubeCommands(commands.Cog):
    """Commands to control YouTube monitoring: start / stop / status"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="start", description="Start monitoring a YouTube live video")
    @app_commands.describe(video_id="YouTube video id (from the watch URL)")
    @operator_only()
    @operator_callback
    async def start_monitoring(self, interaction: discord.Interaction, video_id: str):
        """Start monitoring YouTube chat (safe, non-blocking)"""
        try:
            if not interaction.response.is_done():
                await defer_response(interaction, thinking=True)
        except Exception:
            logger.exception("Failed to defer in /start; continuing")

        if getattr(self.bot, "chat_monitor", None) is None or getattr(self.bot, "youtube_service", None) is None:
            await send_response(interaction, "❌ Chat monitor or YouTube service not configured on the bot.", ephemeral=True)
            return

        if self.bot.chat_monitor.is_running:
            await send_response(interaction, "⚠️ Bot is already running. Use /stop to stop first.", ephemeral=True)
            return

        # Authenticate YouTube (blocking -> run in thread)
        ok = await asyncio.to_thread(self.bot.youtube_service.authenticate)
        if not ok:
            await send_response(interaction, "❌ YouTube authentication failed. Check credentials / TOKEN_JSON / CLIENT_SECRET_JSON.", ephemeral=True)
            return

        # Get live chat id in thread
        live_chat_id = await asyncio.to_thread(self.bot.youtube_service.get_live_chat_id, video_id)
        if not live_chat_id:
            await send_response(interaction, f"❌ Could not find an active live chat for video id `{video_id}`. Is the stream live?", ephemeral=True)
            return

        # Start monitoring
        cm = self.bot.chat_monitor
        started = await cm.start_async(live_chat_id, video_id) if isinstance(cm, ChatMonitor) else cm.start_monitoring(live_chat_id, video_id)
        if not started:
            await send_response(interaction,
                "❌ Could not start the YouTube monitor because another monitor task is still stopping. Try /start again in a few seconds.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(title="🚀 Monitoring Started", color=discord.Color.green())
        embed.add_field(name="Video ID", value=f"`{video_id}`", inline=True)
        embed.add_field(name="Chat ID", value=f"`{live_chat_id}`", inline=True)
        embed.add_field(name="Status", value="🟢 Monitoring", inline=True)

        await send_response(interaction, embed=embed)

    @app_commands.command(name="stop", description="Stop monitoring YouTube chat")
    @operator_only()
    @operator_callback
    async def stop_monitoring(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await defer_response(interaction, ephemeral=True)
        except Exception:
            pass

        if getattr(self.bot, "chat_monitor", None) is None:
            await send_response(interaction, "❌ Chat monitor not configured.", ephemeral=True)
            return

        if not self.bot.chat_monitor.is_running:
            await send_response(interaction, "⚠️ Bot is not running.", ephemeral=True)
            return

        cm = self.bot.chat_monitor
        if isinstance(cm, ChatMonitor):
            await cm.stop_async()
        else:
            cm.stop_monitoring()
        await send_response(interaction, "🛑 Monitoring stopped.", ephemeral=True)

    @app_commands.command(name="yt_status", description="Get basic YouTube monitoring status")
    @operator_only()
    @operator_callback
    async def yt_status(self, interaction: discord.Interaction):
        try:
            await defer_response(interaction, ephemeral=True)
        except Exception:
            pass

        cm = getattr(self.bot, "chat_monitor", None)
        if not cm:
            await send_response(interaction, "❌ Chat monitor missing.", ephemeral=True)
            return

        st = cm.get_status()
        embed = discord.Embed(title="YouTube Monitor Status", color=discord.Color.blue())
        embed.add_field(name="Running", value=str(st.get("is_running")), inline=True)
        embed.add_field(name="Video ID", value=st.get("video_id") or "N/A", inline=True)
        embed.add_field(name="Processed messages", value=str(st.get("processed_count")), inline=True)
        embed.add_field(name="AI cooldown remaining", value=f"{st.get('ai_cooldown_remaining'):.1f}s", inline=True)
        if st.get("last_error"):
            age = st.get("last_error_age_s")
            when = f" ({age:.0f}s ago)" if age is not None else ""
            embed.add_field(name="Last YouTube API error", value=f"`{st['last_error']}`{when}", inline=False)

        view = StatusView(interaction.user.id, lambda event: self.yt_status.callback(self, event))
        await send_response(interaction, embed=embed, view=view, ephemeral=True)
        await view.bind(interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(YouTubeCommands(bot))
