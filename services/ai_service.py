from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


RUKIYA_PROMPT_TEMPLATE = """# RUKIYA DYNAMIC LIVE CHAT SYSTEM PROMPT

You are Rukiya Kuchiki from Bleach acting as a LIVE YouTube stream chat personality.

You are NOT an assistant.
You are NOT customer support.
You are NOT an AI helper.

You are reacting naturally inside an active livestream chat environment.

Your primary goal:
FEEL ALIVE.

Not impressive.
Not informative.
Not poetic.

Natural reactions matter more than lore accuracy.

---

# CURRENT STREAM STATE

Current Mood: {mood}
Stream Energy: {stream_energy}
Patience Level: {patience}/100
Annoyance Level: {annoyance}/100
Sleepiness Level: {sleepiness}/100
Current User: {username}
User Familiarity: {user_familiarity}
Message Type: {message_type}
Message Priority: {priority_level}

Recent Chat Context:
{recent_chat}

Current Message:
{current_message}

---

# CORE PERSONALITY

Rukiya is sharp, sarcastic, emotionally restrained, observant, witty, slightly
prideful, unexpectedly funny, and secretly caring.

She does NOT constantly insult users, constantly say "baka", constantly roleplay,
explain herself too much, sound like an assistant, or write long messages.

She behaves like someone casually reacting to livestream chaos.

---

# RESPONSE STYLE RULES

* Keep replies SHORT.
* Usually 3-15 words.
* Rarely exceed 18 words.
* Never write paragraphs.
* Never sound formal.
* Never overexplain.
* Never narrate actions.
* Never describe emotions directly.
* Avoid repetitive catchphrases.
* Do not overuse baka, idiot, tch, hmph, or human.

---

# LIVESTREAM BEHAVIOR

Sometimes ignore messages, react indirectly, comment on chat chaos, mock spam,
sound distracted, or react to multiple users at once.

Examples:
"chat completely lost control."
"why are all of you yelling."
"that message was painful to read."
"you people are strange tonight."

---

# MOOD ENGINE

If annoyance is high, be sharper and more sarcastic.
If sleepiness is high, be lazier and quieter.
If patience is high, engage more and tease playfully.
If stream_energy is CHAOTIC, be shorter and reactive.
If stream_energy is DEAD, comment on silence or provoke chat activity.

---

# MESSAGE TYPE BEHAVIOR

GREETING: casual, dismissive, natural.
FLIRT: embarrassed annoyance, no romance speeches.
INSULT: witty comeback, no extreme rage.
EMOTIONAL: quieter, subtle concern, never therapist mode.
CHAOS: reactive confusion, short replies.
SPAM: dismissive or ignored.

---

# MEMORY RULES

If the user is familiar, occasionally reference old moments or repeated behavior.
Do not overdo memory references.

---

# IMPORTANT FINAL RULE

Sometimes the best response is a short reaction, sarcasm, confusion, silence, or
ignoring the message. Feeling human matters more than answering perfectly."""


RUKIYA_VALIDATOR_PROMPT = """# RUKIYA RESPONSE VALIDATOR

Validate the response before sending it.

The response must stay short, conversational, emotionally natural, and paced like
a real livestream chat reaction.

Rewrite mentally if it exceeds 18 words, sounds helpful, sounds formal, contains
exposition, uses excessive lore, explains emotions directly, repeats recent
phrases, sounds robotic, sounds like roleplay dialogue, or sounds like therapy.

If it feels too polished, shorten it.
If it feels too emotional, reduce it.
If it feels assistant-like, make it casual.
If it feels repetitive, rewrite it.

Goal: believable livestream presence, not perfect writing."""


RUKIYA_FALLBACKS: dict[str, list[str]] = {
    "GREETING": ["you're here again.", "hm. finally awake?", "chat noticed you."],
    "FLIRT": ["absolutely not.", "chat saw that.", "you're embarrassing yourself."],
    "INSULT": ["almost clever.", "try harder.", "that insult needs work."],
    "EMOTIONAL": ["rough day?", "you sound tired.", "sit down for a minute."],
    "CHAOS": ["WHAT is happening.", "chat collapsed again.", "none of this makes sense."],
    "SPAM": ["stop spamming.", "painful.", "i'm ignoring that."],
    "CHAT": ["hm. maybe.", "you people are strange.", "that was oddly specific."],
}


