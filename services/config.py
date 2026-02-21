# services/config.py
"""Centralized configuration for YouTube/Discord bot with environment variable support"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Set, Optional


@dataclass
class Config:
    """Configuration class with defaults and environment-friendly fields"""

    # API keys and tokens
    discord_token: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    openrouter_model: str = "deepseek/deepseek-r1"
    openrouter_endpoint: str = "https://openrouter.ai/api/v1/chat/completions"

    # YouTube settings
    client_secrets_file: str = "client_secret.json"
    token_file: str = "token.json"
    video_id: str = ""

    # Bot behavior settings
    bot_name: str = "Rukiya"
    max_message_length: int = 250
    ai_cooldown: int = 5          # ⬇ lowered from 20 → 5s for live chat responsiveness
    poll_interval: int = 3        # ⬇ poll more frequently
    send_cooldown: float = 1.5
    chat_check_interval: int = 5

    # Sets for filtering and triggers
    bot_users: Set[str] = field(default_factory=set)
    banned_words: Set[str] = field(default_factory=set)
    ai_triggers: Set[str] = field(default_factory=set)

    def __post_init__(self):
        """Initialize default sets and load from environment variables"""

        # Bot usernames to ignore (won't trigger AI responses)
        if not self.bot_users:
            self.bot_users = {
                "nightbot", "streamelements", "streamlabs", "moobot",
                "rukiya", "rukia",  # bot's own names so it doesn't reply to itself
            }

        # Words that block AI responses
        if not self.banned_words:
            self.banned_words = {
                "spam", "scam", "fake", "stupid",
            }

        # Triggers — any of these appearing ANYWHERE in a message will invoke Rukiya
        # Covers typos, short tags, Hinglish variations, and @-mentions
        if not self.ai_triggers:
            self.ai_triggers = {
                # Direct name calls
                "rukiya", "rukia", "ruki",
                # @-mentions
                "@rukiya", "@rukia", "@ruki",
                # Greetings / calls
                "hey rukiya", "hi rukiya", "hello rukiya",
                "hey rukia", "hi rukia",
                "oi rukiya", "oi rukia",
                # Hinglish calls
                "rukiya kya", "rukia kya", "rukiya yaar",
                "arey rukiya", "arey rukia",
                # Common misspellings
                "rukia", "rukiya", "rukia chan", "rukiya chan",
            }

        # Load secrets from environment
        if not self.discord_token:
            self.discord_token = os.getenv("DISCORD_TOKEN")

        if not self.openrouter_api_key:
            self.openrouter_api_key = os.getenv("OPENROUTER_API_KEY")

        if not self.video_id:
            self.video_id = os.getenv("YOUTUBE_VIDEO_ID", "")

        # Override with environment variables
        self.openrouter_model = os.getenv("OPENROUTER_MODEL", self.openrouter_model)
        self.openrouter_endpoint = os.getenv("OPENROUTER_ENDPOINT", self.openrouter_endpoint)
        self.bot_name = os.getenv("BOT_NAME", self.bot_name)

        # Parse integer env vars safely
        try:
            self.ai_cooldown = int(os.getenv("AI_COOLDOWN", str(self.ai_cooldown)))
            self.max_message_length = int(os.getenv("MAX_MESSAGE_LENGTH", str(self.max_message_length)))
            self.poll_interval = int(os.getenv("POLL_INTERVAL", str(self.poll_interval)))
        except ValueError:
            pass

    def update_from_dict(self, data: dict) -> None:
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def update_from_obj(self, obj) -> None:
        for key in dir(obj):
            if not key.startswith('_') and hasattr(self, key):
                setattr(self, key, getattr(obj, key))
