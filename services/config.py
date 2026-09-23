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
    poll_interval: int = 10       # ⬆ set to 10s default to conserve YouTube API quota
    send_cooldown: float = 1.5
    chat_check_interval: int = 5

    # V2 Architecture settings
    db_path: str = "rukiya_memory.db"
    max_memory_per_user: int = 30
    max_stream_memory: int = 20
    max_context_messages: int = 8
    memory_decay_days: float = 30.0
    response_threshold: float = 0.50
    anti_repeat_threshold: float = 0.70
    processed_messages_max: int = 5000
    rate_limit_global_capacity: int = 10
    rate_limit_user_capacity: int = 2
    rate_limit_idle_interval: float = 180.0
    rate_limit_idle_capacity: int = 1
    rate_limit_discord_capacity: int = 5
    idle_chat_enabled: bool = True

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
        self.db_path = os.getenv("DB_PATH", self.db_path)

        # Parse integer env vars safely
        try:
            self.ai_cooldown = int(os.getenv("AI_COOLDOWN", str(self.ai_cooldown)))
            self.max_message_length = int(os.getenv("MAX_MESSAGE_LENGTH", str(self.max_message_length)))
            self.poll_interval = int(os.getenv("POLL_INTERVAL", str(self.poll_interval)))
            self.max_memory_per_user = int(os.getenv("MAX_MEMORY_PER_USER", str(self.max_memory_per_user)))
            self.max_stream_memory = int(os.getenv("MAX_STREAM_MEMORY", str(self.max_stream_memory)))
            self.max_context_messages = int(os.getenv("MAX_CONTEXT_MESSAGES", str(self.max_context_messages)))
            self.processed_messages_max = int(os.getenv("PROCESSED_MESSAGES_MAX", str(self.processed_messages_max)))
            self.rate_limit_global_capacity = int(os.getenv("RATE_LIMIT_GLOBAL_CAPACITY", str(self.rate_limit_global_capacity)))
            self.rate_limit_user_capacity = int(os.getenv("RATE_LIMIT_USER_CAPACITY", str(self.rate_limit_user_capacity)))
        except ValueError:
            pass

        # Parse float env vars safely
        try:
            self.memory_decay_days = float(os.getenv("MEMORY_DECAY_DAYS", str(self.memory_decay_days)))
            self.response_threshold = float(os.getenv("RESPONSE_THRESHOLD", str(self.response_threshold)))
            self.anti_repeat_threshold = float(os.getenv("ANTI_REPEAT_THRESHOLD", str(self.anti_repeat_threshold)))
            self.rate_limit_idle_interval = float(os.getenv("IDLE_CHAT_INTERVAL", str(self.rate_limit_idle_interval)))
        except ValueError:
            pass

        idle_enabled_env = os.getenv("IDLE_CHAT_ENABLED")
        if idle_enabled_env is not None:
            self.idle_chat_enabled = idle_enabled_env.lower() in ("1", "true", "yes")

    def update_from_dict(self, data: dict) -> None:
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def update_from_obj(self, obj) -> None:
        for key in dir(obj):
            if not key.startswith('_') and hasattr(self, key):
                setattr(self, key, getattr(obj, key))
