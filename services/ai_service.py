import time
import logging
from typing import Optional
import google.generativeai as genai

logger = logging.getLogger(__name__)

class AIService:
    """Handles AI response generation"""

    def __init__(self, config):
        self.config = config
        self.model = None
        self.last_used = 0

        if config.gemini_api_key:
            try:
                genai.configure(api_key=config.gemini_api_key)
                self.model = genai.GenerativeModel('gemini-2.0-flash-exp')
                logger.info("AI service initialized")
            except Exception as e:
                logger.error(f"AI initialization failed: {e}")

    def can_respond(self) -> bool:
        """Check if AI can respond (cooldown check)"""
        return time.time() - self.last_used > self.config.ai_cooldown

    def should_respond(self, message: str, author: str) -> bool:
        """Check if bot should respond to message"""
        if not self.model or not self.can_respond():
            return False

        message_lower = message.lower()
        author_lower = author.lower()

        # Skip bot users
        if author_lower in self.config.bot_users:
            return False

        # Skip banned words
        if any(word in message_lower for word in self.config.banned_words):
            return False

        # Check triggers
        return any(trigger in message_lower for trigger in self.config.ai_triggers)

    def generate_response(self, message: str, author: str) -> Optional[str]:
        """Generate AI response"""
        if not self.should_respond(message, author):
            return None

        try:
            prompt = (
                f"User {author} said: {message}\n"
                f"Respond as Rukiya, a fun Hinglish YouTube chat bot. "
                f"Keep it under 12 words, be friendly and engaging. "
                f"Use emojis and Hinglish style."
            )

            response = self.model.generate_content(prompt)

            if response.text:
                self.last_used = time.time()
                generated_text = response.text.strip()

                # Truncate if too long
                if len(generated_text) > self.config.max_message_length:
                    generated_text = generated_text[:self.config.max_message_length - 3] + "..."

                logger.info(f"AI response generated for {author}: {generated_text}")
                return generated_text

        except Exception as e:
            logger.error(f"AI response generation failed: {e}")

        return None

    def get_cooldown_remaining(self) -> float:
        """Get remaining cooldown time"""
        elapsed = time.time() - self.last_used
        remaining = max(0, self.config.ai_cooldown - elapsed)
        return remaining