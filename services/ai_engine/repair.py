"""services/ai_engine/repair.py
Bounded repair loop executing corrective prompt refinement under a strict attempt limit.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Coroutine, List, Optional, Tuple

from services.ai_engine.context_compiler import ContextCompiler
from services.ai_engine.critic import Critic
from services.ai_engine.models import (
    AIEngineRequest,
    AIProviderResult,
    CriticReport,
    CriticVerdict,
    EvidenceItem,
    Plan,
)
from services.config import Config

logger = logging.getLogger(__name__)


class RepairEngine:
    """Manages the iterative repair loop capped by AI_MAX_REPAIR_ATTEMPTS."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.max_attempts = int(getattr(self.config, "ai_max_repair_attempts", 2))

    @staticmethod
    def apply_deterministic_repairs(text: str, max_length: int = 250) -> str:
        """Apply fast zero-cost local repairs before resorting to LLM regeneration."""
        cleaned = text.strip()
        # Remove markdown quotes
        if (cleaned.startswith('"') and cleaned.endswith('"')) or (cleaned.startswith("'") and cleaned.endswith("'")):
            cleaned = cleaned[1:-1].strip()

        # Remove stage directions (*sighs*, (yawns))
        cleaned = re.sub(r"(\*[^*]+\*|\([^)]{2,}\))", "", cleaned).strip()

        # Collapse whitespace
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        # Length clamp
        if len(cleaned) > max_length:
            trimmed = cleaned[:max_length]
            last_dot = max(trimmed.rfind("."), trimmed.rfind("!"), trimmed.rfind("?"))
            cleaned = trimmed[:last_dot + 1] if last_dot > 0 else trimmed + "..."

        return cleaned

    async def execute_repair_loop(
        self,
        initial_text: str,
        initial_report: CriticReport,
        request: AIEngineRequest,
        plan: Plan,
        evidence_items: Optional[List[EvidenceItem]],
        compiler: ContextCompiler,
        critic: Critic,
        generate_fn: Callable[[List[dict]], Coroutine[Any, Any, AIProviderResult]],
        recent_responses: Optional[List[str]] = None
    ) -> Tuple[str, int, CriticReport]:
        """
        Iterates up to max_attempts to fix critic issues.
        Returns (final_text, repair_count, final_critic_report).
        """
        current_text = initial_text
        current_report = initial_report
        repair_count = 0

        # Try fast local deterministic repair first
        cleaned = self.apply_deterministic_repairs(current_text, critic.max_length)
        report_after_clean = critic.evaluate(
            cleaned, request, plan, evidence_items, recent_responses
        )
        if report_after_clean.verdict == CriticVerdict.PASS:
            logger.info("Deterministic repair resolved critic issue.")
            return cleaned, 0, report_after_clean

        current_text = cleaned
        current_report = report_after_clean

        while current_report.verdict == CriticVerdict.REPAIR and repair_count < self.max_attempts:
            repair_count += 1
            instruction = current_report.repair_instruction or "Condense into 1 short Rukiya-style sentence."
            logger.info("Executing LLM repair attempt %d/%d: %s", repair_count, self.max_attempts, instruction)

            # Recompile prompt with explicit correction directive
            repaired_messages = compiler.compile(
                request=request,
                plan=plan,
                evidence_items=evidence_items,
                repair_instruction=instruction
            )

            try:
                res = await generate_fn(repaired_messages)
                if not res or not res.text.strip():
                    logger.warning("Repair generation returned empty text on attempt %d", repair_count)
                    break

                cand_text = self.apply_deterministic_repairs(res.text, critic.max_length)
                current_text = cand_text
                current_report = critic.evaluate(
                    current_text, request, plan, evidence_items, recent_responses
                )

                if current_report.verdict == CriticVerdict.PASS:
                    logger.info("Repair attempt %d succeeded with PASS.", repair_count)
                    return current_text, repair_count, current_report

            except Exception as e:
                logger.warning("Exception during repair attempt %d: %s", repair_count, e)
                break

        return current_text, repair_count, current_report
