# services/config.py
from dataclasses import dataclass
from typing import Set, Optional
import os

@dataclass
class Config:
    """Configuration class with defaults and environment-friendly fields"""
    discord_token: Optional[str] = None
    openrouter_api_key: Optional[str] = None
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

        # Optionally pick up OPENROUTER_API_KEY from environment if not provided
        if not self.openrouter_api_key:
            self.openrouter_api_key = os.getenv("OPENROUTER_API_KEY")

        # Do not raise hard error here; allow running with AI disabled
        if not self.openrouter_api_key:
            # logger can be used by caller to warn; keep silent here
            pass
