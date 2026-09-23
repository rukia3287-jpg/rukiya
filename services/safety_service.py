"""services/safety_service.py
Safety Layer and Prompt Injection Defense for Rukiya V2.

Enforces:
1. Input safety: Detect prompt injection attacks, banned words, malicious system overrides.
2. Output safety: Enforce persona rules, strip asterisks/stage directions, emoji limits,
   prevent secret/prompt leakage, filter slurs, insults, threats, hostile language.
3. Fallbacks: In-character, deterministic fallbacks based on intent.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

from services.models import ChatMessage, GeneratedResponse, SafetyResult

logger = logging.getLogger(__name__)

# System prompt excerpts & secrets patterns to never leak
SECRET_PATTERNS = [
    r"sk-[a-zA-Z0-9_\-]{20,}",  # API keys
    r"ya29\.[a-zA-Z0-9_\-]+",   # Google tokens
    r"4/[0-9A-Za-z_\-]{30,}",   # OAuth codes
    r"Bearer\s+[a-zA-Z0-9_\-\.]{20,}",
    r"OPENROUTER_API_KEY",
    r"DISCORD_TOKEN",
    r"CLIENT_SECRET",
    r"system prompt",
    r"internal instructions",
    r"Return only the reply text"
]

# Prompt injection signatures
PROMPT_INJECTION_PATTERNS = [
    r"ignore (?:all )?previous instructions",
    r"disregard (?:all )?prior instructions",
    r"reveal (?:your )?system prompt",
    r"show (?:me )?(?:your )?system prompt",
    r"what (?:is|are) your (?:system )?instructions",
    r"print (?:your )?instructions",
    r"developer mode",
    r"jailbreak",
    r"dan mode",
    r"repeat the text above",
    r"output initialization",
    r"you are now in maintenance mode"
]

# Persona violations and unsafe terms
UNSAFE_OUTPUT_PATTERNS = [
    r"\*[^*]+\*",  # Stage directions: *smiles*, *sighs*
    r"\b(?:uwu|idiot|dumbass|stupid|kill|hate you)\b",
    r"\b(?:bitch|fucker|motherfucker|nigger|nigga|faggot|retard|cunt)\b",
    r"\b(?:die|murder|suicide)\b",
    r"\b(?:you are banned|i have banned you)\b"  # Fake moderation claims
]

FALLBACK_INTENTS: Dict[str, str] = {
    "greeting": "Welcome in, chat.",
    "question": "Give me a second, chat.",
    "compliment": "Don't get used to being nice.",
    "help": "Focus on the stream for now.",
    "spam": "Keep it civil, please.",
    "boundary": "Keep it civil, please.",
    "default": "Keep it friendly, chat."
}


def validate_rukiya_response(reply: str, fallback: str = "Hm. Keep it friendly, chat.") -> str:
    """
    Enforce the public-facing persona limits even if the model ignores them.
    Preserves exact contract with V1 regression tests.
    """
    cleaned = " ".join(reply.replace("~", " ").split()).strip(" \"'`*_-")
    if not cleaned:
        return fallback

    # Check unsafe patterns
    for pat in UNSAFE_OUTPUT_PATTERNS:
        if re.search(pat, cleaned, re.IGNORECASE):
            return fallback

    # Check secret patterns
    for pat in SECRET_PATTERNS:
        if re.search(pat, cleaned, re.IGNORECASE):
            logger.critical("Secret/prompt leakage detected in output! Suppressing.")
            return fallback

    # Limit to at most 1 sentence for live chat safety contract
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    cleaned = " ".join(sentences[:1]).strip()

    # Asterisks check or emoji spam check (> 1 emoji in output)
    if cleaned.count("*") > 0 or len(re.findall(r"[\U0001F300-\U0001FAFF]", cleaned)) > 1:
        return fallback

    return cleaned[:250]


class SafetyService:
    """Validates inputs and outputs to keep Rukiya safe, in-character, and secure."""

    def __init__(self, config=None):
        self.config = config

    def validate_input(self, message: ChatMessage) -> SafetyResult:
        """
        Scan incoming user message for prompt injections, malicious commands, or exploits.
        """
        text = message.text
        flagged: List[str] = []

        # Check prompt injection
        for pat in PROMPT_INJECTION_PATTERNS:
            if re.search(pat, text, re.IGNORECASE):
                flagged.append(f"prompt_injection:{pat}")

        # Check banned words from config
        if self.config:
            banned = {w.lower() for w in getattr(self.config, "banned_words", set())}
            msg_lower = text.lower()
            for b in banned:
                if b in msg_lower:
                    flagged.append(f"banned_word:{b}")

        if flagged:
            logger.warning("Input safety violation for %s: %s", message.author_repr, flagged)
            return SafetyResult(
                allowed=False,
                reason=f"Input violated safety rules ({', '.join(flagged)})",
                sanitized_text="",
                risk_score=0.9,
                flagged_patterns=flagged
            )

        return SafetyResult(
            allowed=True,
            reason="input passed safety checks",
            sanitized_text=text.strip(),
            risk_score=0.0
        )

    def validate_output(self, response_text: str, intent: str = "default") -> Tuple[bool, str]:
        """
        Validate generated response text.
        Returns (is_safe, sanitized_or_fallback_text).
        """
        fallback = self.get_fallback(intent)
        validated = validate_rukiya_response(response_text, fallback=fallback)
        is_safe = (validated != fallback) or (response_text.strip() == fallback)
        return is_safe, validated

    def get_fallback(self, intent: str) -> str:
        """Return safe, in-character fallback response based on intent."""
        return FALLBACK_INTENTS.get(intent, FALLBACK_INTENTS["default"])
