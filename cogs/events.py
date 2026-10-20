import logging
from discord.ext import commands
from services.discord_ui import record_error

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
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Handle prefix command errors"""
        if isinstance(error, commands.CommandNotFound):
            return  # Ignore command not found

        reference = record_error(getattr(error, "original", error))

        try:
            await ctx.send(f"⚠️ The command could not finish. An operator can check reference `{reference}` in the bot logs.", delete_after=10)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Events(bot))
