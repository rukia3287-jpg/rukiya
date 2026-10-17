"""Adaptive behavior policy for Rukiya's hybrid conversation pipeline.

This module deliberately separates task effort from communication style. It does
not claim to measure intelligence; it selects a bounded response strategy from
observable task signals.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional


SERIOUS_INTENTS = frozenset({"crisis", "sensitive"})


@dataclass(frozen=True)
class AdaptiveProfile:
    effort_level: str
    complexity_hint: float
    style_instruction: str
    local_reply: Optional[str] = None


class AdaptiveBehaviorController:
    """Choose task effort and conversational style without making an LLM call."""

    def __init__(self, config=None):
        self.config = config
        self.enabled = os.getenv("RUKIYA_ADAPTIVE_BEHAVIOR_ENABLED", "true").strip().lower() not in {
            "0", "false", "no", "off"
        }
        self.local_rules_enabled = os.getenv("RUKIYA_LOCAL_RULES_ENABLED", "false").strip().lower() in {
            "1", "true", "yes", "on"
        }

    @staticmethod
    def _complexity(text: str, intent: str, has_memory: bool) -> float:
        lowered = text.lower()
        words = len(lowered.split())
        score = min(0.30, words / 80.0)
        if intent in {"greeting", "compliment"}:
            score = min(score, 0.08)
        if intent in SERIOUS_INTENTS:
            score = max(score, 0.45)
        if any(term in lowered for term in (
            "debug", "error", "architecture", "implement", "refactor", "algorithm",
            "compare", "versus", "step by step", "trade-off", "tradeoff", "why does",
            "how does", "design a system", "multiple files", "root cause"
        )):
            score += 0.30
        if any(term in lowered for term in (
            "plan", "roadmap", "evaluate", "analyze", "analyse", "review", "explain in detail",
            "pros and cons", "several", "multiple", "complex"
        )):
            score += 0.20
        if has_memory and any(term in lowered for term in (
            "remember", "last time", "yesterday", "earlier", "what did i", "continue", "same issue"
        )):
            score += 0.15
        return round(max(0.0, min(1.0, score)), 2)

    @staticmethod
    def _local_reply(text: str, intent: str, enabled: bool) -> Optional[str]:
        if not enabled:
            return None
        normalized = re.sub(r"[!.,?]+$", "", text.strip().lower())
        greetings = {
            "hi", "hello", "hey", "yo", "sup", "hi rukiya", "hello rukiya",
            "hey rukiya", "yo rukiya", "good morning", "good evening"
        }
        thanks = {"thanks", "thank you", "thx", "ty", "thanks rukiya"}
        if normalized in greetings and intent == "greeting":
            return "Hm. You're back. What is it this time?"
        if normalized in thanks:
            return "Don't mention it. Just don't make a habit of needing rescue."
        return None

    def assess(
        self,
        *,
        text: str,
        intent: str,
        platform: str = "discord",
        interaction_count: int = 0,
        has_memory: bool = False,
    ) -> AdaptiveProfile:
        """Return a deterministic, bounded effort and style profile for one turn."""
        complexity = self._complexity(text, intent, has_memory) if self.enabled else 0.25
        if intent in SERIOUS_INTENTS:
            effort = "analytical" if complexity >= 0.60 else "contextual"
        elif complexity >= 0.75:
            effort = "deep"
        elif complexity >= 0.40:
            effort = "analytical"
        elif has_memory or intent in {"question", "help"}:
            effort = "contextual"
        else:
            effort = "instant"

        if intent in SERIOUS_INTENTS:
            style = (
                "COMMUNICATION STYLE: Be calm, caring, direct, and nonjudgmental. "
                "No sarcasm, teasing, flirtation, or forced humor. Prioritize the person's wellbeing."
            )
        elif effort == "deep":
            style = (
                "COMMUNICATION STYLE: Treat this as a complex task. Be precise, structured, and specific; "
                "check assumptions and explain the useful conclusion without padding the response."
            )
        elif effort == "analytical":
            style = (
                "COMMUNICATION STYLE: Give a thoughtful, accurate answer with enough reasoning and concrete "
                "details to be useful. Stay in Rukiya's natural voice; do not become a generic help desk."
            )
        elif intent in {"greeting", "compliment"}:
            style = (
                "COMMUNICATION STYLE: Keep it brief, natural, and in character. Use light teasing only when "
                "it fits the current exchange; avoid repeating stock catchphrases."
            )
        else:
            familiarity = "familiar" if interaction_count >= 5 else "new or occasional"
            style = (
                f"COMMUNICATION STYLE: This is a {familiarity} viewer. Respond naturally to the actual message, "
                "keep casual chat concise, and use a relevant callback only when supported by context."
            )

        if platform.lower() in {"youtube", "twitch", "kick"} and effort in {"instant", "contextual"}:
            style += " LIVESTREAM LIMIT: Prefer one concise sentence and usually stay under 220 characters."
        else:
            style += " Match answer length to the task; do not confuse brevity with shallow reasoning."

        local_reply = self._local_reply(text, intent, self.local_rules_enabled and self.enabled)
        return AdaptiveProfile(
            effort_level=effort,
            complexity_hint=complexity,
            style_instruction=style,
            local_reply=local_reply,
        )
