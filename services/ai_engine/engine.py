"""services/ai_engine/engine.py
Central controller for the Rukiya AI Engine coordinating planning, routing, multi-provider execution,
evidence ranking, self-criticism, and repair loops.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional, Tuple

from services.ai_engine.budget import BudgetManager
from services.ai_engine.cache import InFlightDeduplicator, SearchResultCache
from services.ai_engine.context_compiler import ContextCompiler
from services.ai_engine.critic import Critic
from services.ai_engine.errors import ErrorCategory, ProviderError, classify_provider_error
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
        """Fetch search result from positive cache, negative cache, in-flight task, or Gemini provider."""
        # 1. Check positive cache
        cached = self.cache.get(query, time_scope=time_scope)
        if cached:
            logger.debug("Search cache hit for query: '%s'", query)
            return cached, True

        # 2. Check negative cache (fast fail during outage/rate-limit)
        neg = self.cache.get_negative(query)
        if neg:
            logger.info("Search negative cache hit for query '%s' (category=%s, retry_after=%.1fs)", query, neg.category.value, neg.retry_after - time.time())
            return SearchResult(
                query=query,
                text="",
                sources=[],
                citations=[],
                success=False,
                error=neg.error_message,
                error_details=ProviderError(
                    provider="gemini",
                    capability="search",
                    category=neg.category,
                    retryable=False
                )
            ), False

        # 3. Check circuit breaker specifically for gemini:search
        if not self.health_tracker.is_available("gemini:search") and "gemini:search" in getattr(self.health_tracker, "circuits", {}):
            logger.warning("Circuit breaker OPEN for gemini:search; fast-failing query '%s'", query)
            return SearchResult(
                query=query,
                text="",
                sources=[],
                citations=[],
                success=False,
                error="Circuit breaker OPEN for gemini:search",
                error_details=ProviderError(
                    provider="gemini",
                    capability="search",
                    category=ErrorCategory.RATE_LIMITED,
                    retryable=False
                )
            ), False

        gemini = self.registry.get("gemini")
        if not gemini or not gemini.is_configured():
            return SearchResult(
                query=query,
                text="",
                sources=[],
                citations=[],
                success=False,
                error="Gemini provider not configured",
                error_details=ProviderError(
                    provider="gemini",
                    capability="search",
                    category=ErrorCategory.AUTH_ERROR,
                    retryable=False
                )
            ), False

        # 4. In-flight request deduplication
        async def _fetch() -> SearchResult:
            res = await self.executor.execute_search(gemini, query=query)
            err_details = getattr(res, "error_details", None)
            if res.error and not err_details:
                err_details = classify_provider_error(res.error, provider="gemini", capability="search")

            is_success = not bool(res.error) and bool(res.text or res.citations)
            self.health_tracker.record_call(
                "gemini:search",
                is_success,
                res.latency_ms,
                error=err_details
            )

            if not is_success:
                cat = err_details.category if err_details else ErrorCategory.UNKNOWN
                # Store in negative cache so subsequent calls fail fast without network spam
                self.cache.set_negative(query, category=cat, error_message=str(res.error))
                return SearchResult(
                    query=query,
                    text="",
                    sources=[],
                    citations=[],
                    success=False,
                    error=res.error or "Search execution returned empty result",
                    error_details=err_details
                )

            sources = res.citations or []
            sr = SearchResult(
                query=query,
                text=res.text,
                sources=sources,
                citations=sources,
                success=True,
                error=None,
                error_details=None
            )
            # Record in budget & positive cache
            self.budget_manager.record_search()
            self.cache.set(query, sr, category=category, time_scope=time_scope)
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

        planned_route = route.value
        executed_route = route.value
        provider_used = "none"
        final_provider = "none"
        search_attempted = False
        search_succeeded = False
        verified_current_information = False
        fallback_used = False
        fallback_reason: Optional[str] = None

        candidate_text = ""
        cache_hit = False
        citations: List[dict] = []
        evidence_items: List[EvidenceItem] = []
        evidence_quality: Optional[float] = None
        error_msg: Optional[str] = None
        repair_count = 0

        openrouter = self.registry.get("openrouter")
        gemini = self.registry.get("gemini")

        # 3. Execution per Route
        try:
            if route == RouteType.STATIC_FALLBACK:
                candidate_text = self.get_fallback(plan.intent)
                fallback_used = True
                fallback_reason = "static_fallback"
                provider_used = "static_fallback"
                final_provider = "static_fallback"

            elif route == RouteType.OPENROUTER_DIRECT:
                provider_used = "openrouter"
                final_provider = "openrouter"
                messages = self.compiler.compile(request, plan)
                res = await self.executor.execute_generate(openrouter, messages)  # type: ignore
                self.health_tracker.record_call("openrouter:generation", not bool(res.error), res.latency_ms, error=res.error_details)

                if res.text and not res.error:
                    candidate_text = res.text
                    self.budget_manager.record_generation("openrouter", request.user_id)
                else:
                    # Automatic BACKUP to Gemini
                    logger.warning("OpenRouter failed; triggering BACKUP route to Gemini.")
                    fallback_used = True
                    fallback_reason = "openrouter_failed"
                    if gemini and self.health_tracker.is_available("gemini:generation"):
                        b_res = await self.executor.execute_generate(gemini, messages)
                        self.health_tracker.record_call("gemini:generation", not bool(b_res.error), b_res.latency_ms, error=b_res.error_details)
                        if b_res.text and not b_res.error:
                            candidate_text = b_res.text
                            provider_used = "gemini_backup"
                            final_provider = "gemini_backup"
                            executed_route = "backup"
                            fallback_used = False
                            fallback_reason = None
                            self.budget_manager.record_generation("gemini", request.user_id)
                        else:
                            candidate_text = self.get_fallback(plan.intent)
                            provider_used = "static_fallback"
                            final_provider = "static_fallback"
                            executed_route = "static_fallback"
                    else:
                        candidate_text = self.get_fallback(plan.intent)
                        provider_used = "static_fallback"
                        final_provider = "static_fallback"
                        executed_route = "static_fallback"

            elif route in (RouteType.GEMINI_DIRECT, RouteType.BACKUP):
                provider_used = "gemini"
                final_provider = "gemini"
                messages = self.compiler.compile(request, plan)
                res = await self.executor.execute_generate(gemini, messages)  # type: ignore
                self.health_tracker.record_call("gemini:generation", not bool(res.error), res.latency_ms, error=res.error_details)

                if res.text and not res.error:
                    candidate_text = res.text
                    self.budget_manager.record_generation("gemini", request.user_id)
                else:
                    candidate_text = self.get_fallback(plan.intent)
                    provider_used = "static_fallback"
                    final_provider = "static_fallback"
                    fallback_used = True
                    fallback_reason = "gemini_generation_failed"

            elif route in (RouteType.HYBRID, RouteType.GEMINI_SEARCH, RouteType.CONSENSUS):
                search_attempted = True
                query_to_search = plan.search_queries[0].query if plan.search_queries else request.text
                category = plan.search_queries[0].category if plan.search_queries else "general"
                time_scope = plan.search_queries[0].time_scope if plan.search_queries else ""

                sr, hit = await self._execute_search_grounding(
                    query_to_search, category=category, time_scope=time_scope
                )
                cache_hit = hit
                citations = sr.citations
                search_succeeded = bool(sr.success and (sr.text.strip() or sr.sources))

                if search_succeeded:
                    verified_current_information = True
                    evidence_items = self.evidence_engine.evaluate_sources(sr.sources, query_to_search)
                    has_conflict, _ = self.evidence_engine.detect_conflicts(evidence_items)
                    evidence_quality = self.evidence_engine.calculate_evidence_quality(evidence_items, has_conflict)

                    if route == RouteType.GEMINI_SEARCH or not openrouter or not self.health_tracker.is_available("openrouter:generation"):
                        provider_used = "gemini_search"
                        final_provider = "gemini_search"
                        if sr.text.strip():
                            candidate_text = sr.text.strip()
                        else:
                            candidate_text = self.get_fallback("search_unavailable")
                            provider_used = "static_fallback"
                            final_provider = "static_fallback"
                            fallback_used = True
                            fallback_reason = "gemini_search_empty"
                    else:
                        # HYBRID / CONSENSUS: OpenRouter synthesizes evidence into Rukiya persona
                        provider_used = "hybrid (gemini+openrouter)"
                        final_provider = "openrouter"
                        hybrid_messages = self.compiler.compile(
                            request=request,
                            plan=plan,
                            evidence_items=evidence_items,
                            search_attempted=True,
                            search_succeeded=True,
                            verified_current_information=True
                        )
                        gen_res = await self.executor.execute_generate(openrouter, hybrid_messages)
                        self.health_tracker.record_call("openrouter:generation", not bool(gen_res.error), gen_res.latency_ms, error=gen_res.error_details)
                        if gen_res.text and not gen_res.error:
                            candidate_text = gen_res.text
                            self.budget_manager.record_generation("openrouter", request.user_id)
                        elif sr.text.strip():
                            candidate_text = sr.text.strip()
                            provider_used = "gemini_search"
                            final_provider = "gemini_search"
                            fallback_used = True
                            fallback_reason = "openrouter_failed"
                        else:
                            candidate_text = self.get_fallback(plan.intent)
                            provider_used = "static_fallback"
                            final_provider = "static_fallback"
                            fallback_used = True
                            fallback_reason = "all_providers_failed"
                else:
                    # Search failed fast (e.g. 429 rate limit or quota exceeded)
                    verified_current_information = False
                    fallback_used = True
                    executed_route = "degraded_hybrid" if route in (RouteType.HYBRID, RouteType.CONSENSUS) else "degraded_search"
                    err_cat = sr.error_details.category.value if sr.error_details else "failed"
                    fallback_reason = f"gemini_search_{err_cat.lower()}"

                    # Provide structured failure context to final generator with strict anti-hallucination directive
                    if openrouter and self.health_tracker.is_available("openrouter:generation"):
                        provider_used = "degraded_hybrid (openrouter)"
                        final_provider = "openrouter"
                        hybrid_messages = self.compiler.compile(
                            request=request,
                            plan=plan,
                            evidence_items=[],
                            search_attempted=True,
                            search_succeeded=False,
                            search_failure_reason=fallback_reason,
                            verified_current_information=False
                        )
                        gen_res = await self.executor.execute_generate(openrouter, hybrid_messages)
                        self.health_tracker.record_call("openrouter:generation", not bool(gen_res.error), gen_res.latency_ms, error=gen_res.error_details)
                        if gen_res.text and not gen_res.error:
                            candidate_text = gen_res.text
                            self.budget_manager.record_generation("openrouter", request.user_id)
                        elif gemini and self.health_tracker.is_available("gemini:generation"):
                            b_res = await self.executor.execute_generate(gemini, hybrid_messages)
                            self.health_tracker.record_call("gemini:generation", not bool(b_res.error), b_res.latency_ms, error=b_res.error_details)
                            if b_res.text and not b_res.error:
                                candidate_text = b_res.text
                                provider_used = "degraded_hybrid (gemini)"
                                final_provider = "gemini"
                                self.budget_manager.record_generation("gemini", request.user_id)
                            else:
                                candidate_text = self.get_fallback("search_unavailable")
                                provider_used = "static_fallback"
                                final_provider = "static_fallback"
                        else:
                            candidate_text = self.get_fallback("search_unavailable")
                            provider_used = "static_fallback"
                            final_provider = "static_fallback"
                    elif gemini and self.health_tracker.is_available("gemini:generation"):
                        provider_used = "degraded_hybrid (gemini)"
                        final_provider = "gemini"
                        hybrid_messages = self.compiler.compile(
                            request=request,
                            plan=plan,
                            evidence_items=[],
                            search_attempted=True,
                            search_succeeded=False,
                            search_failure_reason=fallback_reason,
                            verified_current_information=False
                        )
                        b_res = await self.executor.execute_generate(gemini, hybrid_messages)
                        self.health_tracker.record_call("gemini:generation", not bool(b_res.error), b_res.latency_ms, error=b_res.error_details)
                        if b_res.text and not b_res.error:
                            candidate_text = b_res.text
                            self.budget_manager.record_generation("gemini", request.user_id)
                        else:
                            candidate_text = self.get_fallback("search_unavailable")
                            provider_used = "static_fallback"
                            final_provider = "static_fallback"
                    else:
                        candidate_text = self.get_fallback("search_unavailable")
                        provider_used = "static_fallback"
                        final_provider = "static_fallback"

        except Exception as e:
            logger.exception("AI Engine execution exception: %s", e)
            candidate_text = self.get_fallback(plan.intent)
            fallback_used = True
            fallback_reason = "exception"
            provider_used = "static_fallback"
            final_provider = "static_fallback"
            error_msg = str(e)

        # 4. Self-Critic & Repair Loop
        if candidate_text and final_provider != "static_fallback":
            report = self.critic.evaluate(
                candidate_text,
                request=request,
                plan=plan,
                evidence_items=evidence_items,
                verified_current_information=verified_current_information,
                search_failed=(search_attempted and not search_succeeded)
            )
            if report.verdict == CriticVerdict.REPAIR:
                logger.info("Critic requested REPAIR: %s", report.reasons)

                # Generator lambda for repair
                async def _repair_gen(msgs: List[dict]) -> AIProviderResult:
                    target_prov = openrouter if "openrouter" in final_provider else (gemini or openrouter)
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
                    candidate_text = self.get_fallback(plan.intent if not (search_attempted and not search_succeeded) else "search_unavailable")
                    fallback_used = True
                    fallback_reason = "critic_repair_exhausted"
                    final_provider = "static_fallback"

            elif report.verdict == CriticVerdict.REJECT:
                logger.warning("Critic REJECTED response due to safety: %s", report.reasons)
                candidate_text = self.get_fallback(plan.intent)
                fallback_used = True
                fallback_reason = "critic_rejected"
                final_provider = "static_fallback"

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
            "event=ai_engine_result req_id=%s planned_route=%s executed_route=%s provider=%s search_attempted=%s search_succeeded=%s verified_current=%s fallback=%s fallback_reason=%s hit=%s repairs=%d latency_ms=%.1f",
            request_id, planned_route, executed_route, final_provider, search_attempted, search_succeeded, verified_current_information, fallback_used, fallback_reason, cache_hit, repair_count, total_latency_ms
        )

        return AIEngineResult(
            text=validated,
            provider=provider_used,
            route=executed_route,
            used_search=search_attempted,
            citations=citations,
            evidence_quality=evidence_quality,
            confidence=0.9 if (not fallback_used and (not search_attempted or search_succeeded)) else 0.5,
            latency_ms=total_latency_ms,
            fallback_used=fallback_used,
            cache_hit=cache_hit,
            repair_count=repair_count,
            cost_class=plan.cost_class.value,
            request_id=request_id,
            error=error_msg,
            planned_route=planned_route,
            executed_route=executed_route,
            final_provider=final_provider,
            search_attempted=search_attempted,
            search_succeeded=search_succeeded,
            verified_current_information=verified_current_information,
            fallback_reason=fallback_reason,
        )

    async def aclose(self) -> None:
        """Cancel in-flight deduplicated search tasks and cleanly shut down resources."""
        if hasattr(self.deduplicator, "cancel_all"):
            res = self.deduplicator.cancel_all()
            if asyncio.iscoroutine(res):
                await res
        logger.info("AIEngine shut down cleanly.")
