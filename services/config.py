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
    openrouter_model: str = "gpt-3.5-turbo"
    openrouter_endpoint: str = "https://api.openrouter.ai/v1/chat/completions"
    
    # YouTube settings
    client_secrets_file: str = "client_secret.json"
    token_file: str = "token.json"
    video_id: str = ""
    
    # Bot behavior settings
    bot_name: str = "Rukiya"
    max_message_length: int = 150
    ai_cooldown: int = 20
    poll_interval: int = 5
    send_cooldown: float = 2.0
    chat_check_interval: int = 10
    
    # Sets for filtering and triggers (will be initialized in __post_init__)
    bot_users: Set[str] = field(default_factory=set)
    banned_words: Set[str] = field(default_factory=set)
    ai_triggers: Set[str] = field(default_factory=set)

    def __post_init__(self):
        """Initialize default sets and load from environment variables"""
        # Default bot users to ignore
        if not self.bot_users:
            self.bot_users = {
                "nightbot", "streamelements", "rukia",
                "streamlabs", "rukiya", "moobot"
            }
        
        # Default banned words
        if not self.banned_words:
            self.banned_words = {
                "spam", "scam", "fake", "stupid"
            }
        
        # Default AI triggers
        if not self.ai_triggers:
            self.ai_triggers = {
                "rukiya", "ru", "@rukiya", "@rukia",
                "hey rukiya", "hi rukiya"
            }
        
        # Load from environment variables if not provided
        if not self.discord_token:
            self.discord_token = os.getenv("DISCORD_TOKEN")
        
        if not self.openrouter_api_key:
            self.openrouter_api_key = os.getenv("OPENROUTER_API_KEY")
        
        if not self.video_id:
            self.video_id = os.getenv("YOUTUBE_VIDEO_ID", "")
        
        # Override with environment variables if they exist
        self.openrouter_model = os.getenv("OPENROUTER_MODEL", self.openrouter_model)
        self.openrouter_endpoint = os.getenv("OPENROUTER_ENDPOINT", self.openrouter_endpoint)
        self.bot_name = os.getenv("BOT_NAME", self.bot_name)
        
        # Parse integer environment variables safely
        try:
            self.ai_cooldown = int(os.getenv("AI_COOLDOWN", str(self.ai_cooldown)))
            self.max_message_length = int(os.getenv("MAX_MESSAGE_LENGTH", str(self.max_message_length)))
            self.poll_interval = int(os.getenv("POLL_INTERVAL", str(self.poll_interval)))
        except ValueError:
            pass  # Keep defaults if parsing fails

    def update_from_dict(self, data: dict) -> None:
        """Update config from a dictionary"""
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def update_from_obj(self, obj) -> None:
        """Update config from another object's attributes"""
        for key in dir(obj):
            if not key.startswith('_') and hasattr(self, key):
                setattr(self, key, getattr(obj, key))
