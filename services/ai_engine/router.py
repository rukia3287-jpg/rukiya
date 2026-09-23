"""services/ai_engine/router.py
Adaptive multi-provider routing combining weighted multi-factor scoring with hard overriding rules.
"""
from __future__ import annotations

import logging
from typing import Optional

from services.ai_engine.budget import BudgetManager
from services.ai_engine.health import ProviderHealthTracker
from services.ai_engine.models import AIEngineRequest, Plan, RouteType
from services.config import Config

logger = logging.getLogger(__name__)


class Router:
    """Selects optimal execution route based on plan, provider health, budget, and persona needs."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()

    def route(
        self,
        plan: Plan,
        request: AIEngineRequest,
        health: ProviderHealthTracker,
        budget: BudgetManager
    ) -> RouteType:
        # Check provider availability
        or_avail = health.is_available("openrouter") and budget.can_execute_ai("openrouter", request.user_id)
        gem_avail = health.is_available("gemini") and budget.can_execute_ai("gemini", request.user_id)
        search_avail = gem_avail and budget.can_search(request.user_id)

        # ─── HARD OVERRIDES ──────────────────────────────────────
        # 1. Total Outage
        if not or_avail and not gem_avail:
            logger.warning("Both providers unavailable; routing to STATIC_FALLBACK.")
            return RouteType.STATIC_FALLBACK

        # 2. Search / Freshness Requirement
        if plan.search_required:
            if search_avail:
                # Consensus for rare high-complexity, high-uncertainty questions
                if plan.complexity > 0.85 and or_avail and request.platform != "youtube":
                    logger.info("Routing to CONSENSUS (high complexity %.2f).", plan.complexity)
                    return RouteType.CONSENSUS

                # Hybrid is the preferred mode for current info + Rukiya persona
                if or_avail:
                    logger.info("Routing to HYBRID (Gemini Search + OpenRouter Persona).")
                    return RouteType.HYBRID

                logger.info("OpenRouter unavailable; routing to GEMINI_SEARCH directly.")
                return RouteType.GEMINI_SEARCH
            else:
                logger.warning("Search required but search quota/provider unavailable.")
                if or_avail:
                    return RouteType.OPENROUTER_DIRECT
                return RouteType.GEMINI_DIRECT

        # 3. Provider Failures / Backups
        if not or_avail:
            logger.info("OpenRouter unavailable; falling back to GEMINI_DIRECT (BACKUP).")
            return RouteType.BACKUP if gem_avail else RouteType.STATIC_FALLBACK

        if not gem_avail:
            logger.info("Gemini unavailable; defaulting to OPENROUTER_DIRECT.")
            return RouteType.OPENROUTER_DIRECT

        # ─── WEIGHTED ROUTE SCORING ──────────────────────────────
        or_health = health.get_health_score("openrouter")
        gem_health = health.get_health_score("gemini")

        personality_need = 0.95
        conv_fit = 0.90 if plan.intent in ("chatter", "greeting", "compliment", "offtopic") else 0.50
        mem_fit = 0.80 if plan.memory_required else 0.50
        task_compat_or = 0.90 if not plan.search_required else 0.20

        openrouter_score = (
            personality_need * 0.20
            + conv_fit * 0.15
            + or_health * 0.20
            + 0.9 * 0.10  # low latency
            + 0.9 * 0.10  # low cost
            + mem_fit * 0.10
            + task_compat_or * 0.15
        )

        freshness = 1.0 if plan.freshness_required else 0.0
        search_need = 1.0 if plan.search_required else 0.0
        evidence_need = 0.9 if plan.verification_required else 0.1
        task_compat_gem = 0.90 if plan.search_required else 0.60

        gemini_score = (
            freshness * 0.25
            + search_need * 0.25
            + evidence_need * 0.15
            + gem_health * 0.15
            + task_compat_gem * 0.10
            + 0.9 * 0.10  # low cost
        )

        logger.debug("Route scores: OpenRouter=%.3f, Gemini=%.3f", openrouter_score, gemini_score)

        if gemini_score > openrouter_score and search_avail:
            return RouteType.HYBRID if or_avail else RouteType.GEMINI_SEARCH

        return RouteType.OPENROUTER_DIRECT
