from dataclasses import dataclass
from typing import Set

@dataclass
class Config:
    """Configuration class with validation"""
    discord_token: str
    gemini_api_key: str
    client_secrets_file: str = "client_secret.json"
    token_file: str = "token.json"

    bot_users: Set[str] = None
    banned_words: Set[str] = None
    ai_triggers: Set[str] = None
    max_message_length: int = 150
    ai_cooldown: int = 20
    chat_check_interval: int = 10

    def __post_init__(self):
        self.bot_users = {
            "nightbot", "streamelements", "rukia", 
            "streamlabs", "rukiya", "moobot"
        }
        self.banned_words = {
            "spam", "scam", "fake", "stupid"
        }
        self.ai_triggers = {
            "rukiya", "ru", "@rukiya", "@rukia", 
            "hey rukiya", "hi rukiya"
        }

        # Validate required fields
        if not self.discord_token:
            raise ValueError("DISCORD_TOKEN is required")
        if not self.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is required")
