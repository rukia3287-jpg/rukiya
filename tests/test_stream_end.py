import asyncio
import sys
import types
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

# Ensure googleapiclient mocks exist for headless test running
for module_name in (
    "googleapiclient",
    "googleapiclient.discovery",
    "googleapiclient.errors",
    "google",
    "google.auth",
    "google.auth.transport",
    "google.auth.transport.requests",
    "google.oauth2",
    "google.oauth2.credentials",
):
    sys.modules.setdefault(module_name, types.ModuleType(module_name))

if not hasattr(sys.modules["googleapiclient.errors"], "HttpError"):
    sys.modules["googleapiclient.errors"].HttpError = type("HttpError", (Exception,), {})
if not hasattr(sys.modules["google.auth.transport.requests"], "Request"):
    sys.modules["google.auth.transport.requests"].Request = type("Request", (), {})
if not hasattr(sys.modules["google.oauth2.credentials"], "Credentials"):
    sys.modules["google.oauth2.credentials"].Credentials = type("Credentials", (), {})

from services.chat_monitor import ChatMonitor
from services.youtube_service import YouTubeService, ChatBot
from googleapiclient.errors import HttpError


class DummyAI:
    def get_cooldown_remaining(self):
        return 0

    async def generate_response(self, text, author):
        return f"Echo {text}"


class MockHttpError(HttpError):
    def __init__(self, status, reason, message=""):
        super().__init__(f"HttpError {status}: {reason} {message}")
        self.status = status
        self.reason = reason
        self.content = f'{{"error": {{"errors": [{{"reason": "{reason}"}}], "code": {status}, "message": "{message}"}}}}'.encode("utf-8")

        class Resp:
            def __init__(self, s):
                self.status = s
        self.resp = Resp(status)


class StreamEndTests(unittest.IsolatedAsyncioTestCase):

    async def test_offline_at_in_response_stops_monitoring(self):
        service = MagicMock()
        service.get_chat_messages.return_value = {
            "items": [
                {
                    "id": "msg1",
                    "snippet": {
                        "displayMessage": "Final message before stream ends",
                        "authorDisplayName": "Viewer1",
                    }
                }
            ],
            "offlineAt": "2026-09-05T12:00:00Z"
        }

        received_msgs = []
        async def on_msg(m, a):
            received_msgs.append((m, a))

        stopped_reasons = []
        async def on_stop(reason):
            stopped_reasons.append(reason)

        monitor = ChatMonitor(service, DummyAI(), {"poll_interval": 10})
        monitor.subscribe(on_msg)
        monitor.register_stop_callback(on_stop)

        self.assertTrue(monitor.start_monitoring("test_chat_id", "test_video_id"))
        task = monitor._monitor_task
        await task

        self.assertFalse(monitor.is_running)
        self.assertEqual(monitor.last_stop_reason, "stream_ended")
        self.assertEqual(len(received_msgs), 1)
        self.assertEqual(received_msgs[0][0], "Final message before stream ends")
        self.assertIn("stream_ended", stopped_reasons)

    async def test_live_chat_ended_error_stops_without_retry(self):
        service = MagicMock()
        service.get_chat_messages.side_effect = MockHttpError(403, "liveChatEnded", "The live chat is no longer active.")

        stopped_reasons = []
        async def on_stop(reason):
            stopped_reasons.append(reason)

        monitor = ChatMonitor(service, DummyAI(), {"poll_interval": 10})
        monitor.register_stop_callback(on_stop)

        self.assertTrue(monitor.start_monitoring("test_chat_id", "test_video_id"))
        task = monitor._monitor_task
        await task

        self.assertFalse(monitor.is_running)
        self.assertEqual(monitor.last_stop_reason, "stream_ended")
        self.assertEqual(service.get_chat_messages.call_count, 1)
        self.assertIn("stream_ended", stopped_reasons)

    async def test_live_chat_not_found_error_stops_monitoring(self):
        service = MagicMock()
        service.get_chat_messages.side_effect = MockHttpError(404, "liveChatNotFound", "The live chat cannot be found.")

        monitor = ChatMonitor(service, DummyAI(), {"poll_interval": 10})
        self.assertTrue(monitor.start_monitoring("test_chat_id", "test_video_id"))
        task = monitor._monitor_task
        await task

        self.assertFalse(monitor.is_running)
        self.assertEqual(monitor.last_stop_reason, "stream_ended")
        self.assertEqual(service.get_chat_messages.call_count, 1)

    async def test_live_chat_disabled_error_stops_monitoring(self):
        service = MagicMock()
        service.get_chat_messages.side_effect = MockHttpError(403, "liveChatDisabled", "The live chat is disabled for this stream.")

        monitor = ChatMonitor(service, DummyAI(), {"poll_interval": 10})
        self.assertTrue(monitor.start_monitoring("test_chat_id", "test_video_id"))
        task = monitor._monitor_task
        await task

        self.assertFalse(monitor.is_running)
        self.assertEqual(monitor.last_stop_reason, "stream_ended")
        self.assertEqual(service.get_chat_messages.call_count, 1)

    async def test_periodic_stream_liveness_check_detects_ended_stream(self):
        service = MagicMock()
        # Chat messages call succeeds with empty items
        service.get_chat_messages.return_value = {"items": [], "pollingIntervalMillis": 1000}
        # Stream liveness check reports stream ended (False)
        service.is_stream_live.return_value = False

        monitor = ChatMonitor(service, DummyAI(), {"poll_interval": 1.0, "stream_check_interval": 0.0})
        self.assertTrue(monitor.start_monitoring("test_chat_id", "test_video_id"))
        task = monitor._monitor_task
        await task

        self.assertFalse(monitor.is_running)
        self.assertEqual(monitor.last_stop_reason, "stream_ended")
        service.is_stream_live.assert_called_with("test_video_id")

    async def test_status_reports_last_stop_reason(self):
        service = MagicMock()
        service.get_chat_messages.side_effect = MockHttpError(403, "liveChatEnded")

        monitor = ChatMonitor(service, DummyAI())
        self.assertTrue(monitor.start_monitoring("test_chat_id"))
        await monitor._monitor_task

        st = monitor.get_status()
        self.assertFalse(st["is_running"])
        self.assertEqual(st["last_stop_reason"], "stream_ended")

    async def test_manual_stop_records_manual_reason(self):
        service = MagicMock()
        service.get_chat_messages.return_value = {"items": [], "pollingIntervalMillis": 10000}

        monitor = ChatMonitor(service, DummyAI())
        self.assertTrue(monitor.start_monitoring("test_chat_id"))
        await asyncio.sleep(0.01)

        monitor.stop_monitoring(reason="manual")
        await monitor._monitor_task

        st = monitor.get_status()
        self.assertFalse(st["is_running"])
        self.assertEqual(st["last_stop_reason"], "manual")


