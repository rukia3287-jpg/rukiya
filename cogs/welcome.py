# cogs/welcome.py
import asyncio
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from services.safety_service import validate_rukiya_response

logger = logging.getLogger(__name__)

class Welcome(commands.Cog):
    """Welcome new Discord members and optionally post welcome to YouTube chat."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai = getattr(bot, "ai_service", None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            guild = member.guild
            if not guild:
                return

            channel = discord.utils.get(guild.text_channels, name="welcome")
            if channel:
                try:
                    await channel.send(f"👋 Welcome {member.mention} — enjoy your stay!")
                except Exception:
                    logger.exception("Failed to send welcome in channel")

            try:
                await member.send(f"Hi {member.display_name}, welcome to {guild.name}!")
            except Exception:
                pass

            cm = getattr(self.bot, "chat_monitor", None)
            if cm and cm.is_running:
                # Display names are untrusted input: keep only plain name characters.
                safe_name = re.sub(r"[^\w .\-]", "", member.display_name)[:32].strip() or "friend"
                welcome_msg = f"Welcome {safe_name}! 🎉"
                if self.ai:
                    try:
                        maybe = await self.ai.generate_response(
                            f"Write a friendly short welcome for {safe_name}",
                            "welcome-bot",
                            bypass_trigger=True,
                            bypass_cooldown=True,
                        )
                        if maybe:
                            welcome_msg = maybe
                    except Exception:
                        logger.exception("AI generation for welcome failed")

                welcome_msg = validate_rukiya_response(welcome_msg, fallback="Welcome to the community! 🎉")
                try:
                    await cm.send_chat_message(welcome_msg, message_kind="welcome")
                except Exception:
                    logger.exception("Failed to send welcome to YouTube chat")

        except Exception:
            logger.exception("on_member_join failure")

    @app_commands.command(name="welcome_send", description="Send a manual welcome message to YouTube chat (or Discord fallback)")
    @app_commands.describe(text="Text to send (if empty, AI or default will be used)")
    async def welcome_send(self, interaction: discord.Interaction, text: Optional[str] = None):
        if not interaction.guild or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ Only server administrators can send welcome messages.",
                ephemeral=True,
            )
            return

        try:
            if not interaction.response.is_done():
                await interaction.response.defer(thinking=True)
        except Exception:
            pass

        cm = getattr(self.bot, "chat_monitor", None)
        if not text:
            if self.ai:
                text = await self.ai.generate_response(
                    "Generate a short friendly welcome message",
                    "welcome-cmd",
                    bypass_trigger=True,
                    bypass_cooldown=True,
                )
                if text:
                    text = validate_rukiya_response(text, fallback="Welcome everyone! 🎉")
            if not text:
                text = "Welcome everyone! 🎉"

        if cm and cm.is_running:
            try:
                ok = await cm.send_chat_message(text, message_kind="welcome")
                if ok:
                    await interaction.followup.send("✅ Welcome sent to YouTube chat.", ephemeral=True)
                    return
            except Exception:
                logger.exception("Failed to send welcome to YouTube")

        try:
            await interaction.followup.send(f"📣 Welcome: {text}", allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.exception("Failed to send welcome fallback message")


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
