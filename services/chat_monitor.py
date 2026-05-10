"""Robust YouTube live chat monitor."""

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
    """Polls YouTube chat, notifies subscribers, and can send chat messages safely."""

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

        self._poll_interval = float(self._cfg("poll_interval", 2.0))
        self._send_cooldown = float(self._cfg("send_cooldown", 2.0))
        self._idle_chat_enabled = bool(self._cfg("idle_chat_enabled", True))
        self._idle_chat_interval = float(self._cfg("idle_chat_interval", 180))
        idle_messages = self._cfg("idle_chat_messages", ()) or ()
        self._idle_chat_messages = [
            m.strip() for m in idle_messages if isinstance(m, str) and m.strip()
        ]
        self._last_activity_at = time.monotonic()
        self._last_idle_message_at = 0.0

    def _cfg(self, key: str, default: Any = None) -> Any:
        if self.config is None:
            return default
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    def subscribe(self, callback: SubscriberType) -> None:
        if callback not in self.subscribers:
            self.subscribers.append(callback)
            name = getattr(callback, "__name__", repr(callback))
            logger.info("Subscriber added: %s", name)

    def unsubscribe(self, callback: SubscriberType) -> None:
        if callback in self.subscribers:
            self.subscribers.remove(callback)
            name = getattr(callback, "__name__", repr(callback))
            logger.info("Subscriber removed: %s", name)

    async def _notify_subscribers(self, message: str, author: str) -> None:
        for callback in list(self.subscribers):
            try:
                await callback(message, author)
            except Exception as exc:
                name = getattr(callback, "__name__", repr(callback))
                logger.exception("Error in subscriber %s: %s", name, exc)

    def start_monitoring(
        self,
        live_chat_id: str,
        video_id: Optional[str] = None,
        *,
        start_background: bool = True,
    ) -> None:
        self.live_chat_id = live_chat_id
        self.video_id = video_id
        self.is_running = True
        self.next_page_token = None
        self.processed_messages.clear()
        self._last_activity_at = time.monotonic()
        self._last_idle_message_at = 0.0
        logger.info("Started monitoring chat: %s", live_chat_id)

        if not start_background:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running asyncio loop; background monitoring not started")
            return

        if not self._monitor_task or self._monitor_task.done():
            self._monitor_task = loop.create_task(self._monitor_loop())
            logger.info("Background monitor loop started")

    def stop_monitoring(self) -> None:
        self.is_running = False
        self.live_chat_id = None
        self.video_id = None
        self.next_page_token = None
        self.processed_messages.clear()

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()

    def get_status(self) -> dict[str, Any]:
        cooldown = 0.0
        if hasattr(self.ai, "get_cooldown_remaining"):
            cooldown = self.ai.get_cooldown_remaining()

        return {
            "is_running": self.is_running,
            "live_chat_id": self.live_chat_id,
            "video_id": self.video_id,
            "processed_count": len(self.processed_messages),
            "ai_cooldown_remaining": cooldown,
            "subscribers_count": len(self.subscribers),
        }

    async def send_chat_message(self, text: str) -> bool:
        if not text:
            logger.debug("send_chat_message called with empty text")
            return False
        if not self.live_chat_id:
            logger.warning("send_chat_message called without a live_chat_id")
            return False

        try:
            result = await asyncio.to_thread(
                self.youtube.send_message,
                self.live_chat_id,
                text,
            )
            if result:
                self._last_activity_at = time.monotonic()
                logger.info("Sent chat message via YouTube service")
            else:
                logger.warning("youtube.send_message returned a falsy result")

            await asyncio.sleep(self._send_cooldown)
            return bool(result)
        except Exception as exc:
            logger.exception("Exception while sending chat message: %s", exc)
            return False

    async def send_chat_message_with_retry(
        self,
        text: str,
        retries: int = 1,
        retry_delay: float = 1.0,
    ) -> bool:
        max_attempts = 1 + max(0, int(retries))
        for attempt in range(max_attempts):
            if await self.send_chat_message(text):
                if attempt:
                    logger.info("send_chat_message succeeded on retry #%d", attempt)
                return True

            if attempt + 1 < max_attempts:
                logger.warning(
                    "send_chat_message failed; retrying %d/%d after %.1fs",
                    attempt + 1,
                    retries,
                    retry_delay,
                )
                await asyncio.sleep(retry_delay)

        return False

    async def process_messages(self) -> None:
        if not self.is_running or not self.live_chat_id:
            return

        try:
            response = await asyncio.to_thread(
                self.youtube.get_chat_messages,
                self.live_chat_id,
                self.next_page_token,
            )
            if not response:
                await self._maybe_send_idle_message()
                return

            self.next_page_token = response.get("nextPageToken")
            for item in response.get("items", []):
                snippet = item.get("snippet", {})
                message_id = item.get("id")
                author = snippet.get("authorDisplayName", "Unknown")
                message = snippet.get("displayMessage", "")

                if not message or not message_id or message_id in self.processed_messages:
                    continue

                self.processed_messages.add(message_id)
                self._last_activity_at = time.monotonic()
                await self._notify_subscribers(message, author)

            await self._maybe_send_idle_message()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Error processing messages: %s", exc)
            if "livechatid" in str(exc).lower() and "not found" in str(exc).lower():
                logger.warning("Live chat ended; stopping monitor")
                self.stop_monitoring()

    async def _maybe_send_idle_message(self) -> None:
        if not self._idle_chat_enabled or not self._idle_chat_messages:
            return
        if not self.live_chat_id or not self.is_running:
            return

        now = time.monotonic()
        if now - self._last_activity_at < self._idle_chat_interval:
            return
        if self._last_idle_message_at and now - self._last_idle_message_at < self._idle_chat_interval:
            return

        idle_text = random.choice(self._idle_chat_messages)
        if await self.send_chat_message(idle_text):
            self._last_idle_message_at = time.monotonic()
            logger.info("Idle chat message sent")

    async def _monitor_loop(self) -> None:
        try:
            while self.is_running:
                await self.process_messages()
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            logger.info("Monitor loop cancelled")
        except Exception as exc:
            logger.exception("Unexpected error in monitor loop: %s", exc)
            self.stop_monitoring()