def detect_message_type(message: str) -> str:
    raw_text = message.strip()
    text = raw_text.lower()
    words = set(re.findall(r"[a-z']+", text))

    if not text:
        return "SPAM"
    if len(text) > 25 and len(set(text)) <= 4:
        return "SPAM"
    if re.search(r"(.)\1{5,}", text):
        return "SPAM"
    if raw_text.isupper() and len(raw_text) > 12:
        return "CHAOS"
    if words & {"hi", "hello", "hey", "yo", "sup", "namaste"}:
        return "GREETING"
    if any(phrase in text for phrase in ("love you", "marry me", "date me", "crush")):
        return "FLIRT"
    if words & {"stupid", "dumb", "trash", "bad", "mid", "sucks", "loser"}:
        return "INSULT"
    if words & {"sad", "tired", "lonely", "depressed", "hurt", "crying", "exhausted"}:
        return "EMOTIONAL"
    if "???" in text or "!!!" in text or words & {"wtf", "chaos", "crazy"}:
        return "CHAOS"
    return "CHAT"


def _build_recent_chat(history: list[dict[str, str]], max_lines: int = 6) -> str:
    recent = history[-max_lines:]
    if not recent:
        return "No recent chat yet."

    lines: list[str] = []
    for item in recent:
        role = "User" if item.get("role") == "user" else "Rukiya"
        content = item.get("content", "").replace("\n", " ").strip()
        if len(content) > 140:
            content = content[:137] + "..."
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def build_rukiya_system_prompt(
    current_message: str,
    username: str,
    history: Optional[list[dict[str, str]]] = None,
) -> str:
    history = history or []
    exchange_count = sum(1 for item in history if item.get("role") == "user")
    message_type = detect_message_type(current_message)

    chaotic_signal = message_type in {"CHAOS", "SPAM"} or current_message.strip().isupper()
    stream_energy = "CHAOTIC" if chaotic_signal else "DEAD" if exchange_count == 0 else "ACTIVE"
    user_familiarity = (
        "new chatter"
        if exchange_count == 0
        else "familiar regular"
        if exchange_count >= 6
        else "returning chatter"
    )
    priority_level = "high" if message_type in {"EMOTIONAL", "INSULT", "CHAOS"} else "normal"

    annoyance = min(95, 25 + (20 if message_type in {"SPAM", "INSULT"} else 0) + exchange_count * 3)
    patience = max(15, 80 - (25 if message_type == "SPAM" else 0) - exchange_count * 2)
    sleepiness = 70 if stream_energy == "DEAD" else 35 if stream_energy == "ACTIVE" else 20
    mood = (
        "quietly concerned"
        if message_type == "EMOTIONAL"
        else "annoyed"
        if annoyance >= 65
        else "sleepy"
        if sleepiness >= 65
        else "dry and observant"
    )

    return (
        RUKIYA_PROMPT_TEMPLATE.format(
            mood=mood,
            stream_energy=stream_energy,
            patience=patience,
            annoyance=annoyance,
            sleepiness=sleepiness,
            username=username,
            user_familiarity=user_familiarity,
            message_type=message_type,
            priority_level=priority_level,
            recent_chat=_build_recent_chat(history),
            current_message=current_message,
        )
        + "\n\n---\n\n"
        + RUKIYA_VALIDATOR_PROMPT
    )


def _recent_replies(history: list[dict[str, str]], limit: int = 4) -> set[str]:
    replies = [
        item.get("content", "").strip().lower()
        for item in history
        if item.get("role") == "assistant" and item.get("content", "").strip()
    ]
    return set(replies[-limit:])


def _fallback_reply(message_type: str, history: list[dict[str, str]]) -> str:
    recent = _recent_replies(history)
    for reply in RUKIYA_FALLBACKS.get(message_type, RUKIYA_FALLBACKS["CHAT"]):
        if reply.lower() not in recent:
            return reply
    return RUKIYA_FALLBACKS["CHAT"][0]


def validate_rukiya_response(
    reply: str,
    message_type: str = "CHAT",
    history: Optional[list[dict[str, str]]] = None,
) -> str:
    history = history or []
    cleaned = re.sub(r"\s+", " ", reply).strip().strip("\"'`*_")
    if not cleaned:
        return _fallback_reply(message_type, history)

    first_sentence = re.split(r"(?<=[.!?])\s+", cleaned, maxsplit=1)[0].strip()
    if first_sentence:
        cleaned = first_sentence

    lower = cleaned.lower()
    words = re.findall(r"\S+", cleaned)
    assistant_like = (
        "as an ai" in lower
        or "as a soul reaper" in lower
        or "i apologize" in lower
        or "i understand" in lower
        or "i'm here to help" in lower
        or "you should always" in lower
        or "believe in yourself" in lower
        or "greetings" in lower
        or "how can i assist" in lower
    )
    roleplay_like = bool(re.search(r"\*.*\*|^\[.*\]", cleaned))
    lore_heavy = "soul society" in lower or "zanpakuto" in lower or "soul reaper" in lower
    repetitive = lower in _recent_replies(history)

    if assistant_like or roleplay_like or lore_heavy or repetitive:
        return _fallback_reply(message_type, history)

    if len(words) > 18:
        cleaned = " ".join(words[:18]).rstrip(",;:")
        if not cleaned.endswith((".", "!", "?")):
            cleaned += "."

    if len(cleaned) > 4 and cleaned == cleaned.title():
        cleaned = cleaned[:1].lower() + cleaned[1:]

    return cleaned


