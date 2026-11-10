from services.discord_ui import defer_response, send_response, StatusView
# cogs/youtube_commands.py
import asyncio
from services.chat_monitor import ChatMonitor
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands
from services.access import operator_only, operator_callback
from services.youtube_access import YouTubeStartRoles, can_start_youtube, youtube_start_only, youtube_start_callback

logger = logging.getLogger(__name__)

class YouTubeCommands(commands.Cog):
    """Commands to control YouTube monitoring: start / stop / status"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._control_lock = asyncio.Lock()
        self.start_roles = YouTubeStartRoles(bot.memory_service)

    @app_commands.command(name="start", description="Start Rukiya in YouTube live chat (operator or approved server role)")
    @app_commands.describe(video_id="YouTube video ID: the 11-character value after watch?v=")
    @youtube_start_only()
    @youtube_start_callback
    async def start_monitoring(self, interaction: discord.Interaction, video_id: str):
        video_id = video_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            await send_response(interaction, "Use the 11-character YouTube video ID, for example `dQw4w9WgXcQ` from `youtube.com/watch?v=dQw4w9WgXcQ`.", ephemeral=True)
            return
        if self._control_lock.locked():
            await send_response(interaction, "A YouTube start/stop request is already in progress. Try again after it finishes.", ephemeral=True)
            return
        async with self._control_lock:
            if getattr(self.bot, "chat_monitor", None) is None or getattr(self.bot, "youtube_service", None) is None:
                await send_response(interaction, "❌ Chat monitor or YouTube service is not configured.", ephemeral=True)
                return
            if self.bot.chat_monitor.is_running:
                await send_response(interaction, "⚠️ Rukiya is already running on a stream. An operator must use /stop before another stream can start.", ephemeral=True)
                return
            ok = await self._youtube_worker(self.bot.youtube_service.authenticate)
            if not ok:
                await send_response(interaction, "❌ YouTube authentication failed. Ask an operator to check the bot's YouTube credentials.", ephemeral=True)
                return
            live_chat_id = await self._youtube_worker(self.bot.youtube_service.get_live_chat_id, video_id)
            if not live_chat_id:
                await send_response(interaction, "❌ No active live chat was found for that video ID. The stream must be live with chat enabled.", ephemeral=True)
                return
            # Role membership and persisted grants may change during OAuth/API work.
            if not await can_start_youtube(self.bot, interaction, refresh_member=True):
                await send_response(interaction, "Your YouTube start access could not be confirmed. The request was not started; check your role with an operator.", ephemeral=True)
                return
            cm = self.bot.chat_monitor
            started = await cm.start_async(live_chat_id, video_id) if isinstance(cm, ChatMonitor) else cm.start_monitoring(live_chat_id, video_id)
            if not started:
                await send_response(interaction, "❌ The previous monitor is still stopping. Try /start again in a few seconds.", ephemeral=True)
                return
            embed = discord.Embed(title="🚀 Monitoring Started", color=discord.Color.green())
            embed.add_field(name="Video ID", value=f"`{video_id}`", inline=True)
            embed.add_field(name="Chat ID", value=f"`{live_chat_id}`", inline=True)
            embed.add_field(name="Status", value="🟢 Monitoring", inline=True)
            await send_response(interaction, embed=embed, ephemeral=True)

    @staticmethod
    async def _youtube_worker(function, *args):
        # Keep the control lock until a started SDK worker actually finishes,
        # including repeated cancellation; the shared authenticated client is not
        # safe to concurrently rebuild from another /start.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if task.done():
                    break
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError
                raise
        if cancelled:
            if not task.cancelled():
                task.exception()
            raise asyncio.CancelledError
        return result

    youtube_roles = app_commands.Group(name="youtube_roles", description="Owner/operators: choose roles allowed to start Rukiya on YouTube")

    @youtube_roles.command(name="add", description="Allow a role in this server to use /start with a YouTube video ID")
    @app_commands.guild_only()
    @operator_only()
    @operator_callback
    async def add_start_role(self, interaction: discord.Interaction, role: discord.Role):
        await defer_response(interaction, ephemeral=True, thinking=True)
        if interaction.guild_id is None or role.guild.id != interaction.guild_id:
            await send_response(interaction, "Choose a role from this server.", ephemeral=True)
            return
        await self.start_roles.set_role(interaction.guild_id, role.id, True)
        await send_response(interaction, f"Role `{role.id}` can now use /start in this server. Other YouTube/operator commands keep their existing permissions.", ephemeral=True)

    @youtube_roles.command(name="remove", description="Remove a role's permission to start Rukiya on YouTube")
    @app_commands.guild_only()
    @operator_only()
    @operator_callback
    async def remove_start_role(self, interaction: discord.Interaction, role: discord.Role):
        await defer_response(interaction, ephemeral=True, thinking=True)
        if interaction.guild_id is None or role.guild.id != interaction.guild_id:
            await send_response(interaction, "Choose a role from this server.", ephemeral=True)
            return
        await self.start_roles.set_role(interaction.guild_id, role.id, False)
        await send_response(interaction, f"Role `{role.id}` can no longer grant /start access in this server.", ephemeral=True)

    @youtube_roles.command(name="list", description="Show the roles allowed to use /start in this server")
    @app_commands.guild_only()
    @operator_only()
    @operator_callback
    async def list_start_roles(self, interaction: discord.Interaction):
        await defer_response(interaction, ephemeral=True, thinking=True)
        if interaction.guild_id is None:
            await send_response(interaction, "Use this command in a Discord server.", ephemeral=True)
            return
        roles = await self.start_roles.roles(interaction.guild_id)
        lines = [f"<@&{role_id}> (`{role_id}`)" for role_id in sorted(roles)]
        text = "Roles allowed to use /start in this server:\n" + "\n".join(lines) if lines else "No roles configured. The bot owner and operators can still use /start."
        if len(text) <= 1900:
            await send_response(interaction, text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        else:
            import io
            await send_response(interaction, "Configured YouTube start roles:", file=discord.File(io.BytesIO(text.encode()), filename="youtube-start-roles.txt"), ephemeral=True)

    @app_commands.command(name="stop", description="Stop monitoring YouTube chat")
    @operator_only()
    @operator_callback
    async def stop_monitoring(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await defer_response(interaction, ephemeral=True)
        except Exception:
            pass

        if self._control_lock.locked():
            await send_response(interaction, "A YouTube start/stop request is in progress. Try /stop again after it finishes.", ephemeral=True)
            return
        async with self._control_lock:
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
