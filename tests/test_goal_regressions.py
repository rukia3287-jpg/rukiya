import asyncio
import sys
import threading
import types
import unittest
from unittest.mock import patch

from services.chat_monitor import ChatMonitor

# The quota logic is unit-tested without requiring production OAuth packages.
for module_name in ("googleapiclient", "googleapiclient.discovery", "googleapiclient.errors", "google", "google.auth", "google.auth.transport", "google.auth.transport.requests", "google.oauth2", "google.oauth2.credentials"):
    sys.modules.setdefault(module_name, types.ModuleType(module_name))
sys.modules["googleapiclient.discovery"].build = lambda *_args, **_kwargs: None
sys.modules["googleapiclient.errors"].HttpError = type("HttpError", (Exception,), {})
sys.modules["google.auth.transport.requests"].Request = type("Request", (), {})
sys.modules["google.oauth2.credentials"].Credentials = type("Credentials", (), {})
from services.youtube_service import YouTubeService
from services.ai_service import RUKIYA_SYSTEM_PROMPT, validate_rukiya_response


class EmptyAI:
    def get_cooldown_remaining(self):
        return 0


class ResponseService:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response or {"items": []}, error, 0

    def get_chat_messages(self, *_):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response

    def send_message(self, *_args, **_kwargs):
        return True


class QuotaError(Exception):
    class Response:
        status = 403
    resp = Response()
    content = b'{"reason":"quotaExceeded"}'


class MonitorGoalTests(unittest.IsolatedAsyncioTestCase):
    async def _run_once_and_capture_sleep(self, hint):
        monitor = ChatMonitor(ResponseService({"items": [], "pollingIntervalMillis": hint}), EmptyAI(), {"poll_interval": 99})
        monitor.start_monitoring("chat", start_background=False)
        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)
            monitor.is_running = False

        with patch("services.chat_monitor.asyncio.sleep", fake_sleep):
            await monitor._monitor_loop()
        return sleeps

    async def test_api_poll_interval_is_used_and_tiny_hint_backs_off(self):
        self.assertEqual(await self._run_once_and_capture_sleep(8000), [8.0])
        self.assertEqual(await self._run_once_and_capture_sleep(2000), [10.0])

    async def test_stop_cancels_inflight_task_and_blocks_immediate_restart(self):
        started, release = threading.Event(), threading.Event()

        class BlockingService(ResponseService):
            def get_chat_messages(self, *_):
                started.set()
                release.wait(1)
                return {"items": []}

        monitor = ChatMonitor(BlockingService(), EmptyAI())
        with self.assertLogs("services.chat_monitor", level="INFO") as logs:
            self.assertTrue(monitor.start_monitoring("chat"))
            await asyncio.to_thread(started.wait, 0.5)
            task = monitor._monitor_task
            monitor.stop_monitoring()
            self.assertGreater(task.cancelling(), 0)
            self.assertFalse(monitor.is_running)
            self.assertFalse(monitor.start_monitoring("new-chat"))
            await task
        release.set()
        self.assertIsNone(monitor._monitor_task)
        self.assertTrue(any("Monitor task finished" in line for line in logs.output))

    async def test_quota_stops_without_retry_and_transient_error_retries(self):
        quota_service = ResponseService(error=QuotaError("quota"))
        quota_monitor = ChatMonitor(quota_service, EmptyAI())
        self.assertTrue(quota_monitor.start_monitoring("chat"))
        task = quota_monitor._monitor_task
        await task
        self.assertFalse(quota_monitor.is_running)
        self.assertEqual(quota_service.calls, 1)

        class Flaky(ResponseService):
            def get_chat_messages(self, *_):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("network timeout")
                return {"items": []}

        monitor = ChatMonitor(Flaky(), EmptyAI(), {"poll_interval": 9})
        monitor.start_monitoring("chat", start_background=False)
        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)
            if len(sleeps) == 2:
                monitor.is_running = False

        with patch("services.chat_monitor.asyncio.sleep", fake_sleep):
            await monitor._monitor_loop()
        self.assertEqual(sleeps[0], 1.0)
        self.assertEqual(monitor.youtube.calls, 2)


class InsertGoalTests(unittest.TestCase):
    def test_nonessential_inserts_cap_at_sixty_and_replies_continue(self):
        inserts = []

        class Request:
            def execute(self):
                inserts.append(1)
        class Messages:
            def insert(self, **_):
                return Request()
        class Client:
            def liveChatMessages(self):
                return Messages()

        service = object.__new__(YouTubeService)
        service.youtube = Client()
        service._nonessential_insert_count = 0
        service._nonessential_insert_cap = 60
        service._nonessential_warning_at = 48
        service._nonessential_warning_logged = False
        with self.assertLogs("services.youtube_service", level="WARNING") as logs:
            outcomes = [service.send_message("chat", "idle", message_kind="idle") for _ in range(61)]
        self.assertEqual(outcomes.count(True), 60)
        self.assertFalse(outcomes[-1])
        self.assertTrue(any("80%" in line for line in logs.output))
        self.assertTrue(service.send_message("chat", "direct reply", message_kind="reply"))
        self.assertEqual(len(inserts), 61)


class PersonaGoalTests(unittest.TestCase):
    def test_ten_varied_chat_samples_obey_persona_safety_contract(self):
        samples = [
            ("What game is this?", "It's tonight's chaos, apparently."),
            ("You're awful", "Keep it civil, please."),
            ("I just joined", "Welcome in—glad you made it."),
            ("What is the capital of France?", "Let's keep the chat on the stream, yeah?"),
            ("The stream is amazing", "Thanks. That was almost sweet."),
            ("Are you an AI?", "Yes, I'm an AI character for this stream."),
            ("SPAM SPAM SPAM", "Please slow down, chat."),
            ("I'm having a bad day", "That sounds rough; take it easy tonight."),
            ("Can you beat that boss?", "Give me one clean attempt first."),
            ("hello everyone", "Hm. Welcome in, everyone."),
        ]
        self.assertIn("Never use stage directions", RUKIYA_SYSTEM_PROMPT)
        forbidden = ("*", "uwu", "~", "idiot", "dumbass", "stupid")
        for user_message, simulated_model_reply in samples:
            with self.subTest(user_message=user_message):
                reply = validate_rukiya_response(simulated_model_reply)
                self.assertLessEqual(len(__import__("re").findall(r"[.!?]", reply)), 1)
                self.assertFalse(any(token in reply.lower() for token in forbidden))
                self.assertLessEqual(len(__import__("re").findall(r"[\U0001F300-\U0001FAFF]", reply)), 1)
