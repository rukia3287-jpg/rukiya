# cogs/welcome.py
import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

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
                welcome_msg = f"Welcome {member.display_name}! 🎉"
                if self.ai:
                    try:
                        maybe = await self.ai.generate_response(f"Write a friendly short welcome for {member.display_name}", "welcome-bot")
                        if maybe:
                            welcome_msg = maybe
                    except Exception:
                        logger.exception("AI generation for welcome failed")

                try:
                    await cm.send_chat_message(welcome_msg)
                except Exception:
                    logger.exception("Failed to send welcome to YouTube chat")

        except Exception:
            logger.exception("on_member_join failure")

    @app_commands.command(name="welcome_send", description="Send a manual welcome message to YouTube chat (or Discord fallback)")
    @app_commands.describe(text="Text to send (if empty, AI or default will be used)")
    async def welcome_send(self, interaction: discord.Interaction, text: Optional[str] = None):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(thinking=True)
        except Exception:
            pass

        cm = getattr(self.bot, "chat_monitor", None)
        if not text:
            if self.ai:
                text = await self.ai.generate_response("Generate a short friendly welcome message", "welcome-cmd")
            if not text:
                text = "Welcome everyone! 🎉"

        if cm and cm.is_running:
            try:
                ok = await cm.send_chat_message(text)
                if ok:
                    await interaction.followup.send("✅ Welcome sent to YouTube chat.", ephemeral=True)
                    return
            except Exception:
                logger.exception("Failed to send welcome to YouTube")

        try:
            await interaction.followup.send(f"📣 Welcome: {text}")
        except Exception:
            logger.exception("Failed to send welcome fallback message")


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
