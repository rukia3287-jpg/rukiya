#!/usr/bin/env python3
import os
import asyncio
import logging
from dotenv import load_dotenv
import discord
from discord.ext import commands

from services.config import Config
from services.youtube_service import YouTubeService
from services.ai_service import AIService
from services.chat_monitor import ChatMonitor

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('rukiya_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

class RukiyaBot(commands.Bot):
    """Main bot class"""

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None
        )

        # Initialize configuration
        try:
            self.config = Config(
                discord_token=os.getenv("DISCORD_TOKEN"),
                gemini_api_key=os.getenv("GEMINI_API_KEY")
            )
        except ValueError as e:
            logger.error(f"Configuration error: {e}")
            raise

        # Initialize services
        self.youtube_service = YouTubeService(self.config)
        self.ai_service = AIService(self.config)
        self.chat_monitor = ChatMonitor(
            self.youtube_service, 
            self.ai_service, 
            self.config
        )

        logger.info("Bot initialized successfully")

    async def setup_hook(self):
        """Setup hook to load cogs and sync commands"""
        # Load all cogs
        cogs_to_load = [
            'cogs.youtube_commands',
            'cogs.admin_commands', 
            'cogs.utility_commands',
            'cogs.events'
        ]

        for cog in cogs_to_load:
            try:
                await self.load_extension(cog)
                logger.info(f"✅ Loaded cog: {cog}")
            except Exception as e:
                logger.error(f"❌ Failed to load cog {cog}: {e}")

        # Sync slash commands
        try:
            synced = await self.tree.sync()
            logger.info(f"✅ Synced {len(synced)} slash commands")
        except Exception as e:
            logger.error(f"❌ Failed to sync commands: {e}")

    async def on_ready(self):
        logger.info(f"🚀 {self.user} is online and ready!")
        logger.info(f"📊 Connected to {len(self.guilds)} guild(s)")

async def main():
    """Main function to run the bot"""
    try:
        bot = RukiyaBot()
        await bot.start(bot.config.discord_token)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")