class YouTubeServiceLivenessTests(unittest.TestCase):

    def _create_service(self, list_return_value=None, list_side_effect=None):
        service = object.__new__(YouTubeService)
        client = MagicMock()
        videos_resource = MagicMock()
        list_req = MagicMock()
        if list_side_effect:
            list_req.execute.side_effect = list_side_effect
        else:
            list_req.execute.return_value = list_return_value or {}
        videos_resource.list.return_value = list_req
        client.videos.return_value = videos_resource
        service.youtube = client
        return service

    def test_is_stream_live_returns_true_when_stream_active(self):
        service = self._create_service({
            "items": [{
                "snippet": {"liveBroadcastContent": "live"},
                "liveStreamingDetails": {
                    "activeLiveChatId": "live_chat_123"
                }
            }]
        })
        self.assertTrue(service.is_stream_live("video123"))

    def test_is_stream_live_returns_false_when_actual_end_time_set(self):
        service = self._create_service({
            "items": [{
                "snippet": {"liveBroadcastContent": "none"},
                "liveStreamingDetails": {
                    "activeLiveChatId": "live_chat_123",
                    "actualEndTime": "2026-09-05T12:00:00Z"
                }
            }]
        })
        self.assertFalse(service.is_stream_live("video123"))

    def test_is_stream_live_returns_false_when_broadcast_none(self):
        service = self._create_service({
            "items": [{
                "snippet": {"liveBroadcastContent": "none"},
                "liveStreamingDetails": {}
            }]
        })
        self.assertFalse(service.is_stream_live("video123"))

    def test_is_stream_live_returns_false_when_no_items(self):
        service = self._create_service({"items": []})
        self.assertFalse(service.is_stream_live("missing_video"))

    def test_is_stream_live_returns_false_on_404_error(self):
        service = self._create_service(list_side_effect=MockHttpError(404, "videoNotFound"))
        self.assertFalse(service.is_stream_live("video404"))


class LegacyChatBotStreamEndTests(unittest.IsolatedAsyncioTestCase):

    async def test_chatbot_process_once_stops_on_offline_at(self):
        service = MagicMock()
        service.get_chat_messages.return_value = {
            "items": [],
            "offlineAt": "2026-09-05T12:00:00Z"
        }
        config = MagicMock()
        config.poll_interval = 1

        bot = ChatBot(service, DummyAI(), config)
        bot.running = True
        bot.live_chat_id = "chat123"

        await bot._process_once()
        self.assertFalse(bot.running)

    async def test_chatbot_process_once_stops_on_live_chat_ended_error(self):
        service = MagicMock()
        service.get_chat_messages.side_effect = MockHttpError(403, "liveChatEnded")
        config = MagicMock()
        config.poll_interval = 1

        bot = ChatBot(service, DummyAI(), config)
        bot.running = True
        bot.live_chat_id = "chat123"

        await bot._process_once()
        self.assertFalse(bot.running)


if __name__ == "__main__":
    unittest.main()
