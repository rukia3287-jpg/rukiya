import logging
import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

class Events(commands.Cog):
    """General event listeners"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"Bot ready: {self.bot.user} (ID: {self.bot.user.id})")
        logger.info(f"Guilds: {len(self.bot.guilds)}")
        
        # Check services
        if not getattr(self.bot, "ai_service", None):
            logger.warning("⚠️ ai_service not attached to bot")
        if not getattr(self.bot, "chat_monitor", None):
            logger.warning("⚠️ chat_monitor not attached to bot")

    @commands.Cog.listener()
    async def on_app_command_error(self, interaction: discord.Interaction, error: Exception):
        """Handle slash command errors"""
        logger.exception(f"Slash command error: {error}")
        
        error_message = f"⚠️ Error: {str(error)}"
        
        try:
            if interaction.response.is_done():
                await interaction.followup.send(error_message, ephemeral=True)
            else:
                await interaction.response.send_message(error_message, ephemeral=True)
        except Exception as e:
            logger.exception(f"Failed to report error to user: {e}")

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Handle prefix command errors"""
        if isinstance(error, commands.CommandNotFound):
            return  # Ignore command not found
        
        logger.exception(f"Command error: {error}")
        
        try:
            await ctx.send(f"⚠️ Error: {error}", delete_after=10)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Events(bot))
