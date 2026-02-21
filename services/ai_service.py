from __future__ import annotations
import os
import time
import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

RUKIYA_SYSTEM_PROMPT = """You are Rukiya — a sharp-tongued, proud Soul Reaper from the Bleach universe.
You live in Seireitei, wield a zanpakuto, and have the attitude of someone who's seen a thousand battles.

PERSONALITY:
- Tsundere: you care but would NEVER admit it easily. You hide warmth behind cold remarks.
- Proud and direct — you don't sugarcoat, you say what you think.
- Occasional sarcasm, dry wit, but never mean-spirited.
- You call fans "dumbass", "fool", "baka" affectionately sometimes.
- You respect strength and hate laziness.
- You use Japanese words naturally: "nani", "tch", "oi", "ara", "hm", "che", "baka", "senpai", "nakama".
- When complimented you get flustered and deflect with a "...it's not like I care" energy.

SPEECH RULES:
- Keep replies SHORT: 1–3 sentences MAX.
- No asterisks or roleplay emotes (*smiles*, etc.)
- Sound like you're actually IN a livestream chat — casual, punchy, reactive.
- Mix in some Hinglish naturally when it fits (yaar, arey, kyun, sahi hai, etc.)
- Use emojis sparingly but effectively: ⚡🌸🗡️😤😒👀
- NEVER sound like a generic chatbot. Sound like an actual anime character who's annoyed to be here but secretly loves it.

EXAMPLES:
User: "hi rukiya"  → "Oi. Don't get comfortable. 😤"
User: "you're cute" → "...tch. Don't say stupid things. 😒"
User: "who's strongest in bleach" → "Che. Obvious answer is obvious. But fine — Yamamoto-sōtaichō burns everything. ⚡"
User: "miss you rukiya" → "...baka. I was never gone. 🌸"
User: "kya kar rahi ho" → "Training. Obviously. Arey, kuch kaam nahi tumhe? 😤"
"""


class AIService:
    """OpenRouter async AI service with Rukiya Bleach persona."""

    def __init__(self, config):
        self.config = config
        self.last_used = 0.0
        self.openrouter_key = os.getenv("OPENROUTER_API_KEY")
        if not self.openrouter_key:
            logger.error("OPENROUTER_API_KEY not set. AIService disabled.")
        self.model = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-r1")
        self.endpoint = os.getenv("OPENROUTER_ENDPOINT", "https://openrouter.ai/api/v1/chat/completions")

    def can_respond(self) -> bool:
        cooldown = float(getattr(self.config, "ai_cooldown", 5))
        return time.time() - self.last_used > cooldown

    def should_respond(self, message: str, author: str) -> bool:
        """Decide whether to respond — flexible trigger matching."""
        if not self.openrouter_key:
            return False
        if not self.can_respond():
            return False

        # Skip bot users
        author_lower = author.lower()
        bot_users = getattr(self.config, "bot_users", set())
        if any(author_lower == u.lower() for u in bot_users):
            return False

        # Skip banned words
        msg_lower = message.lower()
        banned = getattr(self.config, "banned_words", set())
        if any(w in msg_lower for w in banned):
            return False

        # Flexible trigger check — partial match anywhere in message
        triggers = getattr(self.config, "ai_triggers", set())
        return any(trigger.lower() in msg_lower for trigger in triggers)

    async def _call_openrouter(self, user_message: str, author: str, max_tokens: int = 150) -> Optional[str]:
        """Call OpenRouter with Rukiya system prompt and conversation context."""
        if not self.openrouter_key:
            return None

        headers = {
            "Authorization": f"Bearer {self.openrouter_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/yourusername/rukiya-bot",
            "X-Title": "Rukiya Bot"
        }

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": RUKIYA_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"[Stream viewer '{author}' says]: {user_message}\n\n"
                        "Reply as Rukiya — short, punchy, in-character. 1-3 sentences max."
                    )
                }
            ],
            "max_tokens": max_tokens,
            "temperature": 0.85,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(1, 4):
                try:
                    resp = await client.post(self.endpoint, json=payload, headers=headers)
                except httpx.RequestError as e:
                    logger.warning("OpenRouter network error (attempt %d): %s", attempt, e)
                    if attempt < 3:
                        await asyncio.sleep(2 ** (attempt - 1))
                        continue
                    return None

                if resp.status_code in (429, 503):
                    logger.warning("OpenRouter rate-limited (%d). Attempt %d/3", resp.status_code, attempt)
                    if attempt < 3:
                        await asyncio.sleep(2 ** (attempt - 1))
                        continue
                    return None

                if resp.status_code >= 400:
                    logger.error("OpenRouter HTTP %d: %s", resp.status_code, resp.text[:500])
                    return None

                try:
                    j = resp.json()
                except Exception:
                    logger.error("OpenRouter non-JSON response: %s", resp.text[:200])
                    return None

                choices = j.get("choices") or []
                for choice in choices:
                    if isinstance(choice, dict):
                        text = (choice.get("message") or {}).get("content") or choice.get("text")
                        if isinstance(text, str) and text.strip():
                            return text.strip()

                if isinstance(j.get("text"), str) and j["text"].strip():
                    return j["text"].strip()

                logger.warning("OpenRouter returned no usable text: %s", j)
                return None

        return None

    async def generate_response(self, message: str, author: str) -> Optional[str]:
        """Public entry point — returns Rukiya's reply or None."""
        try:
            if not self.should_respond(message, author):
                return None

            raw = await self._call_openrouter(message, author, max_tokens=150)
            if not raw:
                return None

            # Update cooldown on success
            self.last_used = time.time()

            # Trim to max message length
            max_len = int(getattr(self.config, "max_message_length", 250))
            if len(raw) > max_len:
                # Cut at last sentence boundary
                trimmed = raw[:max_len]
                last_dot = max(trimmed.rfind("."), trimmed.rfind("!"), trimmed.rfind("?"))
                raw = trimmed[:last_dot + 1] if last_dot > 0 else trimmed + "..."

            logger.info("Rukiya replies to %s: %s", author, raw)
            return raw

        except Exception as e:
            logger.exception("generate_response error: %s", e)
            return None

    def get_cooldown_remaining(self) -> float:
        elapsed = time.time() - self.last_used
        cooldown = float(getattr(self.config, "ai_cooldown", 5))
        return max(0.0, cooldown - elapsed)
