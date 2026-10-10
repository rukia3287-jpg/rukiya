#!/usr/bin/env python3
import os
import asyncio
import logging
import signal
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
from services.runtime_status import build_health_payload, shutdown_services

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

        # Cogs that failed to load, surfaced by /health.
        self.failed_cogs: list[str] = []

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
                except Exception:
                    # Full traceback: a bare message hides import errors inside the cog.
                    logger.exception(f"❌ Failed to load {module_path}")
                    self.failed_cogs.append(module_path)

        try:
            synced = await self.tree.sync()
            logger.info(f"🌐 Synced {len(synced)} slash commands")
        except Exception as e:
            logger.error(f"Slash command sync failed: {e}")

    async def on_ready(self):
        logger.info(f"🚀 {self.user} is online!")
        logger.info(f"📊 Connected to {len(self.guilds)} guild(s)")

    async def close(self):
        # Stop the YouTube polling task and AI engine before the Discord client goes away.
        await shutdown_services(self)
        await super().close()


# ----- Render health server -----

async def start_web_server(bot: RukiyaBot):
    async def health_check(request):
        # Always 200 so the host's liveness probe passes; the body reports degradation.
        return web.json_response(build_health_payload(bot), status=200)

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

def _install_shutdown_signals(bot: RukiyaBot) -> None:
    """Close the bot cleanly on SIGTERM (Render stop/redeploy) and SIGINT."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda s=sig: (
                logger.info("Received %s; shutting down", s.name),
                asyncio.ensure_future(bot.close()),
            ))
        except (NotImplementedError, RuntimeError, AttributeError):
            pass  # e.g. the Windows event loop has no signal handlers; Ctrl+C still works


async def main():
    bot = None
    try:
        bot = RukiyaBot()
        _install_shutdown_signals(bot)
        await start_web_server(bot)
        await bot.start(bot.config.discord_token)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        raise
    finally:
        # bot.start() returns or raises without running close() on most paths, so the
        # YouTube poller and AI engine are shut down here as well (close() is idempotent).
        if bot is not None and not bot.is_closed():
            await bot.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        # Preserve a non-zero process exit code so Render/systemd/Kubernetes
        # can detect a real startup crash and restart the service.
        logger.exception("Failed to start bot")
        raise
