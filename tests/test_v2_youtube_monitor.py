import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from services.config import Config
from services.chat_monitor import ChatMonitor
from services.memory_service import MemoryService
from services.orchestrator import RukiyaOrchestrator


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

    def test_stream_session_lifecycle(self):
        self.monitor.start_monitoring("chat_live_123", video_id="vid_abc", start_background=False)
        self.assertTrue(self.monitor.is_running)
        self.assertIsNotNone(self.monitor.stream_session_id)
        self.assertIn("vid_abc", self.monitor.stream_session_id)
        self.assertEqual(self.memory.active_session_id, self.monitor.stream_session_id)

        # Stop monitoring
        self.monitor.stop_monitoring()
        self.assertFalse(self.monitor.is_running)
        self.assertIsNone(self.monitor.stream_session_id)
        self.assertIsNone(self.memory.active_session_id)


if __name__ == "__main__":
    unittest.main()
