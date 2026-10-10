import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from services.config import Config
from services.chat_monitor import ChatMonitor
from services.memory_service import MemoryService
from services.orchestrator import RukiyaOrchestrator
from services.ai_engine.models import AIEngineResult
from cogs.chat_bot import RukiyaCog


class FakeYouTube:
    def __init__(self):
        self.sent_messages = []

    def send_message(self, live_chat_id, text, message_kind="reply"):
        self.sent_messages.append((live_chat_id, text, message_kind))
        return True

    def get_chat_messages(self, live_chat_id, page_token=None):
        return {"items": []}


class TestYouTubeMonitorV2(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = Config(processed_messages_max=5)
        self.memory = MemoryService(Config(db_path=":memory:"))
        self.orchestrator = RukiyaOrchestrator(config=self.config, memory_service=self.memory)
        self.yt = FakeYouTube()
        self.ai = MagicMock()
        self.ai.get_cooldown_remaining = MagicMock(return_value=0.0)

        self.monitor = ChatMonitor(
            youtube_service=self.yt,
            ai_service=self.ai,
            config=self.config,
            orchestrator=self.orchestrator
        )

    def test_bounded_lru_cache_eviction(self):
        # Insert 8 messages into a monitor with capacity 5
        self.monitor.processed_messages_max = 5
        for i in range(8):
            mid = f"msg_{i}"
            self.monitor.processed_messages[mid] = float(i)
            if len(self.monitor.processed_messages) > self.monitor.processed_messages_max:
                self.monitor.processed_messages.popitem(last=False)

        self.assertEqual(len(self.monitor.processed_messages), 5)
        # Oldest msg_0, msg_1, msg_2 should have been evicted
        self.assertNotIn("msg_0", self.monitor.processed_messages)
        self.assertNotIn("msg_1", self.monitor.processed_messages)
        self.assertNotIn("msg_2", self.monitor.processed_messages)
        self.assertIn("msg_7", self.monitor.processed_messages)

    async def test_process_messages_uses_author_display_name(self):
        self.yt.get_chat_messages = lambda live_chat_id, page_token=None: {
            "nextPageToken": "next",
            "pollingIntervalMillis": 8000,
            "items": [{
                "id": "yt-msg-1",
                "snippet": {"displayMessage": "rukiya hi"},
                "authorDetails": {"displayName": "Alice"},
            }],
        }

        received = []

        async def subscriber(message, author):
            received.append((message, author))

        self.monitor.subscribe(subscriber)
        self.monitor.start_monitoring("chat_live_123", video_id="vid_abc", start_background=False)
        await self.monitor.process_messages()

        self.assertEqual(received, [("rukiya hi", "Alice")])
        self.assertEqual(self.monitor.next_page_token, "next")
        self.assertEqual(self.monitor._next_poll_delay, 8.0)

        self.monitor.stop_monitoring()

    async def test_process_messages_passes_author_details_to_subscribers_that_accept_them(self):
        self.yt.get_chat_messages = lambda live_chat_id, page_token=None: {
            "nextPageToken": "next",
            "items": [{
                "id": "yt-msg-details",
                "snippet": {"displayMessage": "rukiya hi"},
                "authorDetails": {"displayName": "Alice", "channelId": "UCabcdefghijklmnopqrstuv", "isChatModerator": True},
            }],
        }
        legacy, detailed = [], []

        async def legacy_subscriber(message, author):
            legacy.append((message, author))

        async def detailed_subscriber(message, author, author_details=None):
            detailed.append((message, author, author_details))

        self.monitor.subscribe(legacy_subscriber)
        self.monitor.subscribe(detailed_subscriber)
        self.monitor.start_monitoring("chat_live_123", video_id="vid_abc", start_background=False)
        await self.monitor.process_messages()

        self.assertEqual(legacy, [("rukiya hi", "Alice")])
        self.assertEqual(len(detailed), 1)
        self.assertEqual(detailed[0][2]["channelId"], "UCabcdefghijklmnopqrstuv")
        self.monitor.stop_monitoring()

    async def test_youtube_viewers_are_keyed_by_channel_id_not_display_name(self):
        engine = MagicMock()
        engine.process = AsyncMock(return_value=AIEngineResult(text="Tch, chat.", provider="openrouter"))
        # A real database file (":memory:" is a new empty database on every connection).
        db_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, db_dir, True)
        memory = MemoryService(Config(db_path=os.path.join(db_dir, "yt_identity.db")))
        orchestrator = RukiyaOrchestrator(config=self.config, memory_service=memory, ai_engine=engine)
        resolved = []
        real_resolve = orchestrator.identity_service.resolve

        def spy_resolve(message):
            user = real_resolve(message)
            resolved.append(user.canonical_id)
            return user

        orchestrator.identity_service.resolve = spy_resolve

        class FakeChatMonitor:
            is_running = True

            async def send_chat_message(self, text):
                return True

        class FakeBot:
            pass

        bot = FakeBot()
        bot.orchestrator = orchestrator
        bot.ai_service = self.ai
        bot.chat_monitor = FakeChatMonitor()
        cog = RukiyaCog(bot)
        cog.enabled = True
        cog.cooldown_seconds = 0

        await cog.on_yt_message("rukiya hello", "Alex", {"channelId": "UCaaaaaaaaaaaaaaaaaaaaaa", "displayName": "Alex"})
        await cog.on_yt_message("rukiya hello", "Alex", {"channelId": "UCbbbbbbbbbbbbbbbbbbbbbb", "displayName": "Alex"})
        # Legacy two-argument delivery (no channel ID) keeps the old display-name key.
        await cog.on_yt_message("rukiya hello", "Alex")

        self.assertEqual(
            resolved,
            ["youtube:UCaaaaaaaaaaaaaaaaaaaaaa", "youtube:UCbbbbbbbbbbbbbbbbbbbbbb", "youtube:Alex"],
        )

    async def test_youtube_callback_reaches_send_path(self):
        engine = MagicMock()
        engine.process = AsyncMock(
            return_value=AIEngineResult(
                text="Tch, chat.",
                provider="openrouter",
            )
        )
        orchestrator = RukiyaOrchestrator(
            config=self.config,
            memory_service=self.memory,
            ai_engine=engine,
        )

        class FakeChatMonitor:
            is_running = True

            def __init__(self):
                self.sent_messages = []

            async def send_chat_message(self, text):
                self.sent_messages.append(text)
                return True

        class FakeBot:
            pass

        bot = FakeBot()
        bot.orchestrator = orchestrator
        bot.ai_service = self.ai
        bot.chat_monitor = FakeChatMonitor()

        cog = RukiyaCog(bot)
        cog.enabled = True
        cog._last_sent_at = 0.0

        await cog.on_yt_message("rukiya hello", "Alice")

        self.assertEqual(bot.chat_monitor.sent_messages, ["Tch, chat."])
        engine.process.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
