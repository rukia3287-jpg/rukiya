"""tests/test_v2_runtime_status.py
Health reporting and orderly shutdown (services/runtime_status.py).
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from services.chat_monitor import ChatMonitor
from services.runtime_status import build_health_payload, shutdown_services


class FakeBot:
    pass


class HealthPayloadTests(unittest.TestCase):
    def test_reports_degraded_subsystems_without_failing(self):
        bot = FakeBot()
        bot.is_ready = lambda: True
        bot.failed_cogs = ["cogs.shayari"]
        bot.memory_service = MagicMock(fallback_mode=True)
        bot.chat_monitor = MagicMock(is_running=True)

        payload = build_health_payload(bot)

        self.assertEqual(payload, {
            "status": "degraded",
            "discord_ready": True,
            "failed_cogs": ["cogs.shayari"],
            "memory_fallback_mode": True,
            "youtube_monitoring": True,
        })

    def test_healthy_bot_is_ok(self):
        bot = FakeBot()
        bot.is_ready = lambda: True
        bot.failed_cogs = []
        bot.memory_service = MagicMock(fallback_mode=False)
        bot.chat_monitor = MagicMock(is_running=False)
        self.assertEqual(build_health_payload(bot)["status"], "ok")

    def test_health_check_gives_memory_a_chance_to_recover(self):
        import os
        import shutil
        import tempfile
        from services.config import Config
        from services.memory_service import MemoryService

        db_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, db_dir, True)
        now = [1000.0]
        memory = MemoryService(Config(db_path=os.path.join(db_dir, "h.db")), clock=lambda: now[0])
        memory.fallback_mode = True
        bot = FakeBot()
        bot.memory_service = memory

        self.assertTrue(build_health_payload(bot)["memory_fallback_mode"])
        now[0] += MemoryService.DB_RETRY_SECONDS + 1
        self.assertFalse(build_health_payload(bot)["memory_fallback_mode"])

    def test_bot_still_starting_up_does_not_raise(self):
        payload = build_health_payload(FakeBot())
        self.assertEqual(payload["status"], "ok")
        self.assertFalse(payload["discord_ready"])


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_stops_youtube_monitor_task_and_closes_orchestrator(self):
        youtube = MagicMock()
        youtube.get_chat_messages = MagicMock(return_value={"items": [], "pollingIntervalMillis": 10000})
        ai = MagicMock()
        ai.get_cooldown_remaining = MagicMock(return_value=0)
        monitor = ChatMonitor(youtube, ai, {"poll_interval": 10})
        self.assertTrue(monitor.start_monitoring("live-chat"))
        await asyncio.sleep(0)
        task = monitor._monitor_task

        bot = FakeBot()
        bot.chat_monitor = monitor
        bot.orchestrator = MagicMock()
        bot.orchestrator.aclose = AsyncMock()

        await shutdown_services(bot)

        self.assertTrue(task.done())
        self.assertFalse(monitor.is_running)
        bot.orchestrator.aclose.assert_awaited_once()

    async def test_a_failing_step_does_not_block_the_next(self):
        bot = FakeBot()
        bot.chat_monitor = MagicMock(is_running=True, _monitor_task=None)
        bot.chat_monitor.stop_monitoring.side_effect = RuntimeError("boom")
        bot.orchestrator = MagicMock()
        bot.orchestrator.aclose = AsyncMock()

        await shutdown_services(bot)

        bot.orchestrator.aclose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
