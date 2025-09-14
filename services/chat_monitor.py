import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class ChatMonitor:
    """Monitors YouTube chat"""

    def __init__(self, youtube_service, ai_service, config):
        self.youtube = youtube_service
        self.ai = ai_service
        self.config = config
        self.is_running = False
        self.live_chat_id = None
        self.next_page_token = None
        self.video_id = None
        self.processed_messages = set()

    def start_monitoring(self, live_chat_id: str, video_id: str = None):
        """Start monitoring chat"""
        self.live_chat_id = live_chat_id
        self.video_id = video_id
        self.is_running = True
        self.next_page_token = None
        self.processed_messages.clear()
        logger.info(f"Started monitoring chat: {live_chat_id}")

    def stop_monitoring(self):
        """Stop monitoring chat"""
        self.is_running = False
        self.live_chat_id = None
        self.video_id = None
        self.next_page_token = None
        self.processed_messages.clear()
        logger.info("Stopped monitoring chat")

    def get_status(self) -> dict:
        """Get current monitoring status"""
        return {
            "is_running": self.is_running,
            "live_chat_id": self.live_chat_id,
            "video_id": self.video_id,
            "processed_count": len(self.processed_messages),
            "ai_cooldown_remaining": self.ai.get_cooldown_remaining()
        }

    async def process_messages(self):
        """Process chat messages"""
        if not self.is_running or not self.live_chat_id:
            return

        try:
            response = self.youtube.get_chat_messages(
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

                # Skip if already processed or empty
                if not message or message_id in self.processed_messages:
                    continue

                self.processed_messages.add(message_id)

                # Generate AI response
                ai_response = self.ai.generate_response(message, author)
                if ai_response:
                    success = self.youtube.send_message(self.live_chat_id, ai_response)
                    if success:
                        messages_processed += 1
                        # Rate limiting to avoid spam
                        await asyncio.sleep(2)

            if messages_processed > 0:
                logger.info(f"Processed {messages_processed} messages")

        except Exception as e:
            logger.error(f"Error processing messages: {e}")
            # If there's a critical error, we might want to stop monitoring
            if "liveChatId" in str(e) and "not found" in str(e):
                logger.warning("Live chat ended, stopping monitoring")
                self.stop_monitoring()