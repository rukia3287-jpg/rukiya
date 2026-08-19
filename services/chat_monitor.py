"""YouTube live-chat monitor with quota-aware polling and shutdown control."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable, Optional, Union

logger = logging.getLogger(__name__)

ConfigType = Union[dict, object, None]
SubscriberType = Callable[[str, str], Awaitable[Any]]


class ChatMonitor:
    """Own exactly one background polling task at a time."""

    # A malformed/tiny server hint is more harmful than useful.  Valid server
    # intervals (including 8 seconds) are honored exactly; tiny values back off.
    MIN_SAFE_SERVER_POLL_SECONDS = 5.0
    TINY_HINT_BACKOFF_SECONDS = 10.0

    def __init__(self, youtube_service, ai_service, config: ConfigType = None):
        self.youtube = youtube_service
        self.ai = ai_service
        self.config = config
        self.is_running = False
        self.live_chat_id: Optional[str] = None
        self.next_page_token: Optional[str] = None
        self.video_id: Optional[str] = None
        self.processed_messages: set[str] = set()
        self.subscribers: list[SubscriberType] = []
        self._monitor_task: Optional[asyncio.Task] = None
        self._stopping = False
        self._poll_interval = float(self._cfg("poll_interval", 10.0))
        self._next_poll_delay = self._poll_interval
        self._send_cooldown = float(self._cfg("send_cooldown", 2.0))
        self._idle_chat_enabled = bool(self._cfg("idle_chat_enabled", True))
        self._idle_chat_interval = float(self._cfg("idle_chat_interval", 180))
        messages = self._cfg("idle_chat_messages", ()) or ()
        self._idle_chat_messages = [m.strip() for m in messages if isinstance(m, str) and m.strip()]
        self._last_activity_at = time.monotonic()
        self._last_idle_message_at = 0.0

    def _cfg(self, key: str, default: Any = None) -> Any:
        if self.config is None:
            return default
        return self.config.get(key, default) if isinstance(self.config, dict) else getattr(self.config, key, default)

    def subscribe(self, callback: SubscriberType) -> None:
        if callback not in self.subscribers:
            self.subscribers.append(callback)

    def unsubscribe(self, callback: SubscriberType) -> None:
        if callback in self.subscribers:
            self.subscribers.remove(callback)

    async def _notify_subscribers(self, message: str, author: str) -> None:
        for callback in list(self.subscribers):
            try:
                await callback(message, author)
            except Exception:
                logger.exception("Subscriber failed: %r", callback)

    def start_monitoring(self, live_chat_id: str, video_id: Optional[str] = None, *, start_background: bool = True) -> bool:
        """Start monitoring, or reject while the prior task is still winding down."""
        if self.is_running or self._stopping or (self._monitor_task and not self._monitor_task.done()):
            logger.warning("Start rejected: an existing monitor task is active or stopping")
            return False
        self.live_chat_id, self.video_id = live_chat_id, video_id
        self.is_running = True
        self.next_page_token = None
        self.processed_messages.clear()
        self._next_poll_delay = self._poll_interval
        self._last_activity_at, self._last_idle_message_at = time.monotonic(), 0.0
        logger.info("Started monitoring chat: %s", live_chat_id)
        if not start_background:
            return True
        try:
            self._monitor_task = asyncio.get_running_loop().create_task(self._monitor_loop(), name="youtube-chat-monitor")
        except RuntimeError:
            self.is_running = False
            logger.warning("No running asyncio loop; monitor was not started")
            return False
        logger.info("Background monitor loop started")
        return True

    def stop_monitoring(self) -> None:
        """Request cancellation; the loop's finally block logs confirmed completion."""
        task = self._monitor_task
        self.is_running = False
        self._stopping = bool(task and not task.done())
        if task and not task.done():
            task.cancel()
            logger.info("Monitor task cancellation requested")
        else:
            self._stopping = False
        self.live_chat_id = self.video_id = self.next_page_token = None
        self.processed_messages.clear()

    def get_status(self) -> dict[str, Any]:
        return {"is_running": self.is_running, "is_stopping": self._stopping, "live_chat_id": self.live_chat_id,
                "video_id": self.video_id, "processed_count": len(self.processed_messages),
                "ai_cooldown_remaining": self.ai.get_cooldown_remaining() if hasattr(self.ai, "get_cooldown_remaining") else 0,
                "subscribers_count": len(self.subscribers)}

    @staticmethod
    def _is_quota_exhausted(exc: Exception) -> bool:
        status = getattr(getattr(exc, "resp", None), "status", None) or getattr(exc, "status_code", None)
        content = getattr(exc, "content", b"")
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        return status == 403 and "quotaExceeded" in (str(exc) + str(content))

    def _set_poll_delay(self, response: dict[str, Any]) -> None:
        hint = response.get("pollingIntervalMillis")
        if hint is None:
            self._next_poll_delay = self._poll_interval
            return
        try:
            seconds = float(hint) / 1000.0
        except (TypeError, ValueError):
            logger.warning("Invalid pollingIntervalMillis=%r; using configured fallback", hint)
            self._next_poll_delay = self._poll_interval
            return
        self._next_poll_delay = seconds if seconds >= self.MIN_SAFE_SERVER_POLL_SECONDS else self.TINY_HINT_BACKOFF_SECONDS

    async def send_chat_message(self, text: str, *, message_kind: str = "reply") -> bool:
        if not text or not self.live_chat_id:
            return False
        try:
            sent = await asyncio.to_thread(self.youtube.send_message, self.live_chat_id, text, message_kind=message_kind)
            if sent:
                self._last_activity_at = time.monotonic()
            await asyncio.sleep(self._send_cooldown)
            return bool(sent)
        except Exception:
            logger.exception("Could not send chat message")
            return False

    async def send_chat_message_with_retry(self, text: str, retries: int = 1, retry_delay: float = 1.0, *, message_kind: str = "reply") -> bool:
        for attempt in range(retries + 1):
            if await self.send_chat_message(text, message_kind=message_kind):
                return True
            if attempt < retries:
                await asyncio.sleep(retry_delay)
        return False

    async def process_messages(self) -> None:
        if not self.is_running or not self.live_chat_id:
            return
        try:
            response = await asyncio.to_thread(self.youtube.get_chat_messages, self.live_chat_id, self.next_page_token)
            if not response:
                await self._maybe_send_idle_message()
                return
            self._set_poll_delay(response)
            self.next_page_token = response.get("nextPageToken")
            for item in response.get("items", []):
                snippet, message_id = item.get("snippet", {}), item.get("id")
                message, author = snippet.get("displayMessage", ""), snippet.get("authorDisplayName", "Unknown")
                if not message or not message_id or message_id in self.processed_messages:
                    continue
                self.processed_messages.add(message_id)
                self._last_activity_at = time.monotonic()
                await self._notify_subscribers(message, author)
            await self._maybe_send_idle_message()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._is_quota_exhausted(exc):
                logger.error("YouTube quota exhausted; monitoring stops without retry")
                self.stop_monitoring()
                # A task that cancels itself must reach an await point to
                # receive (and let _monitor_loop handle) CancelledError.
                await asyncio.sleep(0)
                return
            logger.warning("Polling failed (%s); retrying after exponential backoff", exc)
            raise

    async def _maybe_send_idle_message(self) -> None:
        if not (self._idle_chat_enabled and self._idle_chat_messages and self.live_chat_id and self.is_running):
            return
        now = time.monotonic()
        if now - self._last_activity_at < self._idle_chat_interval or (self._last_idle_message_at and now - self._last_idle_message_at < self._idle_chat_interval):
            return
        if await self.send_chat_message(random.choice(self._idle_chat_messages), message_kind="idle"):
            self._last_idle_message_at = time.monotonic()
            logger.info("Idle chat message sent")

    async def _monitor_loop(self) -> None:
        retries = 0
        try:
            while self.is_running:
                try:
                    await self.process_messages()
                    retries = 0
                    if self.is_running:
                        await asyncio.sleep(self._next_poll_delay)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    retries += 1
                    delay = min(60.0, 2.0 ** min(retries - 1, 5))
                    logger.info("Transient poll failure: retry %d in %.1fs", retries, delay)
                    await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.info("Monitor loop received cancellation")
        finally:
            self.is_running = False
            self._stopping = False
            self._monitor_task = None
            logger.info("Monitor task finished")
