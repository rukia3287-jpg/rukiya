"""tests/test_v2_runtime_limits.py
Startup configuration parsing, bounded per-user runtime state, and the Discord AI rate limit.
"""
import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.chat_bot import RukiyaCog
from services.config import Config
from services.decision_service import DecisionService
from services.orchestrator import RukiyaOrchestrator
from services.rate_limiter import RateLimiter


class ConfigParsingTests(unittest.TestCase):
    def test_one_invalid_number_does_not_discard_the_settings_after_it(self):
        env = {
            "AI_COOLDOWN": "not-a-number",      # first in its block
            "MAX_MESSAGE_LENGTH": "123",
            "POLL_INTERVAL": "7",
            "MEMORY_DECAY_DAYS": "soon",        # first in the float block
            "RESPONSE_THRESHOLD": "0.42",
        }
        with patch.dict(os.environ, env):
            with self.assertLogs("services.config", level="WARNING") as logs:
                config = Config()

        self.assertEqual(config.ai_cooldown, Config.ai_cooldown)
        self.assertEqual(config.max_message_length, 123)
        self.assertEqual(config.poll_interval, 7)
        self.assertEqual(config.memory_decay_days, Config.memory_decay_days)
        self.assertAlmostEqual(config.response_threshold, 0.42)
        joined = "\n".join(logs.output)
        self.assertIn("AI_COOLDOWN", joined)
        self.assertIn("MEMORY_DECAY_DAYS", joined)
        self.assertNotIn("not-a-number", joined)  # values are not echoed into logs


class BoundedStateTests(unittest.TestCase):
    def test_rate_limiter_drops_idle_per_user_buckets(self):
        limiter = RateLimiter(Config())
        limiter.MAX_KEYED_BUCKETS = 50
        with patch("services.rate_limiter.time.time", return_value=1000.0):
            for i in range(200):
                limiter.allow("user_ai", key=f"viewer{i}")
        # Long enough later that every bucket has refilled: they carry no state any more.
        with patch("services.rate_limiter.time.time", return_value=5000.0):
            limiter.allow("user_ai", key="newcomer")
        self.assertLessEqual(len(limiter._buckets), 51)

    def test_rate_limiter_keeps_buckets_that_still_hold_state(self):
        limiter = RateLimiter(Config())
        limiter.MAX_KEYED_BUCKETS = 5
        with patch("services.rate_limiter.time.time", return_value=1000.0):
            for _ in range(2):
                limiter.allow("user_ai", key="spammer")  # drains the 2-token bucket
            for i in range(10):
                limiter.allow("user_ai", key=f"viewer{i}")
            # Pruning must not hand the drained user a fresh bucket.
            self.assertFalse(limiter.allow("user_ai", key="spammer").allowed)

    def test_decision_service_forgets_users_outside_the_cooldown_window(self):
        decision = DecisionService(Config())
        decision.MAX_TRACKED_USERS = 20
        with patch("services.decision_service.time.time", return_value=1000.0):
            for i in range(100):
                decision.record_response(f"user{i}", f"reply {i}")
        with patch("services.decision_service.time.time", return_value=2000.0):
            decision.record_response("latest", "another reply")
        self.assertLessEqual(len(decision._last_responded_timestamps), 21)
        self.assertIn("latest", decision._last_responded_timestamps)


class FakeUser:
    def __init__(self, id=222, name="Alice", bot=False):
        self.id, self.name, self.display_name, self.bot = id, name, name, bot


class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


class FakeMessage:
    def __init__(self, content, author):
        self.content, self.author, self.mentions = content, author, []
        self.reply = AsyncMock()
        self.channel = MagicMock()
        self.channel.typing = lambda: FakeTyping()


class DiscordRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = MagicMock()
        self.bot.user = FakeUser(id=999, name="Rukiya", bot=True)
        ctx = MagicMock()
        ctx.valid = False
        self.bot.get_context = AsyncMock(return_value=ctx)
        self.orch = MagicMock(spec=RukiyaOrchestrator)
        self.orch.process_raw_text = AsyncMock(return_value="Hm. What is it?")
        self.bot.orchestrator = self.orch
        self.bot.rate_limiter = RateLimiter(Config(rate_limit_discord_capacity=1))
        self.cog = RukiyaCog(self.bot)
        self.cog.discord_cooldown_seconds = 0.0

    async def test_discord_mentions_respect_the_discord_ai_bucket(self):
        with patch("services.rate_limiter.time.time", return_value=1000.0):
            await self.cog.on_message(FakeMessage("rukiya hi", FakeUser(id=1, name="A")))
            await self.cog.on_message(FakeMessage("rukiya hi", FakeUser(id=2, name="B")))
        self.assertEqual(self.orch.process_raw_text.await_count, 1)

    async def test_one_user_cannot_drain_the_shared_discord_bucket(self):
        self.bot.rate_limiter = RateLimiter(Config(rate_limit_discord_capacity=5))
        self.cog.discord_cooldown_seconds = 30.0
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_reply(*args, **kwargs):
            started.set()
            await release.wait()
            return "Hm."

        self.orch.process_raw_text = AsyncMock(side_effect=slow_reply)
        spammer = FakeUser(id=7, name="Spammer")
        first = asyncio.create_task(self.cog.on_message(FakeMessage("rukiya hi", spammer)))
        await started.wait()
        try:
            for _ in range(4):
                # Repeat mentions must be turned away at once, not queued behind the first.
                await asyncio.wait_for(self.cog.on_message(FakeMessage("rukiya hi again", spammer)), timeout=1)
        finally:
            release.set()
            await first

        self.assertEqual(self.orch.process_raw_text.await_count, 1)
        self.assertGreaterEqual(self.bot.rate_limiter.get_remaining("discord_ai"), 3.9)

    async def test_slash_ask_reports_the_discord_rate_limit(self):
        def interaction():
            it = MagicMock()
            it.response.defer = AsyncMock()
            it.response.send_message = AsyncMock()
            it.followup.send = AsyncMock()
            it.user.display_name = "Viewer"
            it.user.guild_permissions.administrator = False
            return it

        first, second = interaction(), interaction()
        with patch("services.rate_limiter.time.time", return_value=1000.0):
            await self.cog.slash_ask.callback(self.cog, first, question="hello")
            await self.cog.slash_ask.callback(self.cog, second, question="hello again")

        self.assertEqual(self.orch.process_raw_text.await_count, 1)
        second.response.send_message.assert_awaited_once()
        self.assertTrue(second.response.send_message.call_args[1].get("ephemeral"))


if __name__ == "__main__":
    unittest.main()
