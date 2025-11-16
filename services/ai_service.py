from __future__ import annotations
import os
import time
import asyncio
import logging
from typing import Optional, Iterable
from dataclasses import dataclass

import httpx
import discord
from discord.ext import commands
from discord import app_commands

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# -----------------------------
# Simple config dataclass
# -----------------------------
@dataclass
class BotConfig:
    ai_cooldown: float = 10.0
    max_message_length: int = 200
    bot_users: Iterable[str] = ()
    banned_words: Iterable[str] = ()
    ai_triggers: Iterable[str] = ("rukiya", "hey rukiya", "@rukiya")


# -----------------------------
# AIService: OpenRouter-only
# -----------------------------
class AIService:
    """OpenRouter-only async AI service.

    Requires env var OPENROUTER_API_KEY. Optional OPENROUTER_MODEL.

    Public API:
      - await ai_service.generate_response(message, author) -> Optional[str]
      - ai_service.get_cooldown_remaining()
    """

    def __init__(self, config: BotConfig):
        self.config = config
        self.last_used = 0.0
        self.openrouter_key = os.getenv("OPENROUTER_API_KEY")
        if not self.openrouter_key:
            logger.error("OPENROUTER_API_KEY not set. AIService disabled.")
        self.model = os.getenv("OPENROUTER_MODEL", "gpt-3.5-turbo")
        self.endpoint = os.getenv("OPENROUTER_ENDPOINT", "https://api.openrouter.ai/v1/chat/completions")

    def can_respond(self) -> bool:
        return time.time() - self.last_used > float(getattr(self.config, "ai_cooldown", 0))

    def should_respond(self, message: str, author: str) -> bool:
        """Rules to decide whether to call the AI.

        Customize this as needed for usernames/IDs instead of display names.
        """
        if not self.openrouter_key:
            return False
        if not self.can_respond():
            return False
        author_lower = author.lower()
        if any(author_lower == u.lower() for u in getattr(self.config, "bot_users", [])):
            return False
        msg = message.lower()
        if any(w in msg for w in getattr(self.config, "banned_words", [])):
            return False
        return any(trigger in msg for trigger in getattr(self.config, "ai_triggers", []))

    async def _call_openrouter(self, prompt: str, max_tokens: int = 256) -> Optional[str]:
        """Internal: async call to OpenRouter with 3 attempts and exponential backoff."""
        if not self.openrouter_key:
            return None

        headers = {
            "Authorization": f"Bearer {self.openrouter_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.75,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(1, 4):  # 3 attempts
                try:
                    resp = await client.post(self.endpoint, json=payload, headers=headers)
                except httpx.RequestError as e:
                    logger.warning("OpenRouter network error (attempt %d): %s", attempt, e)
                    if attempt < 3:
                        await asyncio.sleep(2 ** (attempt - 1))
                        continue
                    return None

                # handle rate-limits / server busy
                if resp.status_code in (429, 503):
                    logger.warning("OpenRouter rate-limited (status %d). Attempt %d/3", resp.status_code, attempt)
                    if attempt < 3:
                        await asyncio.sleep(2 ** (attempt - 1))
                        continue
                    else:
                        logger.error("OpenRouter rate limit exceeded after 3 attempts.")
                        return None

                if resp.status_code >= 400:
                    logger.error("OpenRouter returned HTTP %d: %s", resp.status_code, resp.text[:500])
                    return None

                # parse JSON safely
                try:
                    j = resp.json()
                except Exception:
                    logger.error("OpenRouter returned non-json response: %s", resp.text[:200])
                    return None

                # Most providers return choices -> message -> content
                choices = j.get("choices") or []
                if choices:
                    first = choices[0]
                    text = None
                    if isinstance(first, dict):
                        text = (first.get("message") or {}).get("content") or first.get("text")
                    if not text:
                        # try to extract any string from choices
                        for c in choices:
                            if isinstance(c, dict):
                                maybe = (c.get("message") or {}).get("content") or c.get("text")
                                if isinstance(maybe, str) and maybe.strip():
                                    text = maybe
                                    break
                    if isinstance(text, str) and text.strip():
                        return text.strip()

                # top-level fallback
                if isinstance(j.get("text"), str) and j.get("text").strip():
                    return j.get("text").strip()

                logger.warning("OpenRouter returned no usable text: %s", j)
                return None

        return None

    async def generate_response(self, message: str, author: str) -> Optional[str]:
        """Public: generates a response asynchronously or returns None."""
        try:
            if not self.should_respond(message, author):
                return None

            prompt = (
                f"User {author} said: {message}\n"
                "Respond as Rukiya, a fun Hinglish YouTube chat bot. "
                "Keep it under 12 words, be friendly and engaging. Use emojis and Hinglish style."
            )

            raw = await self._call_openrouter(prompt, max_tokens=128)
            if not raw:
                return None

            # update cooldown only on success
            self.last_used = time.time()

            max_len = int(getattr(self.config, "max_message_length", 200))
            if len(raw) > max_len:
                raw = raw[: max_len - 3] + "..."

            logger.info("AI response for %s: %s", author, raw)
            return raw

        except Exception as e:
            logger.exception("generate_response error: %s", e)
            return None

    def get_cooldown_remaining(self) -> float:
        elapsed = time.time() - self.last_used
        return max(0.0, float(getattr(self.config, "ai_cooldown", 0)) - elapsed)


# -----------------------------
# Example Discord Cog using AIService
# -----------------------------
class YouTubeCog(commands.Cog):
    def __init__(self, bot: commands.Bot, ai_service: AIService):
        self.bot = bot
        self.ai_service = ai_service

    @app_commands.command(name="start", description="Start monitoring (example)")
    async def start_monitoring(self, interaction: discord.Interaction) -> None:
        # Defer immediately so token stays valid while we do work.
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(thinking=True)
        except discord.NotFound:
            # The interaction expired before we could defer.
            logger.warning("Interaction expired before defer could be sent.")
            # Best-effort: proceed and try to send followup later.
        except Exception:
            logger.exception("Failed to defer interaction")

        # Example input - in real code you would collect real text
        # For demonstration we use a sample message and author.
        sample_message = "hello rukiya, tell me something fun"
        author_name = interaction.user.display_name if interaction.user else "unknown"

        # Call AI (async, non-blocking)
        ai_text = await self.ai_service.generate_response(sample_message, author_name)

        # Send followup. This may still fail if the interaction fully expired; catch that.
        try:
            if ai_text:
                await interaction.followup.send(ai_text)
            else:
                await interaction.followup.send("Sorry, cannot answer right now.")
        except discord.NotFound:
            logger.warning("Couldn't send followup - interaction fully expired.")
        except Exception:
            logger.exception("Failed to send followup")

