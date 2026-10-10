"""tests/test_v2_youtube_errors.py
YouTube API failures are classified (transient vs permanent), logged with context but
without message content, and permanent ones stop the monitor instead of looping.
"""
import json
import unittest
from unittest.mock import MagicMock

from services.chat_monitor import ChatMonitor
from services.youtube_service import YouTubeService, classify_youtube_error


class FakeApiError(Exception):
    """Duck-typed like googleapiclient.errors.HttpError (resp.status + JSON content)."""

    def __init__(self, status, reason=None):
        self.resp = MagicMock(status=status)
        body = {"error": {"code": status, "errors": [{"reason": reason}] if reason else []}}
        self.content = json.dumps(body).encode()
        super().__init__(f"HttpError {status}")


class ClassificationTests(unittest.TestCase):
    def test_classifies_permanent_and_transient_errors(self):
        cases = [
            (FakeApiError(403, "quotaExceeded"), "quota", False, True),
            (FakeApiError(403, "rateLimitExceeded"), "rate_limited", True, False),
            (FakeApiError(429), "rate_limited", True, False),
            (FakeApiError(403, "liveChatEnded"), "chat_ended", False, True),
            (FakeApiError(403, "liveChatDisabled"), "chat_ended", False, True),
            (FakeApiError(404, "liveChatNotFound"), "chat_ended", False, True),
            (FakeApiError(401), "auth", False, True),
            (FakeApiError(403, "forbidden"), "forbidden", False, False),
            (FakeApiError(400, "messageTextInvalid"), "invalid", False, False),
            (FakeApiError(503), "transient", True, False),
            (TimeoutError("read timed out"), "transient", True, False),
            (ConnectionError("reset"), "transient", True, False),
        ]
        for exc, kind, retryable, stops in cases:
            with self.subTest(exc=repr(exc), reason=getattr(exc, "content", b"")[:80]):
                err = classify_youtube_error(exc)
                self.assertEqual((err.kind, err.retryable, err.stops_monitoring), (kind, retryable, stops))

    def test_real_http_error_type_is_understood(self):
        try:
            from googleapiclient.errors import HttpError
        except ImportError:
            self.skipTest("google-api-python-client not installed")
        resp = MagicMock(status=403, reason="Forbidden")
        exc = HttpError(resp, json.dumps({"error": {"errors": [{"reason": "liveChatEnded"}]}}).encode())
        err = classify_youtube_error(exc)
        self.assertEqual((err.kind, err.status, err.reason), ("chat_ended", 403, "liveChatEnded"))


def _service_raising(exc):
    service = object.__new__(YouTubeService)
    service._nonessential_insert_count = 0
    service._nonessential_insert_cap = 60
    service._nonessential_warning_at = 48
    service._nonessential_warning_logged = False
    insert = MagicMock()
    insert.execute.side_effect = exc
    service.youtube = MagicMock()
    service.youtube.liveChatMessages.return_value.insert.return_value = insert
    return service


class SendMessageTests(unittest.TestCase):
    def test_failed_send_records_the_error_and_logs_no_message_text(self):
        service = _service_raising(FakeApiError(403, "liveChatEnded"))
        with self.assertLogs("services.youtube_service", level="WARNING") as logs:
            self.assertFalse(service.send_message("chat", "secret viewer detail in reply", message_kind="reply"))

        self.assertEqual(service.last_send_error.kind, "chat_ended")
        joined = "\n".join(logs.output)
        self.assertIn("chat_ended", joined)
        self.assertNotIn("secret viewer detail", joined)


class MonitorReactionTests(unittest.IsolatedAsyncioTestCase):
    def _monitor(self, youtube):
        ai = MagicMock()
        ai.get_cooldown_remaining = MagicMock(return_value=0)
        monitor = ChatMonitor(youtube, ai, {"send_cooldown": 0})
        monitor.start_monitoring("live-chat", start_background=False)
        return monitor

    async def test_permanent_send_failure_stops_monitoring(self):
        monitor = self._monitor(_service_raising(FakeApiError(403, "liveChatEnded")))

        self.assertFalse(await monitor.send_chat_message("hello"))

        self.assertFalse(monitor.is_running)
        self.assertEqual(monitor.get_status()["last_error"], "send:chat_ended:liveChatEnded")

    async def test_transient_send_failure_keeps_monitoring(self):
        monitor = self._monitor(_service_raising(TimeoutError("timed out")))

        self.assertFalse(await monitor.send_chat_message("hello"))

        self.assertTrue(monitor.is_running)
        self.assertTrue(monitor.get_status()["last_error"].startswith("send:transient"))
        monitor.stop_monitoring()

    async def test_ended_chat_while_polling_stops_without_retry(self):
        youtube = MagicMock()
        youtube.get_chat_messages = MagicMock(side_effect=FakeApiError(403, "liveChatEnded"))
        monitor = self._monitor(youtube)

        await monitor.process_messages()

        self.assertFalse(monitor.is_running)
        self.assertEqual(youtube.get_chat_messages.call_count, 1)
        self.assertEqual(monitor.get_status()["last_error"], "poll:chat_ended:liveChatEnded")

    async def test_transient_polling_error_is_raised_for_backoff(self):
        youtube = MagicMock()
        youtube.get_chat_messages = MagicMock(side_effect=FakeApiError(503))
        monitor = self._monitor(youtube)

        with self.assertRaises(FakeApiError):
            await monitor.process_messages()
        self.assertTrue(monitor.is_running)
        monitor.stop_monitoring()


if __name__ == "__main__":
    unittest.main()
