"""services/ai_engine/engine.py
Central controller for the Rukiya AI Engine coordinating planning, routing, multi-provider execution,
evidence ranking, self-criticism, and repair loops.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

from services.ai_engine.budget import BudgetManager
from services.ai_engine.cache import InFlightDeduplicator, SearchResultCache
from services.ai_engine.context_compiler import ContextCompiler
from services.ai_engine.critic import Critic
from services.ai_engine.evidence_engine import EvidenceEngine
from services.ai_engine.executor import Executor
from services.ai_engine.health import ProviderHealthTracker
from services.ai_engine.models import (
    AIEngineRequest,
    AIEngineResult,
    AIProviderResult,
    CriticVerdict,
    EvidenceItem,
    Plan,
    RouteType,
    SearchResult,
)
from services.ai_engine.planner import Planner
from services.ai_engine.provider_registry import ProviderRegistry
from services.ai_engine.repair import RepairEngine
from services.ai_engine.router import Router
from services.config import Config
from services.safety_service import validate_rukiya_response

logger = logging.getLogger(__name__)

# Deterministic Fallbacks
STATIC_FALLBACKS = {
    "greeting": "Hm. Welcome in.",
    "question": "I can't verify that properly right now.",
    "search_unavailable": "I can't verify that properly right now.",
    "temporary_failure": "Tch. Give me a second, chat.",
    "default": "Don't be reckless, chat."
}


class AIEngine:
    """
    Main controller coordinating:
    Understand -> Plan -> Context -> Route -> Execute -> Evidence -> Critic -> Repair -> Return.
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        registry: Optional[ProviderRegistry] = None,
        planner: Optional[Planner] = None,
        router: Optional[Router] = None,
        compiler: Optional[ContextCompiler] = None,
        evidence_engine: Optional[EvidenceEngine] = None,
        critic: Optional[Critic] = None,
        repair_engine: Optional[RepairEngine] = None,
        executor: Optional[Executor] = None,
        health_tracker: Optional[ProviderHealthTracker] = None,
        budget_manager: Optional[BudgetManager] = None,
        cache: Optional[SearchResultCache] = None,
        deduplicator: Optional[InFlightDeduplicator] = None
    ):
        self.config = config or Config()
        self.registry = registry or ProviderRegistry(self.config)
        self.planner = planner or Planner(self.config)
        self.router = router or Router(self.config)
        self.compiler = compiler or ContextCompiler(self.config)
        self.evidence_engine = evidence_engine or EvidenceEngine()
        self.critic = critic or Critic(self.config)
        self.repair_engine = repair_engine or RepairEngine(self.config)
        self.executor = executor or Executor(self.config)
        self.health_tracker = health_tracker or ProviderHealthTracker()
        self.budget_manager = budget_manager or BudgetManager(self.config)
        self.cache = cache or SearchResultCache(
            default_ttl=getattr(self.config, "gemini_search_cache_ttl", 300)
        )
        self.deduplicator = deduplicator or InFlightDeduplicator()

    def get_fallback(self, intent: Optional[str] = None) -> str:
        if intent == "greeting":
            return STATIC_FALLBACKS["greeting"]
        return STATIC_FALLBACKS.get(intent or "default", STATIC_FALLBACKS["default"])

    async def _execute_search_grounding(
        self,
        query: str,
        category: str = "general",
        time_scope: str = ""
    ) -> Tuple[SearchResult, bool]:
        """Fetch search result from cache, in-flight task, or Gemini provider."""
        # 1. Check cache
        cached = self.cache.get(query, time_scope=time_scope)
        if cached:
            logger.debug("Search cache hit for query: '%s'", query)
            return cached, True

        # 2. In-flight request deduplication
        gemini = self.registry.get("gemini")
        if not gemini or not gemini.is_configured():
            return SearchResult(query=query, text="", sources=[]), False

        async def _fetch():
            res = await self.executor.execute_search(gemini, query=query)
            sources = res.citations or []
            sr = SearchResult(
                query=query,
                text=res.text,
                sources=sources,
                citations=sources
            )
            # Record in budget & cache
            self.budget_manager.record_search()
            self.cache.set(query, sr, category=category, time_scope=time_scope)
            self.health_tracker.record_call("gemini", not bool(res.error), res.latency_ms)
            return sr

        result = await self.deduplicator.execute_or_join(query, _fetch)
        return result, False

    async def process(self, request: AIEngineRequest) -> AIEngineResult:
        start_time = time.time()
        request_id = request.request_id

        # 1. Planning
        plan = self.planner.plan(request)

        # 2. Routing
        route = self.router.route(plan, request, self.health_tracker, self.budget_manager)

        logger.info(
            "event=ai_engine_route req_id=%s route=%s intent=%s complexity=%.2f search_req=%s",
            request_id, route.value, plan.intent, plan.complexity, plan.search_required
        )

        candidate_text = ""
        provider_used = "none"
        used_search = False
        cache_hit = False
        citations: List[dict] = []
        evidence_items: List[EvidenceItem] = []
        evidence_quality: Optional[float] = None
        error_msg: Optional[str] = None
        repair_count = 0
        fallback_used = False

        openrouter = self.registry.get("openrouter")
        gemini = self.registry.get("gemini")

        # 3. Execution per Route
        try:
            if route == RouteType.STATIC_FALLBACK:
                candidate_text = self.get_fallback(plan.intent)
                fallback_used = True
                provider_used = "static_fallback"

            elif route == RouteType.OPENROUTER_DIRECT:
                provider_used = "openrouter"
                messages = self.compiler.compile(request, plan)
                res = await self.executor.execute_generate(openrouter, messages)  # type: ignore
                self.health_tracker.record_call("openrouter", not bool(res.error), res.latency_ms)

                if res.text and not res.error:
                    candidate_text = res.text
                    self.budget_manager.record_generation("openrouter", request.user_id)
                else:
                    # Automatic BACKUP to Gemini
                    logger.warning("OpenRouter failed; triggering BACKUP route to Gemini.")
                    if gemini and self.health_tracker.is_available("gemini"):
                        b_res = await self.executor.execute_generate(gemini, messages)
                        self.health_tracker.record_call("gemini", not bool(b_res.error), b_res.latency_ms)
                        if b_res.text and not b_res.error:
                            candidate_text = b_res.text
                            provider_used = "gemini_backup"
                            self.budget_manager.record_generation("gemini", request.user_id)
                        else:
                            candidate_text = self.get_fallback(plan.intent)
                            fallback_used = True
                    else:
                        candidate_text = self.get_fallback(plan.intent)
                        fallback_used = True

            elif route in (RouteType.GEMINI_DIRECT, RouteType.BACKUP):
                provider_used = "gemini"
                messages = self.compiler.compile(request, plan)
                res = await self.executor.execute_generate(gemini, messages)  # type: ignore
                self.health_tracker.record_call("gemini", not bool(res.error), res.latency_ms)

                if res.text and not res.error:
                    candidate_text = res.text
                    self.budget_manager.record_generation("gemini", request.user_id)
                else:
                    candidate_text = self.get_fallback(plan.intent)
                    fallback_used = True

            elif route in (RouteType.HYBRID, RouteType.GEMINI_SEARCH, RouteType.CONSENSUS):
                used_search = True
                query_to_search = plan.search_queries[0].query if plan.search_queries else request.text
                category = plan.search_queries[0].category if plan.search_queries else "general"
                time_scope = plan.search_queries[0].time_scope if plan.search_queries else ""

                sr, hit = await self._execute_search_grounding(
                    query_to_search, category=category, time_scope=time_scope
                )
                cache_hit = hit
                citations = sr.citations

                # Evaluate and Rank Evidence
                evidence_items = self.evidence_engine.evaluate_sources(sr.sources, query_to_search)
                has_conflict, _ = self.evidence_engine.detect_conflicts(evidence_items)
                evidence_quality = self.evidence_engine.calculate_evidence_quality(evidence_items, has_conflict)

                if route == RouteType.GEMINI_SEARCH or not openrouter or not self.health_tracker.is_available("openrouter"):
                    # Use Gemini result directly or format from evidence
                    provider_used = "gemini_search"
                    if sr.text.strip():
                        candidate_text = sr.text.strip()
                    else:
                        candidate_text = self.get_fallback("search_unavailable")
                        fallback_used = True
                else:
                    # HYBRID / CONSENSUS: OpenRouter synthesizes evidence into Rukiya persona
                    provider_used = "hybrid (gemini+openrouter)"
                    hybrid_messages = self.compiler.compile(
                        request=request,
                        plan=plan,
                        evidence_items=evidence_items
                    )
                    gen_res = await self.executor.execute_generate(openrouter, hybrid_messages)
                    self.health_tracker.record_call("openrouter", not bool(gen_res.error), gen_res.latency_ms)
                    if gen_res.text and not gen_res.error:
                        candidate_text = gen_res.text
                        self.budget_manager.record_generation("openrouter", request.user_id)
                    elif sr.text.strip():
                        # OpenRouter failed in hybrid, use Gemini search output
                        candidate_text = sr.text.strip()
                    else:
                        candidate_text = self.get_fallback(plan.intent)
                        fallback_used = True

        except Exception as e:
            logger.exception("AI Engine execution exception: %s", e)
            candidate_text = self.get_fallback(plan.intent)
            fallback_used = True
            error_msg = str(e)

        # 4. Self-Critic & Repair Loop
        if not fallback_used and candidate_text:
            report = self.critic.evaluate(
                candidate_text,
                request=request,
                plan=plan,
                evidence_items=evidence_items
            )
            if report.verdict == CriticVerdict.REPAIR:
                logger.info("Critic requested REPAIR: %s", report.reasons)

                # Generator lambda for repair
                async def _repair_gen(msgs: List[dict]) -> AIProviderResult:
                    target_prov = openrouter if "openrouter" in provider_used else (gemini or openrouter)
                    return await self.executor.execute_generate(target_prov, msgs)  # type: ignore

                repaired_text, rep_count, final_report = await self.repair_engine.execute_repair_loop(
                    initial_text=candidate_text,
                    initial_report=report,
                    request=request,
                    plan=plan,
                    evidence_items=evidence_items,
                    compiler=self.compiler,
                    critic=self.critic,
                    generate_fn=_repair_gen
                )
                candidate_text = repaired_text
                repair_count = rep_count

                if final_report.verdict != CriticVerdict.PASS:
                    logger.warning("Repair loop exhausted; adopting safe fallback.")
                    candidate_text = self.get_fallback(plan.intent)
                    fallback_used = True

            elif report.verdict == CriticVerdict.REJECT:
                logger.warning("Critic REJECTED response due to safety: %s", report.reasons)
                candidate_text = self.get_fallback(plan.intent)
                fallback_used = True

        # 5. Output Validation
        if not fallback_used:
            validated = validate_rukiya_response(candidate_text)
            max_len = int(getattr(self.config, "max_message_length", 250))
            if len(validated) > max_len:
                trimmed = validated[:max_len]
                last_dot = max(trimmed.rfind("."), trimmed.rfind("!"), trimmed.rfind("?"))
                validated = trimmed[:last_dot + 1] if last_dot > 0 else trimmed + "..."
        else:
            validated = candidate_text.strip()

        total_latency_ms = (time.time() - start_time) * 1000.0

        logger.info(
            "event=ai_engine_result req_id=%s provider=%s route=%s search=%s hit=%s repairs=%d fallback=%s latency_ms=%.1f",
            request_id, provider_used, route.value, used_search, cache_hit, repair_count, fallback_used, total_latency_ms
        )

        return AIEngineResult(
            text=validated,
            provider=provider_used,
            route=route.value,
            used_search=used_search,
            citations=citations,
            evidence_quality=evidence_quality,
            confidence=0.9 if not fallback_used else 0.5,
            latency_ms=total_latency_ms,
            fallback_used=fallback_used,
            cache_hit=cache_hit,
            repair_count=repair_count,
            cost_class=plan.cost_class.value,
            request_id=request_id,
            error=error_msg
        )
