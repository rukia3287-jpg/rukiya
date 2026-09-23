"""services/ai_engine/critic.py
Self-critic evaluating candidate responses for persona, length, safety, repetition, and evidence consistency.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional

from services.ai_engine.models import (
    AIEngineRequest,
    CriticReport,
    CriticVerdict,
    EvidenceItem,
    Plan,
)
from services.config import Config

logger = logging.getLogger(__name__)

BANNED_STAGE_DIRECTIONS = re.compile(r"(\*[^*]+\*|\([^)]{2,}\))")
BOT_CLICHE_PATTERNS = [
    "as an ai", "as a language model", "i do not possess feelings",
    "i cannot feel", "how can i help you today", "how can i assist you"
]


class Critic:
    """Evaluates candidate response and issues PASS, REPAIR, or REJECT verdict."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.max_length = int(getattr(self.config, "max_message_length", 250))

    def evaluate(
        self,
        candidate_text: str,
        request: AIEngineRequest,
        plan: Plan,
        evidence_items: Optional[List[EvidenceItem]] = None,
        recent_responses: Optional[List[str]] = None,
        verified_current_information: bool = True,
        search_failed: bool = False,
    ) -> CriticReport:
        text = candidate_text.strip()
        reasons: List[str] = []

        # 1. Empty Check
        if not text:
            return CriticReport(
                verdict=CriticVerdict.REPAIR,
                score=0.0,
                reasons=["Empty response"],
                repair_instruction="Provide a brief, non-empty response."
            )

        text_lower = text.lower()

        # 2. Safety Check (Explicit or harmful terms)
        banned = getattr(self.config, "banned_words", set())
        for b in banned:
            if b in text_lower:
                return CriticReport(
                    verdict=CriticVerdict.REJECT,
                    score=0.0,
                    reasons=[f"Contains banned word: {b}"],
                    repair_instruction=None
                )

        # 3. Persona / AI Meta Talk Check
        for cliche in BOT_CLICHE_PATTERNS:
            if cliche in text_lower:
                reasons.append("Contains generic AI disclaimer/cliche")
                return CriticReport(
                    verdict=CriticVerdict.REPAIR,
                    score=0.4,
                    reasons=reasons,
                    repair_instruction="Do not refer to yourself as an AI language model. Stay in Rukiya persona."
                )

        # 4. Stage Directions Check (*sighs*, (smiles))
        if BANNED_STAGE_DIRECTIONS.search(text):
            reasons.append("Contains stage directions or asterisks")
            return CriticReport(
                verdict=CriticVerdict.REPAIR,
                score=0.5,
                reasons=reasons,
                repair_instruction="Remove all asterisks, parentheses, and roleplay action descriptors."
            )

        # 5. Length Check
        if len(text) > self.max_length:
            reasons.append(f"Exceeds length limit ({len(text)} > {self.max_length})")
            return CriticReport(
                verdict=CriticVerdict.REPAIR,
                score=0.6,
                reasons=reasons,
                repair_instruction=f"Shorten response to strictly under {self.max_length} characters (1 short sentence)."
            )

        # Check sentence count (max 3 sentences)
        sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
        if len(sentences) > 3:
            reasons.append(f"Too many sentences ({len(sentences)} > 3)")
            return CriticReport(
                verdict=CriticVerdict.REPAIR,
                score=0.65,
                reasons=reasons,
                repair_instruction="Condense into 1 short sentence."
            )

        # 6. Anti-Repetition Check against recent bot replies
        if recent_responses:
            words_cand = set(re.findall(r"\w+", text_lower))
            for prev in recent_responses[-5:]:
                words_prev = set(re.findall(r"\w+", prev.lower()))
                if not words_cand or not words_prev:
                    continue
                intersection = len(words_cand & words_prev)
                union = len(words_cand | words_prev)
                sim = intersection / union if union > 0 else 0.0
                if sim > 0.75:
                    reasons.append("High repetition similarity with recent reply")
                    return CriticReport(
                        verdict=CriticVerdict.REPAIR,
                        score=0.5,
                        reasons=reasons,
                        repair_instruction="Vary your vocabulary and phrasing so you do not repeat recent replies."
                    )

        # 7. Evidence Consistency Check
        if plan.search_required and evidence_items:
            # If evidence has numbers or dates, check that candidate didn't claim it couldn't find anything
            if any(ev.score > 0.7 for ev in evidence_items) and (
                "i don't know" in text_lower or "i couldn't find" in text_lower
            ):
                reasons.append("Failed to leverage available verified evidence")
                return CriticReport(
                    verdict=CriticVerdict.REPAIR,
                    score=0.5,
                    reasons=reasons,
                    repair_instruction="Use the verified facts provided in the evidence to answer the question directly."
                )

        # 8. Anti-Hallucination on Unverified Current Information
        if (plan.freshness_required or plan.search_required) and (not verified_current_information or search_failed):
            price_match = re.search(r"(?:₹|\$|\b(?:rs|rupees|dollars|usd|inr)\b\s*\d+|\b\d+\s*(?:rs|rupees|dollars|inr|usd)\b|costs?\s+\d+)", text_lower)
            if price_match:
                reasons.append("Fabricated current price without search verification")
                return CriticReport(
                    verdict=CriticVerdict.REPAIR,
                    score=0.4,
                    reasons=reasons,
                    repair_instruction="Do not state a fabricated price or unverified current fact. Transparently state in character that you cannot verify the live price right now."
                )

        return CriticReport(
            verdict=CriticVerdict.PASS,
            score=1.0,
            reasons=["All evaluation checks passed."]
        )
