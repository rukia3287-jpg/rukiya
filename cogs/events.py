# cogs/events.py
import logging
import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

class Events(commands.Cog):
    """General event listeners (ready, errors)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"Bot ready: {self.bot.user} (ID: {self.bot.user.id})")
        # Informational: attach services to bot if not present (defensive)
        if not getattr(self.bot, "ai_service", None):
            logger.warning("ai_service not attached to bot (expected).")
        if not getattr(self.bot, "chat_monitor", None):
            logger.warning("chat_monitor not attached to bot (expected).")

    @commands.Cog.listener()
    async def on_app_command_error(self, interaction: discord.Interaction, error: Exception):
        # Top-level handler for slash command errors to avoid silent failures
        try:
            logger.exception("Slash command error: %s", error)
            if interaction.response.is_done():
                # If already responded, use followup
                try:
                    await interaction.followup.send(f"⚠️ Command error: {error}", ephemeral=True)
                except Exception:
                    pass
            else:
                try:
                    await interaction.response.send_message(f"⚠️ Command error: {error}", ephemeral=True)
                except Exception:
                    pass
        except Exception:
            logger.exception("Failed to report slash command error")

async def setup(bot: commands.Bot):
    await bot.add_cog(Events(bot))
