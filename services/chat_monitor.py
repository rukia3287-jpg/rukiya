# services/chat_monitor.py
import asyncio
import logging
from typing import Optional, Callable, List, Any, Awaitable

logger = logging.getLogger(__name__)


class ChatMonitor:
    """Monitors YouTube chat with pub-sub pattern (async-friendly)."""

    def __init__(self, youtube_service, ai_service, config: Optional[dict] = None):
        self.youtube = youtube_service
        self.ai = ai_service
        self.config = config or {}
        self.is_running = False
        self.live_chat_id: Optional[str] = None
        self.next_page_token: Optional[str] = None
        self.video_id: Optional[str] = None
        self.processed_messages = set()

        # Pub-sub pattern for message subscribers. Callbacks should be async: async def cb(msg, author)
        self.subscribers: List[Callable[[str, str], Awaitable[Any]]] = []

        # Background loop control
        self._monitor_task: Optional[asyncio.Task] = None
        self._poll_interval = float(self.config.get("poll_interval", 2.0))
        self._send_cooldown = float(self.config.get("send_cooldown", 2.0))

    # -----------------------
    # Subscription management
    # -----------------------
    def subscribe(self, callback: Callable[[str, str], Awaitable[Any]]):
        """Subscribe to chat messages (callback should be async: async def cb(msg, author))."""
        if callback not in self.subscribers:
            self.subscribers.append(callback)
            name = getattr(callback, "__name__", repr(callback))
            logger.info(f"New subscriber added: {name}")

    def unsubscribe(self, callback: Callable[[str, str], Awaitable[Any]]):
        if callback in self.subscribers:
            self.subscribers.remove(callback)
            name = getattr(callback, "__name__", repr(callback))
            logger.info(f"Subscriber removed: {name}")

    async def _notify_subscribers(self, message: str, author: str):
        for callback in list(self.subscribers):
            try:
                await callback(message, author)
            except Exception as e:
                name = getattr(callback, "__name__", repr(callback))
                logger.error(f"Error in subscriber {name}: {e}")

    # -----------------------
    # Monitoring control
    # -----------------------
    def start_monitoring(self, live_chat_id: str, video_id: Optional[str] = None, *, start_background: bool = True):
        """
        Begin monitoring a live chat. If start_background is True, spawn a background task
        that runs the monitor loop.
        """
        self.live_chat_id = live_chat_id
        self.video_id = video_id
        self.is_running = True
        self.next_page_token = None
        self.processed_messages.clear()
        logger.info(f"Started monitoring chat: {live_chat_id}")

        if start_background:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop (e.g., called from sync context) - create a loop-compatible task later
                loop = None

            if loop:
                if not self._monitor_task or self._monitor_task.done():
                    self._monitor_task = loop.create_task(self._monitor_loop())
                    logger.info("Background monitor loop started")
            else:
                logger.warning("No running asyncio loop - background monitoring not started")

    def stop_monitoring(self):
        """Stop monitoring and cancel background task if present."""
        self.is_running = False
        self.live_chat_id = None
        self.video_id = None
        self.next_page_token = None
        self.processed_messages.clear()

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            logger.info("Background monitor task cancelled")
        self._monitor_task = None
        logger.info("Stopped monitoring chat")

    def get_status(self) -> dict:
        return {
            "is_running": self.is_running,
            "live_chat_id": self.live_chat_id,
            "video_id": self.video_id,
            "processed_count": len(self.processed_messages),
            "ai_cooldown_remaining": self.ai.get_cooldown_remaining() if hasattr(self.ai, "get_cooldown_remaining") else 0,
            "subscribers_count": len(self.subscribers),
        }

    # -----------------------
    # Message sending helpers
    # -----------------------
    async def send_chat_message(self, text: str) -> bool:
        """
        Non-blocking wrapper to send a chat message using the injected youtube service.
        Returns True on success, False on failure.
        """
        if not text:
            logger.debug("send_chat_message called with empty text")
            return False

        if not self.live_chat_id:
            logger.warning("send_chat_message called but no live_chat_id is set")
            return False

        try:
            # youtube.send_message is likely blocking -> run in a thread
            result = await asyncio.to_thread(self.youtube.send_message, self.live_chat_id, text)
            if result:
                logger.info("Sent chat message via youtube service")
            else:
                logger.warning("youtube.send_message returned falsy result")
            # small cooldown to avoid rapid-fire sending
            await asyncio.sleep(self._send_cooldown)
            return bool(result)
        except Exception as exc:
            logger.exception(f"Exception while sending chat message: {exc}")
            return False

    async def send_chat_message_with_retry(self, text: str, retries: int = 1, retry_delay: float = 1.0) -> bool:
        """
        Retry wrapper around send_chat_message for transient failures.
        retries: number of retries after the first attempt (so retries=1 => up to 2 attempts).
        """
        attempt = 0
        max_attempts = 1 + max(0, int(retries))
        while attempt < max_attempts:
            ok = await self.send_chat_message(text)
            if ok:
                if attempt > 0:
                    logger.info(f"send_chat_message succeeded on retry #{attempt}")
                return True
            attempt += 1
            if attempt < max_attempts:
                logger.warning(f"send_chat_message failed; retrying {attempt}/{retries} after {retry_delay}s")
                await asyncio.sleep(retry_delay)
        logger.error("send_chat_message_with_retry exhausted attempts and failed")
        return False

    # -----------------------
    # Message processing loop
    # -----------------------
    async def process_messages(self):
        """
        One-shot: poll YouTube messages, notify subscribers, generate AI responses,
        and send responses back through youtube service. Safe to call repeatedly.
        """
        if not self.is_running or not self.live_chat_id:
            return

        try:
            # call blocking network code in thread to avoid blocking event loop
            response = await asyncio.to_thread(
                self.youtube.get_chat_messages,
                self.live_chat_id,
                self.next_page_token,
            )

            if not response:
                return

            self.next_page_token = response.get("nextPageToken")
            messages_processed = 0

            for item in response.get("items", []):
                snippet = item.get("snippet", {})
                message_id = item.get("id")
                author = snippet.get("authorDisplayName", "Unknown")
                message = snippet.get("displayMessage", "")

                if not message or not message_id or message_id in self.processed_messages:
                    continue

                self.processed_messages.add(message_id)

                # Notify subscribers (welcome messages, logging, etc.)
                await self._notify_subscribers(message, author)

                # Generate AI response (AI service expected to be async)
                ai_response = None
                try:
                    # if ai.generate_response is blocking, it should be wrapped by the ai implementation
                    ai_response = await self.ai.generate_response(message, author)
                except Exception as e:
                    logger.error(f"AI generation error for message {message_id}: {e}")
                    ai_response = None

                if ai_response:
                    # send via wrapper which runs blocking code in thread
                    success = await self.send_chat_message_with_retry(ai_response, retries=1, retry_delay=1.0)
                    if success:
                        messages_processed += 1

            if messages_processed > 0:
                logger.info(f"Processed and responded to {messages_processed} messages")

        except asyncio.CancelledError:
            # allow task cancellation to propagate for graceful shutdown
            raise
        except Exception as e:
            logger.exception(f"Error processing messages: {e}")
            # If the live chat ended, stop monitoring (best-effort detection)
            if "livechatid" in str(e).lower() and "not found" in str(e).lower():
                logger.warning("Live chat ended, stopping monitoring")
                self.stop_monitoring()

    async def _monitor_loop(self):
        """
        Background loop that repeatedly calls process_messages while `is_running`.
        Cancels gracefully when stop_monitoring() is called or the task is cancelled.
        """
        try:
            while self.is_running:
                await self.process_messages()
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            logger.info("Monitor loop cancelled")
            return
        except Exception as e:
            logger.exception(f"Unexpected error in monitor loop: {e}")
            self.stop_monitoring()
