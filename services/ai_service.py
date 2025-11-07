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
        self.request_count = 0
        self.request_window_start = time.time()

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

    def _retry_with_backoff(self, prompt: str, max_retries: int = 3) -> Optional[str]:
        """Retry API call with exponential backoff"""
        for attempt in range(max_retries):
            try:
                response = self.model.generate_content(prompt)
                if response.text:
                    return response.text.strip()
            except Exception as e:
                error_msg = str(e)
                
                # Check if it's a 429 error
                if "429" in error_msg or "Resource exhausted" in error_msg:
                    if attempt < max_retries - 1:
                        # Exponential backoff: 1s, 2s, 4s
                        wait_time = 2 ** attempt
                        logger.warning(f"Rate limit hit, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"Rate limit exceeded after {max_retries} attempts")
                        return None
                else:
                    # For other errors, don't retry
                    logger.error(f"AI response generation failed: {e}")
                    return None
        
        return None

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

            generated_text = self._retry_with_backoff(prompt)

            if generated_text:
                self.last_used = time.time()

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
