"""YouTube live-chat monitor with quota-aware polling, bounded LRU deduplication, and session lifecycle."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import inspect
import logging
import random
import time
from typing import Any, Awaitable, Callable, Optional, Union

from services.youtube_service import YouTubeApiError, YouTubeService, classify_youtube_error

logger = logging.getLogger(__name__)

ConfigType = Union[dict, object, None]
# Subscribers are called as callback(message, author) or, if they accept a third
# positional argument, callback(message, author, author_details).
SubscriberType = Callable[..., Awaitable[Any]]


class ChatMonitor:
    """Owns exactly one background polling task at a time for YouTube live chat."""

    # A malformed/tiny server hint is more harmful than useful. Valid server
    # intervals (including 8 seconds) are honored exactly; tiny values back off.
    MIN_SAFE_SERVER_POLL_SECONDS = 5.0
    TINY_HINT_BACKOFF_SECONDS = 10.0

    def __init__(self, youtube_service, ai_service, config: ConfigType = None, orchestrator: Optional[Any] = None):
        self.youtube = youtube_service
        self.ai = ai_service
        self.config = config
        self.orchestrator = orchestrator
        self.is_running = False
        self.live_chat_id: Optional[str] = None
        self.next_page_token: Optional[str] = None
        self.video_id: Optional[str] = None
        self.stream_session_id: Optional[str] = None

        # Bounded LRU cache for processed messages to prevent memory leaks (max 5000)
        self.processed_messages_max = int(self._cfg("processed_messages_max", 5000))
        self.processed_messages: OrderedDict[str, float] = OrderedDict()

        self.subscribers: list[SubscriberType] = []
        self._monitor_task: Optional[asyncio.Task] = None
        self._stopping = False
        self._poll_interval = float(self._cfg("poll_interval", 10.0))
        self._next_poll_delay = self._poll_interval
        self._send_cooldown = float(self._cfg("send_cooldown", 2.0))
        self._idle_chat_enabled = bool(self._cfg("idle_chat_enabled", True))
        self._idle_chat_interval = float(self._cfg("rate_limit_idle_interval", self._cfg("idle_chat_interval", 180.0)))
        messages = self._cfg("idle_chat_messages", ()) or ()
        self._idle_chat_messages = [m.strip() for m in messages if isinstance(m, str) and m.strip()]
        if not self._idle_chat_messages:
            self._idle_chat_messages = [
                "Enjoying the stream? Drop a comment in chat!",
                "Feel free to ask questions or chat with Rukiya!",
                "Stream is in full swing—what's everyone up to today?"
            ]
        self._last_activity_at = time.monotonic()
        self._last_idle_message_at = 0.0
        self.last_error: Optional[str] = None  # "<poll|send>:<kind>:<reason>", shown by /yt_status
        self._last_error_at: Optional[float] = None

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

    @staticmethod
    def _accepts_author_details(callback: SubscriberType) -> bool:
        try:
            params = list(inspect.signature(callback).parameters.values())
        except (TypeError, ValueError):
            return False
        positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        return len(positional) >= 3 or any(p.kind == p.VAR_POSITIONAL for p in params)

    async def _notify_subscribers(self, message: str, author: str, author_details: Optional[dict] = None) -> None:
        for callback in list(self.subscribers):
            try:
                if author_details is not None and self._accepts_author_details(callback):
                    await callback(message, author, author_details)
                else:
                    await callback(message, author)
            except Exception:
                logger.exception(
                    "Subscriber failed: %r for author=%s message=%r",
                    callback,
                    author,
                    message[:120],
                )

    def start_monitoring(self, live_chat_id: str, video_id: Optional[str] = None, *, start_background: bool = True) -> bool:
        """Start monitoring, or reject while the prior task is still winding down."""
        if self.is_running or self._stopping or (self._monitor_task and not self._monitor_task.done()):
            logger.warning("Start rejected: an existing monitor task is active or stopping")
            return False
        self.live_chat_id, self.video_id = live_chat_id, video_id
        self.last_error = None
        self.is_running = True
        self.next_page_token = None
        self.processed_messages.clear()
        self._next_poll_delay = self._poll_interval
        self._last_activity_at, self._last_idle_message_at = time.monotonic(), 0.0

        # Stream session initialization
        vid = video_id or "unknown"
        self.stream_session_id = f"stream_{vid}_{int(time.time())}"
        mem_svc = getattr(self.orchestrator, "memory_service", None)
        if mem_svc:
            mem_svc.start_stream_session(vid, session_id=self.stream_session_id)

        logger.info("Started monitoring chat: %s (session %s)", live_chat_id, self.stream_session_id)
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

        # End stream session in memory service
        mem_svc = getattr(self.orchestrator, "memory_service", None)
        if mem_svc and self.stream_session_id:
            mem_svc.end_stream_session(self.stream_session_id)

        if task and not task.done():
            task.cancel()
            logger.info("Monitor task cancellation requested")
        else:
            self._stopping = False
        self.live_chat_id = self.video_id = self.next_page_token = self.stream_session_id = None
        self.processed_messages.clear()

    def get_status(self) -> dict[str, Any]:
        return {
            "is_running": self.is_running,
            "is_stopping": self._stopping,
            "live_chat_id": self.live_chat_id,
            "video_id": self.video_id,
            "session_id": self.stream_session_id,
            "processed_count": len(self.processed_messages),
            "ai_cooldown_remaining": self.ai.get_cooldown_remaining() if hasattr(self.ai, "get_cooldown_remaining") else 0,
            "subscribers_count": len(self.subscribers),
            "last_error": self.last_error,
            "last_error_age_s": round(time.time() - self._last_error_at, 1) if self._last_error_at else None,
        }

    def _record_api_error(self, source: str, err: YouTubeApiError) -> None:
        self.last_error = f"{source}:{err.kind}:{err.reason or err.status or '-'}"
        self._last_error_at = time.time()

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
        if not text or not self.live_chat_id or not self.is_running:
            return False

        rate_limiter = getattr(self.orchestrator, "rate_limiter", None)
        if rate_limiter and message_kind == "reply":
            limit_result = rate_limiter.allow("youtube_send")
            if not limit_result.allowed:
                logger.warning(
                    "YouTube send rate-limited; wait %.1fs",
                    limit_result.wait_time,
                )
                return False

        try:
            if isinstance(self.youtube, YouTubeService):
                sent, err = await asyncio.to_thread(
                    self.youtube.send_message_detailed, self.live_chat_id, text, message_kind=message_kind
                )
            else:
                sent = await asyncio.to_thread(self.youtube.send_message, self.live_chat_id, text, message_kind=message_kind)
                err = getattr(self.youtube, "last_send_error", None)
        except Exception:
            logger.exception("Could not send chat message")
            return False
        if sent:
            self._last_activity_at = time.monotonic()
        else:
            if isinstance(err, YouTubeApiError):
                self._record_api_error("send", err)
                if err.stops_monitoring:
                    # e.g. the live chat ended or quota/auth failed: no later send can work.
                    logger.error("YouTube send failed permanently (%s); stopping chat monitor", self.last_error)
                    self.stop_monitoring()
                    return False
        await asyncio.sleep(self._send_cooldown)
        return bool(sent)

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
                if not self.is_running:
                    break  # stopped mid-batch (e.g. a reply found the chat ended)
                snippet, message_id = item.get("snippet", {}), item.get("id")
                author_details = item.get("authorDetails", {}) or {}
                message = snippet.get("displayMessage", "") or snippet.get("textMessageDetails", {}).get("messageText", "")
                author = author_details.get("displayName", "Unknown")
                if not message or not message_id or message_id in self.processed_messages:
                    continue

                # Deliver first, then mark as processed. If every subscriber fails,
                # the message remains eligible for a later poll instead of being lost.
                self._last_activity_at = time.monotonic()
                await self._notify_subscribers(message, author, author_details)

                self.processed_messages[message_id] = time.monotonic()
                if len(self.processed_messages) > self.processed_messages_max:
                    self.processed_messages.popitem(last=False)
            await self._maybe_send_idle_message()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            err = classify_youtube_error(exc)
            self._record_api_error("poll", err)
            # A 403 on polling (e.g. a members-only chat) will not fix itself; on sends it
            # can be a temporary moderator timeout, so only polling stops on it.
            if err.stops_monitoring or err.kind == "forbidden":
                if err.kind == "quota":
                    logger.error("YouTube quota exhausted; monitoring stops without retry")
                else:
                    logger.error("YouTube polling failed permanently (%s); monitoring stops without retry", self.last_error)
                self.stop_monitoring()
                # A task that cancels itself must reach an await point to
                # receive (and let _monitor_loop handle) CancelledError.
                await asyncio.sleep(0)
                return
            if err.kind == "invalid":
                self.next_page_token = None  # e.g. pageTokenInvalid: start over instead of repeating it
            logger.warning("Polling failed (%s); retrying after exponential backoff", exc)
            raise

    async def _maybe_send_idle_message(self) -> None:
        if not (self._idle_chat_enabled and self._idle_chat_messages and self.live_chat_id and self.is_running):
            return
        now = time.monotonic()
        if now - self._last_activity_at < self._idle_chat_interval or (self._last_idle_message_at and now - self._last_idle_message_at < self._idle_chat_interval):
            return

        # Check rate limiter for idle chat budget if limiter exists
        rate_limiter = getattr(self.orchestrator, "rate_limiter", None)
        if rate_limiter:
            lim_res = rate_limiter.allow("idle_chat")
            if not lim_res.allowed:
                return

        idle_msg = random.choice(self._idle_chat_messages)
        if await self.send_chat_message(idle_msg, message_kind="idle"):
            self._last_idle_message_at = time.monotonic()
            logger.info("Idle chat message sent: '%s'", idle_msg)

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
