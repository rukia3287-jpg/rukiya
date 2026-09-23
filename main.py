#!/usr/bin/env python3
import os
import asyncio
import logging
from dotenv import load_dotenv
import discord
from discord.ext import commands
from aiohttp import web

from services.config import Config
from services.youtube_service import YouTubeService
from services.ai_service import AIService
from services.chat_monitor import ChatMonitor
from services.memory_service import MemoryService
from services.identity_service import IdentityService
from services.safety_service import SafetyService
from services.decision_service import DecisionService
from services.rate_limiter import RateLimiter
from services.orchestrator import RukiyaOrchestrator
from services.ai_engine import AIEngine

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('rukiya_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()


class RukiyaBot(commands.Bot):
    """Main bot class with all V2 services attached"""

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None
        )

        # Load centralized configuration
        self.config = Config(
            discord_token=os.getenv("DISCORD_TOKEN"),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY"),
            gemini_api_key=os.getenv("GEMINI_API_KEY"),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
        )

        # Initialize V2 foundational services
        self.memory_service = MemoryService(self.config)
        self.identity_service = IdentityService(self.memory_service)
        self.safety_service = SafetyService(self.config)
        self.decision_service = DecisionService(self.config)
        self.rate_limiter = RateLimiter(self.config)
        self.youtube_service = YouTubeService(self.config)
        self.ai_service = AIService(self.config)
        self.ai_engine = AIEngine(self.config)

        # Central Orchestrator coordinating the pipeline
        self.orchestrator = RukiyaOrchestrator(
            config=self.config,
            identity_service=self.identity_service,
            memory_service=self.memory_service,
            decision_service=self.decision_service,
            safety_service=self.safety_service,
            rate_limiter=self.rate_limiter,
            ai_service=self.ai_service,
            ai_engine=self.ai_engine
        )

        # Chat Monitor wired to orchestrator
        self.chat_monitor = ChatMonitor(
            self.youtube_service,
            self.ai_service,
            self.config,
            orchestrator=self.orchestrator
        )

        logger.info("RukiyaBot V2 services and orchestrator initialized successfully")

    async def setup_hook(self):
        """Load cogs and sync slash commands"""
        cogs_folder = os.path.join(os.path.dirname(__file__), 'cogs')
        for filename in os.listdir(cogs_folder):
            if filename.endswith('.py') and not filename.startswith('__'):
                module_path = f'cogs.{filename[:-3]}'
                try:
                    await self.load_extension(module_path)
                    logger.info(f"✅ Loaded cog: {module_path}")
                except Exception as e:
                    logger.error(f"❌ Failed to load {module_path}: {e}")

        try:
            synced = await self.tree.sync()
            logger.info(f"🌐 Synced {len(synced)} slash commands")
        except Exception as e:
            logger.error(f"Slash command sync failed: {e}")

    async def on_ready(self):
        logger.info(f"🚀 {self.user} is online!")
        logger.info(f"📊 Connected to {len(self.guilds)} guild(s)")


# ----- Render health server -----

async def health_check(request):
    return web.Response(text="Bot is running!", status=200)


async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)

    port = int(os.getenv('PORT', 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"🌐 Health check server on port {port}")


# ----- Main entrypoint -----

async def main():
    try:
        bot = RukiyaBot()
        await start_web_server()
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
