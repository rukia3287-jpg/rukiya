"""services/ai_engine/engine.py
Central controller for the Rukiya AI Engine coordinating planning, routing, multi-provider execution,
evidence ranking, self-criticism, and repair loops.
"""
from __future__ import annotations

import asyncio
import logging
import time
import hashlib
from collections import Counter, deque
from contextvars import ContextVar
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
from services.ai_engine.providers.base import AIProvider
from services.ai_engine.repair import RepairEngine
from services.ai_engine.router import Router
from services.config import Config
from services.brain import LocalSkills
from services.language import ENGLISH, detect_reply_language
from services.safety_service import SERIOUS_INTENT_INSTRUCTIONS, FALLBACK_INTENTS, localize_fallback, validate_rukiya_response

logger = logging.getLogger(__name__)

# Deterministic Fallbacks (single sentences; localized per request via localize_fallback)
STATIC_FALLBACKS = {
    "greeting": "Hm, welcome in.",
    "question": "I can't verify that properly right now.",
    "search_unavailable": "I can't verify that properly right now.",
    "temporary_failure": "Tch, give me a second, chat.",
    "default": "Don't be reckless, chat.",
    # Serious messages share the orchestrator's caring fallbacks.
    "crisis": FALLBACK_INTENTS["crisis"],
    "sensitive": FALLBACK_INTENTS["sensitive"],
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
        self.local_skills = LocalSkills()
        self._external_policy = ContextVar(f'external_{id(self)}', default=True)
        self._user_reservation = ContextVar(f'reservation_{id(self)}', default=None)
        self._search_scope = ContextVar(f'search_scope_{id(self)}', default='')
        self._search_owner = ContextVar(f'search_owner_{id(self)}', default='')
        self._active_tasks = set()
        self._closed = False
        self.provider_operations = Counter()
        self.request_metrics = deque(maxlen=200)

        # Synchronize Gemini provider with health tracker if registered
        gemini = self.registry.get("gemini")
        if gemini and hasattr(gemini, "set_health_tracker"):
            gemini.set_health_tracker(self.health_tracker)

    async def _generate(self, provider: AIProvider, messages: List[dict], *, track_health: bool = True) -> AIProviderResult:
        """Dispatch one generation call (primary, backup or repair).

        Every dispatch counts toward the global daily cap, whether it succeeds or not.
        Over the cap, or for a missing/unconfigured provider, the call is refused without
        dispatch, without being counted, and without touching provider health.
        """
        if provider is None or not provider.is_configured():
            name = getattr(provider, "name", "unknown")
            return AIProviderResult(
                text="",
                provider=name,
                error=f"Provider {name} is not configured",
                error_details=ProviderError(
                    provider=name,
                    capability="generation",
                    category=ErrorCategory.AUTH_ERROR,
                    retryable=False
                )
            )
        name = provider.name
        if name != 'local' and not (self.config.external_ai_enabled and self._external_policy.get()):
            return AIProviderResult('', name, error='External inference disabled by privacy policy')
        if sum(len(str(message.get('content', ''))) for message in messages) > self.config.brain_context_chars:
            return AIProviderResult('', name, error='Context exceeds configured size limit')
        if name != 'local' and not self.budget_manager.try_reserve_generation(name):
            return AIProviderResult(
                text="",
                provider=name,
                error="Daily AI generation budget exhausted",
                error_details=ProviderError(
                    provider=name,
                    capability="generation",
                    category=ErrorCategory.QUOTA_EXHAUSTED,
                    retryable=False
                )
            )
        self.provider_operations[name] += 1
        res = await self.executor.execute_generate(provider, messages)
        if track_health:
            self.health_tracker.record_call(f"{name}:generation", not bool(res.error), res.latency_ms, error=res.error_details)
        return res

    def get_fallback(self, intent: Optional[str] = None, language: str = ENGLISH) -> str:
        return localize_fallback(STATIC_FALLBACKS.get(intent or "default", STATIC_FALLBACKS["default"]), language)

    async def _execute_search_grounding(
        self,
        query: str,
        category: str = "general",
        time_scope: str = "",
        request_id: Optional[str] = None,
    ) -> Tuple[SearchResult, bool]:
        """Fetch search result from positive cache, negative cache, in-flight task, or Gemini provider."""
        if not (self.config.external_ai_enabled and self._external_policy.get()):
            return SearchResult(query=query, text='', success=False, error='External search disabled'), False
        # Both positive/negative entries and in-flight tasks are private to the
        # owning conversation. Stable IDs are case sensitive before hashing.
        scope = self._search_scope.get()
        owner = self._search_owner.get()
        partition = hashlib.sha256(owner.encode()).hexdigest() if owner else ''
        if partition:
            scope_hash = hashlib.sha256(scope.encode()).hexdigest()
            query_hash = hashlib.sha256(' '.join(query.casefold().split()).encode()).hexdigest()
            time_scope = f'{time_scope}|{partition}|{scope_hash}|{query_hash}'
        # 1. Check positive cache
        cached = self.cache.get(query, time_scope=time_scope)
        if cached:
            logger.debug("Search cache hit")
            return cached, True

        # 2. Check negative cache (fast fail during outage/rate-limit)
        neg = self.cache.get_negative(query, time_scope=time_scope)
        if neg:
            logger.info("Search negative cache hit category=%s", neg.category.value)
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
            logger.warning("Circuit breaker OPEN for gemini:search")
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
            # Reserve the search slot right before dispatch (no await in between), so
            # concurrent requests cannot overshoot the daily cap. Failed searches still count.
            if not self.budget_manager.try_reserve_search():
                return SearchResult(
                    query=query,
                    text="",
                    sources=[],
                    citations=[],
                    success=False,
                    error="Daily search budget exhausted",
                    error_details=ProviderError(
                        provider="gemini",
                        capability="search",
                        category=ErrorCategory.QUOTA_EXHAUSTED,
                        retryable=False
                    )
                )
            self.provider_operations['gemini_search'] += 1
            res = await self.executor.execute_search(gemini, query=query, request_id=request_id)
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
                self.cache.set_negative(query, category=cat, error_message=str(res.error), time_scope=time_scope)
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
            self.cache.set(query, sr, category=category, time_scope=time_scope)
            return sr

        cache_key = partition + hashlib.sha256(f'{scope}|{time_scope}|{query}'.encode()).hexdigest()
        result = await self.deduplicator.execute_or_join(cache_key, _fetch)
        return result, False

    async def process(self, request: AIEngineRequest) -> AIEngineResult:
        started = time.perf_counter()
        if self._closed or not request.text.strip() or len(request.text) > self.config.brain_prompt_chars:
            return AIEngineResult('That request is unavailable or too long.', fallback_used=True, error='request_rejected')
        external = self._external_policy.set(bool(request.allow_external))
        reservation = self._user_reservation.set(None)
        scope = self._search_scope.set(f'{request.platform}|{request.canonical_id or request.user_id or request.request_id}|{request.conversation_scope}')
        owner = self._search_owner.set(request.canonical_id or request.user_id or request.request_id)
        task = asyncio.current_task()
        self._active_tasks.add(task)
        try:
            async with asyncio.timeout(self.config.brain_timeout):
                local = self.local_skills.respond(request)
                if local is not None:
                    result = AIEngineResult(local, provider='deterministic', route='deterministic', cost_class='free')
                else:
                    result = await self._process(request)
                result.latency_ms = (time.perf_counter() - started) * 1000
                self.request_metrics.append({'ms': result.latency_ms, 'route': result.executed_route, 'error': bool(result.error)})
                return result
        except TimeoutError:
            return AIEngineResult(self.get_fallback('temporary_failure', detect_reply_language(request.text)),
                                  fallback_used=True, error='request_timeout')
        finally:
            reserved = self._user_reservation.get()
            if reserved:
                self.budget_manager.refund_user_request(reserved)
            self._user_reservation.reset(reservation)
            self._external_policy.reset(external)
            self._search_scope.reset(scope)
            self._search_owner.reset(owner)
            self._active_tasks.discard(task)

    async def _process(self, request: AIEngineRequest) -> AIEngineResult:
        # Fallback replies follow the viewer's language and script.
        language = detect_reply_language(request.text)

        def fallback(intent: Optional[str] = None) -> str:
            return self.get_fallback(intent, language)

        start_time = time.time()
        request_id = request.request_id

        # 1. Planning
        plan = self.planner.plan(request)

        # 2. Routing
        route = self.router.route(
            plan,
            request,
            self.health_tracker,
            self.budget_manager,
            registry=self.registry,
        )

        # Reserve the user's unit in the same synchronous step as the routing check, so
        # concurrent requests from one user cannot all pass the check before one is charged.
        user_reserved = False
        if route != RouteType.STATIC_FALLBACK:
            if self.budget_manager.try_reserve_user_request(request.user_id):
                user_reserved = bool(request.user_id)
                self._user_reservation.set(request.user_id if user_reserved else None)
            else:
                route = RouteType.STATIC_FALLBACK

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
            if route == RouteType.LOCAL:
                local = self.registry.get('local')
                res = await self._generate(local, self.compiler.compile(request, plan))
                if res.text and not res.error:
                    candidate_text = res.text
                    provider_used = final_provider = 'local'
                elif self.config.external_ai_enabled and request.allow_external:
                    # Preserve the existing primary/backup order after local failure.
                    for target in (openrouter, gemini):
                        if not target or not target.is_configured() or not self.health_tracker.is_available(target.name, capability='generation'):
                            continue
                        res = await self._generate(target, self.compiler.compile(request, plan))
                        if res.text and not res.error:
                            candidate_text = res.text
                            provider_used = final_provider = target.name
                            executed_route = 'local_external_fallback'
                            break
                    else:
                        candidate_text = fallback(plan.intent)
                        provider_used = final_provider = 'static_fallback'
                        fallback_used, fallback_reason = True, 'local_and_external_failed'
                else:
                    candidate_text = fallback(plan.intent)
                    provider_used = final_provider = 'static_fallback'
                    fallback_used, fallback_reason = True, 'local_unavailable_external_disabled'

            elif route == RouteType.STATIC_FALLBACK:
                candidate_text = fallback(plan.intent)
                fallback_used = True
                fallback_reason = "static_fallback"
                provider_used = "static_fallback"
                final_provider = "static_fallback"

            elif route == RouteType.OPENROUTER_DIRECT:
                provider_used = "openrouter"
                final_provider = "openrouter"
                messages = self.compiler.compile(request, plan)
                res = await self._generate(openrouter, messages)  # type: ignore[arg-type]

                if res.text and not res.error:
                    candidate_text = res.text
                else:
                    # Automatic BACKUP to Gemini
                    logger.warning("OpenRouter failed; triggering BACKUP route to Gemini.")
                    fallback_used = True
                    fallback_reason = "openrouter_failed"
                    if gemini and self.health_tracker.is_available("gemini:generation"):
                        b_res = await self._generate(gemini, messages)  # type: ignore[arg-type]
                        if b_res.text and not b_res.error:
                            candidate_text = b_res.text
                            provider_used = "gemini_backup"
                            final_provider = "gemini_backup"
                            executed_route = "backup"
                            fallback_used = False
                            fallback_reason = None
                        else:
                            candidate_text = fallback(plan.intent)
                            provider_used = "static_fallback"
                            final_provider = "static_fallback"
                            executed_route = "static_fallback"
                    else:
                        candidate_text = fallback(plan.intent)
                        provider_used = "static_fallback"
                        final_provider = "static_fallback"
                        executed_route = "static_fallback"

            elif route in (RouteType.GEMINI_DIRECT, RouteType.BACKUP):
                provider_used = "gemini"
                final_provider = "gemini"
                messages = self.compiler.compile(request, plan)
                res = await self._generate(gemini, messages)  # type: ignore[arg-type]

                if res.text and not res.error:
                    candidate_text = res.text
                else:
                    candidate_text = fallback(plan.intent)
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
                    query_to_search,
                    category=category,
                    time_scope=time_scope,
                    request_id=request_id,
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
                            candidate_text = fallback("search_unavailable")
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
                        gen_res = await self._generate(openrouter, hybrid_messages)  # type: ignore[arg-type]
                        if gen_res.text and not gen_res.error:
                            candidate_text = gen_res.text
                        elif sr.text.strip():
                            candidate_text = sr.text.strip()
                            provider_used = "gemini_search"
                            final_provider = "gemini_search"
                            fallback_used = True
                            fallback_reason = "openrouter_failed"
                        else:
                            candidate_text = fallback(plan.intent)
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

                    logger.warning(
                        "event=ai_engine_fallback request_id=%s from=gemini_search to=openrouter reason=%s",
                        request_id, err_cat.lower()
                    )

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
                        gen_res = await self._generate(openrouter, hybrid_messages)  # type: ignore[arg-type]
                        if gen_res.text and not gen_res.error:
                            candidate_text = gen_res.text
                        elif gemini and self.health_tracker.is_available("gemini:generation"):
                            b_res = await self._generate(gemini, hybrid_messages)  # type: ignore[arg-type]
                            if b_res.text and not b_res.error:
                                candidate_text = b_res.text
                                provider_used = "degraded_hybrid (gemini)"
                                final_provider = "gemini"
                            else:
                                candidate_text = fallback("search_unavailable")
                                provider_used = "static_fallback"
                                final_provider = "static_fallback"
                        else:
                            candidate_text = fallback("search_unavailable")
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
                        b_res = await self._generate(gemini, hybrid_messages)  # type: ignore[arg-type]
                        if b_res.text and not b_res.error:
                            candidate_text = b_res.text
                        else:
                            candidate_text = fallback("search_unavailable")
                            provider_used = "static_fallback"
                            final_provider = "static_fallback"
                    else:
                        candidate_text = fallback("search_unavailable")
                        provider_used = "static_fallback"
                        final_provider = "static_fallback"

        except Exception as e:
            logger.exception("AI Engine execution exception: %s", e)
            candidate_text = fallback(plan.intent)
            fallback_used = True
            fallback_reason = "exception"
            provider_used = "static_fallback"
            final_provider = "static_fallback"
            error_msg = str(e)
        except asyncio.CancelledError:
            # A cancelled request (e.g. the YouTube monitor stopping) was not served.
            raise

        if final_provider == "static_fallback" and route != RouteType.STATIC_FALLBACK and self.budget_manager.generation_budget_exhausted():
            # Telemetry: the global cap, not a provider failure, is why nothing answered.
            fallback_reason = "budget_exhausted"

        # One logical request costs one unit of the user's daily AI budget, however many
        # provider operations (search + synthesis, backup) served it. Static fallbacks are free.
        if user_reserved and final_provider in ("none", "static_fallback"):
            self.budget_manager.refund_user_request(request.user_id)
            self._user_reservation.set(None)

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
                    target_prov = self.registry.get('local') if final_provider == 'local' else openrouter if "openrouter" in final_provider else (gemini or openrouter)
                    return await self._generate(target_prov, msgs, track_health=False)  # type: ignore[arg-type]

                repaired_text, rep_count, final_report = await self.repair_engine.execute_repair_loop(
                    initial_text=candidate_text,
                    initial_report=report,
                    request=request,
                    plan=plan,
                    evidence_items=evidence_items,
                    compiler=self.compiler,
                    critic=self.critic,
                    generate_fn=_repair_gen,
                    verified_current_information=verified_current_information,
                    search_failed=(search_attempted and not search_succeeded),
                )
                candidate_text = repaired_text
                repair_count = rep_count

                if final_report.verdict != CriticVerdict.PASS:
                    logger.warning("Repair loop exhausted; adopting safe fallback.")
                    candidate_text = fallback(plan.intent if not (search_attempted and not search_succeeded) else "search_unavailable")
                    fallback_used = True
                    fallback_reason = "critic_repair_exhausted"
                    final_provider = "static_fallback"

            elif report.verdict == CriticVerdict.REJECT:
                logger.warning("Critic REJECTED response due to safety: %s", report.reasons)
                candidate_text = fallback(plan.intent)
                fallback_used = True
                fallback_reason = "critic_rejected"
                final_provider = "static_fallback"

        # 5. Output Validation
        if not fallback_used:
            fallback_text = fallback(plan.intent)
            validated = validate_rukiya_response(candidate_text, fallback=fallback_text, max_sentences=1 if plan.intent in SERIOUS_INTENT_INSTRUCTIONS else 2)
            if validated == fallback_text and candidate_text.strip() != fallback_text:
                fallback_used = True
                fallback_reason = fallback_reason or "output_safety_rejected"
                provider_used = "static_fallback"
                final_provider = "static_fallback"

            max_len = int(getattr(self.config, "max_message_length", 250))
            if len(validated) > max_len:
                trimmed = validated[:max_len]
                last_dot = max(trimmed.rfind("."), trimmed.rfind("!"), trimmed.rfind("?"))
                validated = trimmed[:last_dot + 1] if last_dot > 0 else trimmed + "..."
        else:
            validated = candidate_text.strip()

        total_latency_ms = (time.time() - start_time) * 1000.0
        if final_provider not in ('none', 'static_fallback'):
            self._user_reservation.set(None)

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

    async def clear_user_cache(self, canonical):
        partition = hashlib.sha256(canonical.encode()).hexdigest()
        await self.deduplicator.cancel_partition(partition)
        self.cache.clear_partition(partition)

    async def aclose(self) -> None:
        """Cancel in-flight deduplicated search tasks and cleanly shut down resources."""
        self._closed = True
        active = self._active_tasks - {asyncio.current_task()}
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        for provider in getattr(self.registry, '_providers', {}).values():
            if hasattr(provider, 'aclose'):
                await provider.aclose()
        if hasattr(self.deduplicator, "cancel_all"):
            res = self.deduplicator.cancel_all()
            if asyncio.iscoroutine(res):
                await res
        logger.info("AIEngine shut down cleanly.")
