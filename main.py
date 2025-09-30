#!/usr/bin/env python3
import os
import asyncio
import logging
from dotenv import load_dotenv
import discord
from discord.ext import commands
from aiohttp import web
import threading

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
        """Automatically load all cogs in cogs/ folder and sync slash commands"""
        cogs_folder = os.path.join(os.path.dirname(__file__), 'cogs')
        for filename in os.listdir(cogs_folder):
            if filename.endswith('.py') and not filename.startswith('__'):
                cog_name = filename[:-3]  # remove '.py'
                module_path = f'cogs.{cog_name}'
                try:
                    await self.load_extension(module_path)
                    logger.info(f"✅ Loaded cog: {module_path}")
                except Exception as e:
                    logger.error(f"❌ Failed to load cog {module_path}: {e}")

        
        # Sync slash commands
        try:
            synced = await self.tree.sync()
            logger.info(f"✅ Synced {len(synced)} slash commands")
        except Exception as e:
            logger.error(f"❌ Failed to sync commands: {e}")

    async def on_ready(self):
        logger.info(f"🚀 {self.user} is online and ready!")
        logger.info(f"📊 Connected to {len(self.guilds)} guild(s)")

async def health_check(request):
    """Simple health check endpoint for Render"""
    return web.Response(text="Bot is running!", status=200)

async def start_web_server():
    """Start a simple web server for health checks"""
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    port = int(os.getenv('PORT', 8080))  # Render provides PORT env variable
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"🌐 Health check server started on port {port}")

async def main():
    """Main function to run the bot"""
    try:
        bot = RukiyaBot()
        
        # Always start health check server for Render Web Service
        await start_web_server()
        
        # Start the Discord bot
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