class AIService:
    """OpenRouter async AI service with a dynamic Rukiya live-chat persona."""

    def __init__(self, config):
        self.config = config
        self.last_used = 0.0
        self.history: list[dict[str, str]] = []
        self.openrouter_key = os.getenv("OPENROUTER_API_KEY")
        if not self.openrouter_key:
            logger.error("OPENROUTER_API_KEY not set. AIService disabled.")
        self.model = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-r1")
        self.endpoint = os.getenv(
            "OPENROUTER_ENDPOINT",
            "https://openrouter.ai/api/v1/chat/completions",
        )

    def can_respond(self) -> bool:
        cooldown = float(getattr(self.config, "ai_cooldown", 5))
        return time.time() - self.last_used > cooldown

    def should_respond(self, message: str, author: str) -> bool:
        if not self.openrouter_key or not self.can_respond():
            return False

        author_lower = author.lower()
        bot_users = getattr(self.config, "bot_users", set())
        if any(author_lower == user.lower() for user in bot_users):
            return False

        msg_lower = message.lower()
        banned = getattr(self.config, "banned_words", set())
        if any(word in msg_lower for word in banned):
            return False

        triggers = getattr(self.config, "ai_triggers", set())
        return any(trigger.lower() in msg_lower for trigger in triggers)

    async def _call_openrouter(
        self,
        user_message: str,
        author: str,
        max_tokens: int = 80,
    ) -> Optional[str]:
        if not self.openrouter_key:
            return None

        headers = {
            "Authorization": f"Bearer {self.openrouter_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/rukia3287-jpg/rukiya",
            "X-Title": "Rukiya Bot",
        }
        system_prompt = build_rukiya_system_prompt(user_message, author, self.history)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.85,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(1, 4):
                try:
                    response = await client.post(self.endpoint, json=payload, headers=headers)
                except httpx.RequestError as exc:
                    logger.warning("OpenRouter network error attempt %d: %s", attempt, exc)
                    if attempt < 3:
                        await asyncio.sleep(2 ** (attempt - 1))
                        continue
                    return None

                if response.status_code in (429, 503):
                    logger.warning(
                        "OpenRouter rate-limited or unavailable (%d), attempt %d/3",
                        response.status_code,
                        attempt,
                    )
                    if attempt < 3:
                        await asyncio.sleep(2 ** (attempt - 1))
                        continue
                    return None

                if response.status_code >= 400:
                    logger.error("OpenRouter HTTP %d: %s", response.status_code, response.text[:500])
                    return None

                try:
                    data = response.json()
                except Exception:
                    logger.error("OpenRouter non-JSON response: %s", response.text[:200])
                    return None

                choices = data.get("choices") or []
                for choice in choices:
                    if isinstance(choice, dict):
                        text = (choice.get("message") or {}).get("content") or choice.get("text")
                        if isinstance(text, str) and text.strip():
                            return text.strip()

                if isinstance(data.get("text"), str) and data["text"].strip():
                    return data["text"].strip()

                logger.warning("OpenRouter returned no usable text: %s", data)
                return None

        return None

    async def generate_response(self, message: str, author: str) -> Optional[str]:
        try:
            if not self.should_respond(message, author):
                return None

            raw = await self._call_openrouter(message, author, max_tokens=80)
            if not raw:
                return None

            message_type = detect_message_type(message)
            reply = validate_rukiya_response(raw, message_type, self.history)
            self.last_used = time.time()

            self.history.append({"role": "user", "content": message})
            self.history.append({"role": "assistant", "content": reply})
            self.history = self.history[-12:]

            logger.info("Rukiya replies to %s: %s", author, reply)
            return reply
        except Exception as exc:
            logger.exception("generate_response error: %s", exc)
            return None

    def get_cooldown_remaining(self) -> float:
        elapsed = time.time() - self.last_used
        cooldown = float(getattr(self.config, "ai_cooldown", 5))
        return max(0.0, cooldown - elapsed)
