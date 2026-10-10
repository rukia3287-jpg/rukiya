# services/config.py
"""Centralized configuration for YouTube/Discord bot with environment variable support"""
from __future__ import annotations
import logging
import os
from dataclasses import dataclass, field
from typing import Set, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)


@dataclass
class Config:
    """Configuration class with defaults and environment-friendly fields"""

    # API keys and tokens
    discord_token: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    openrouter_model: str = "deepseek/deepseek-r1"
    openrouter_endpoint: str = "https://openrouter.ai/api/v1/chat/completions"

    # Optional JEV adaptive conversation decisions (paid API; disabled by default)
    jev_api_key: Optional[str] = None
    jev_conversation_mode: str = "disabled"  # disabled | shadow | active
    jev_model: str = "jev-1.13"
    jev_api_endpoint: str = "https://jev-ai.org/api/v1/systemone/"
    jev_timeout: float = 3.0
    jev_max_calls_per_minute: int = 12
    jev_respond_threshold: float = 0.72
    jev_ignore_threshold: float = 0.20

    # Gemini & AI Engine settings
    gemini_api_key: Optional[str] = None
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_search_enabled: bool = True
    gemini_search_daily_limit: int = 450
    gemini_search_cache_ttl: int = 300

    ai_max_repair_attempts: int = 2
    ai_max_planner_steps: int = 4
    max_ai_concurrency: int = 3
    max_search_concurrency: int = 2

    openrouter_timeout: float = 20.0
    gemini_timeout: float = 20.0
    search_timeout: float = 15.0
    critic_timeout: float = 15.0
    repair_timeout: float = 15.0

    # YouTube settings
    client_secrets_file: str = "client_secret.json"
    token_file: str = "token.json"
    video_id: str = ""

    # Bot behavior settings
    bot_name: str = "Rukiya"
    ai_temperature: float = 0.85
    ai_reasoning_enabled: bool = True
    ai_reasoning_effort: str = "medium"
    ai_max_completion_tokens: int = 320
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

        if not self.gemini_api_key:
            self.gemini_api_key = os.getenv("GEMINI_API_KEY")

        if not self.jev_api_key:
            self.jev_api_key = os.getenv("JEV_API_KEY")
        self.jev_conversation_mode = os.getenv(
            "JEV_CONVERSATION_MODE", self.jev_conversation_mode
        ).lower().strip()
        if self.jev_conversation_mode not in {"disabled", "shadow", "active"}:
            logger.warning("Ignoring invalid JEV_CONVERSATION_MODE; JEV is disabled")
            self.jev_conversation_mode = "disabled"
        self.jev_model = os.getenv("JEV_MODEL", self.jev_model).strip() or "jev-1.13"
        self.jev_api_endpoint = os.getenv("JEV_API_ENDPOINT", self.jev_api_endpoint).strip() or "https://jev-ai.org/api/v1/systemone/"

        if not self.video_id:
            self.video_id = os.getenv("YOUTUBE_VIDEO_ID", "")

        # Override with environment variables
        self.openrouter_model = os.getenv("OPENROUTER_MODEL", self.openrouter_model)
        self.openrouter_endpoint = os.getenv("OPENROUTER_ENDPOINT", self.openrouter_endpoint)
        self.gemini_model = os.getenv("GEMINI_MODEL", self.gemini_model)

        gemini_search_env = os.getenv("GEMINI_SEARCH_ENABLED")
        if gemini_search_env is not None:
            self.gemini_search_enabled = gemini_search_env.lower() in ("1", "true", "yes")

        self.bot_name = os.getenv("BOT_NAME", self.bot_name)
        # Shared generation temperature. RUKIYA_TEMP is the character-chat override,
        # with AI_TEMPERATURE kept as a generic fallback.
        try:
            self.ai_temperature = float(os.getenv("RUKIYA_TEMP", os.getenv("AI_TEMPERATURE", str(self.ai_temperature))))
            self.ai_temperature = min(2.0, max(0.0, self.ai_temperature))
        except ValueError:
            pass
        self.db_path = os.getenv("DB_PATH", self.db_path)

        # Parse integer env vars one at a time, so a bad value only affects itself
        self.ai_cooldown = self._env_number("AI_COOLDOWN", self.ai_cooldown, int)
        self.max_message_length = self._env_number("MAX_MESSAGE_LENGTH", self.max_message_length, int)
        self.poll_interval = self._env_number("POLL_INTERVAL", self.poll_interval, int)
        self.max_memory_per_user = self._env_number("MAX_MEMORY_PER_USER", self.max_memory_per_user, int)
        self.max_stream_memory = self._env_number("MAX_STREAM_MEMORY", self.max_stream_memory, int)
        self.max_context_messages = self._env_number("MAX_CONTEXT_MESSAGES", self.max_context_messages, int)
        self.processed_messages_max = self._env_number("PROCESSED_MESSAGES_MAX", self.processed_messages_max, int)
        self.rate_limit_global_capacity = self._env_number("RATE_LIMIT_GLOBAL_CAPACITY", self.rate_limit_global_capacity, int)
        self.rate_limit_user_capacity = self._env_number("RATE_LIMIT_USER_CAPACITY", self.rate_limit_user_capacity, int)
        self.rate_limit_idle_capacity = self._env_number("RATE_LIMIT_IDLE_CAPACITY", self.rate_limit_idle_capacity, int)
        self.rate_limit_discord_capacity = self._env_number("RATE_LIMIT_DISCORD_CAPACITY", self.rate_limit_discord_capacity, int)
        self.chat_check_interval = self._env_number("CHAT_CHECK_INTERVAL", self.chat_check_interval, int)
        self.gemini_search_daily_limit = self._env_number("GEMINI_SEARCH_DAILY_LIMIT", self.gemini_search_daily_limit, int)
        self.gemini_search_cache_ttl = self._env_number("GEMINI_SEARCH_CACHE_TTL", self.gemini_search_cache_ttl, int)
        self.jev_max_calls_per_minute = min(
            300, max(1, self._env_number("JEV_MAX_CALLS_PER_MINUTE", self.jev_max_calls_per_minute, int))
        )
        self.ai_max_repair_attempts = self._env_number("AI_MAX_REPAIR_ATTEMPTS", self.ai_max_repair_attempts, int)
        self.ai_max_planner_steps = self._env_number("AI_MAX_PLANNER_STEPS", self.ai_max_planner_steps, int)
        self.ai_max_completion_tokens = max(
            64, min(2000, self._env_number("RUKIYA_MAX_COMPLETION_TOKENS", self.ai_max_completion_tokens, int))
        )
        self.max_ai_concurrency = self._env_number("MAX_AI_CONCURRENCY", self.max_ai_concurrency, int)
        self.max_search_concurrency = self._env_number("MAX_SEARCH_CONCURRENCY", self.max_search_concurrency, int)

        # Parse float env vars one at a time, so a bad value only affects itself
        self.memory_decay_days = self._env_number("MEMORY_DECAY_DAYS", self.memory_decay_days, float)
        self.response_threshold = self._env_number("RESPONSE_THRESHOLD", self.response_threshold, float)
        self.anti_repeat_threshold = self._env_number("ANTI_REPEAT_THRESHOLD", self.anti_repeat_threshold, float)
        self.rate_limit_idle_interval = self._env_number("IDLE_CHAT_INTERVAL", self.rate_limit_idle_interval, float)
        self.openrouter_timeout = self._env_number("OPENROUTER_TIMEOUT", self.openrouter_timeout, float)
        self.gemini_timeout = self._env_number("GEMINI_TIMEOUT", self.gemini_timeout, float)
        self.search_timeout = self._env_number("SEARCH_TIMEOUT", self.search_timeout, float)
        self.critic_timeout = self._env_number("CRITIC_TIMEOUT", self.critic_timeout, float)
        self.repair_timeout = self._env_number("REPAIR_TIMEOUT", self.repair_timeout, float)
        self.send_cooldown = self._env_number("SEND_COOLDOWN", self.send_cooldown, float)
        self.jev_timeout = min(
            15.0, max(0.5, self._env_number("JEV_TIMEOUT", self.jev_timeout, float))
        )
        self.jev_respond_threshold = min(
            1.0, max(0.0, self._env_number("JEV_RESPOND_THRESHOLD", self.jev_respond_threshold, float))
        )
        self.jev_ignore_threshold = min(
            self.jev_respond_threshold,
            max(0.0, self._env_number("JEV_IGNORE_THRESHOLD", self.jev_ignore_threshold, float)),
        )

        reasoning_env = os.getenv("RUKIYA_REASONING_ENABLED")
        if reasoning_env is not None:
            self.ai_reasoning_enabled = reasoning_env.lower() in ("1", "true", "yes", "on")
        self.ai_reasoning_effort = os.getenv(
            "RUKIYA_REASONING_EFFORT", self.ai_reasoning_effort
        ).lower().strip() or self.ai_reasoning_effort

        idle_enabled_env = os.getenv("IDLE_CHAT_ENABLED")
        if idle_enabled_env is not None:
            self.idle_chat_enabled = idle_enabled_env.lower() in ("1", "true", "yes")

    @staticmethod
    def _env_number(name: str, current, cast):
        """Read one numeric env var; an invalid value keeps the default and is reported."""
        raw = os.getenv(name)
        if raw is None:
            return current
        try:
            return cast(raw)
        except ValueError:
            # Report the variable, not its value, in case a secret was pasted into it.
            logger.warning("Ignoring invalid %s value for %s; keeping %r", cast.__name__, name, current)
            return current

    def update_from_dict(self, data: dict) -> None:
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def update_from_obj(self, obj) -> None:
        for key in dir(obj):
            if not key.startswith('_') and hasattr(self, key):
                setattr(self, key, getattr(obj, key))
