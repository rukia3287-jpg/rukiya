# services/chat_monitor.py
import asyncio
import logging
from typing import Optional, Callable, List, Dict, Any

logger = logging.getLogger(__name__)

class ChatMonitor:
    """Monitors YouTube chat with pub-sub pattern (async-friendly)"""

    def __init__(self, youtube_service, ai_service, config):
        self.youtube = youtube_service
        self.ai = ai_service
        self.config = config
        self.is_running = False
        self.live_chat_id = None
        self.next_page_token = None
        self.video_id = None
        self.processed_messages = set()

        # Pub-sub pattern for message subscribers
        self.subscribers: List[Callable[[str, str], Any]] = []

    def subscribe(self, callback: Callable):
        """Subscribe to chat messages (callback should be async: async def cb(msg, author))"""
        if callback not in self.subscribers:
            self.subscribers.append(callback)
            logger.info(f"New subscriber added: {getattr(callback, '__name__', repr(callback))}")

    def unsubscribe(self, callback: Callable):
        if callback in self.subscribers:
            self.subscribers.remove(callback)
            logger.info(f"Subscriber removed: {getattr(callback, '__name__', repr(callback))}")

    async def _notify_subscribers(self, message: str, author: str):
        for callback in list(self.subscribers):
            try:
                await callback(message, author)
            except Exception as e:
                logger.error(f"Error in subscriber {getattr(callback,'__name__', repr(callback))}: {e}")

    def start_monitoring(self, live_chat_id: str, video_id: str = None):
        self.live_chat_id = live_chat_id
        self.video_id = video_id
        self.is_running = True
        self.next_page_token = None
        self.processed_messages.clear()
        logger.info(f"Started monitoring chat: {live_chat_id}")

    def stop_monitoring(self):
        self.is_running = False
        self.live_chat_id = None
        self.video_id = None
        self.next_page_token = None
        self.processed_messages.clear()
        logger.info("Stopped monitoring chat")

    def get_status(self) -> dict:
        return {
            "is_running": self.is_running,
            "live_chat_id": self.live_chat_id,
            "video_id": self.video_id,
            "processed_count": len(self.processed_messages),
            "ai_cooldown_remaining": self.ai.get_cooldown_remaining() if hasattr(self.ai, "get_cooldown_remaining") else 0,
            "subscribers_count": len(self.subscribers)
        }

    async def process_messages(self):
        """Async-friendly message polling handler: call this regularly (e.g. from an async loop)"""
        if not self.is_running or not self.live_chat_id:
            return

        try:
            # Run the blocking YouTube call in a thread
            response = await asyncio.to_thread(
                self.youtube.get_chat_messages,
                self.live_chat_id,
                self.next_page_token
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

                # Generate AI response (await; AI service is async)
                try:
                    ai_response = await self.ai.generate_response(message, author)
                except Exception as e:
                    logger.error(f"AI generation error for message {message_id}: {e}")
                    ai_response = None

                if ai_response:
                    # Send message via YouTube API in thread to avoid blocking loop
                    success = await asyncio.to_thread(self.youtube.send_message, self.live_chat_id, ai_response)
                    if success:
                        messages_processed += 1
                        # simple rate-limit pause
                        await asyncio.sleep(2)

            if messages_processed > 0:
                logger.info(f"Processed {messages_processed} messages")

        except Exception as e:
            logger.exception(f"Error processing messages: {e}")
            # If the live chat ended, stop
            if "liveChatId" in str(e).lower() and "not found" in str(e).lower():
                logger.warning("Live chat ended, stopping monitoring")
                self.stop_monitoring()
