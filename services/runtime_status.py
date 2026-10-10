"""services/runtime_status.py
Process-level health reporting and shutdown for the bot, kept separate from main.py so it
can be unit-tested without starting Discord.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def build_health_payload(bot: Any) -> Dict[str, Any]:
    """Summarize subsystem health for the /health endpoint.

    The endpoint always answers HTTP 200 (it is a liveness probe for the host); the body
    says whether anything is degraded. It never includes secrets, prompts, or chat content.
    """
    is_ready = getattr(bot, "is_ready", None)
    discord_ready = bool(is_ready()) if callable(is_ready) else False
    failed_cogs = sorted(getattr(bot, "failed_cogs", []) or [])
    memory = getattr(bot, "memory_service", None)
    memory_fallback = bool(getattr(memory, "fallback_mode", False))
    monitor = getattr(bot, "chat_monitor", None)
    youtube_monitoring = bool(getattr(monitor, "is_running", False))

    return {
        "status": "degraded" if (failed_cogs or memory_fallback) else "ok",
        "discord_ready": discord_ready,
        "failed_cogs": failed_cogs,
        "memory_fallback_mode": memory_fallback,
        "youtube_monitoring": youtube_monitoring,
    }


async def shutdown_services(bot: Any, task_timeout: float = 5.0) -> None:
    """Stop background work before the Discord client closes. Each step is isolated."""
    monitor = getattr(bot, "chat_monitor", None)
    if monitor is not None:
        task = getattr(monitor, "_monitor_task", None)
        try:
            if getattr(monitor, "is_running", False) or (task is not None and not task.done()):
                monitor.stop_monitoring()
            if task is not None and not task.done():
                await asyncio.wait({task}, timeout=task_timeout)
        except Exception as e:
            logger.warning("Error while stopping YouTube chat monitor: %s", e)

    orchestrator = getattr(bot, "orchestrator", None)
    if orchestrator is not None and hasattr(orchestrator, "aclose"):
        try:
            await orchestrator.aclose()
        except Exception as e:
            logger.warning("Error during orchestrator shutdown: %s", e)
