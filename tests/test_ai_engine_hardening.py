"""tests/test_ai_engine_hardening.py
Hardening and regression test suite for Rukiya AI Engine:
1. Fast-fail on HTTP 429 (no 15s timeout wait).
2. Accurate fallback telemetry (fallback_used=True, executed_route=degraded_hybrid, search_succeeded=False).
3. Capability independence: gemini_generation and gemini_search are separate.
4. Error normalization (429, 403, 401, 400, 500, 503, network, timeout).
5. Negative caching and in-flight deduplication.
6. Anti-hallucination boundary and critic verification.
7. Priority handling (HIGH > LOW).
8. Adaptive circuit breaker state transitions and decay.
"""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services.ai_engine.budget import BudgetManager
from services.ai_engine.cache import InFlightDeduplicator, SearchResultCache
from services.ai_engine.critic import Critic
from services.ai_engine.engine import AIEngine
from services.ai_engine.errors import ErrorCategory, ProviderError, classify_provider_error
from services.ai_engine.health import CircuitState, ProviderHealthTracker
from services.ai_engine.models import (
    AIEngineRequest,
    AIProviderResult,
    CapabilityState,
    CriticVerdict,
    Plan,
    RequestPriority,
    RouteType,
    SearchQuery,
    SearchResult,
)
from services.ai_engine.provider_registry import ProviderRegistry
from services.ai_engine.providers.base import AIProvider
from services.config import Config


class MockProvider(AIProvider):
    def __init__(self, name: str, is_configured_val: bool = True):
        self.name = name
        self._configured = is_configured_val
        self.generate_mock = AsyncMock()
        self.search_mock = AsyncMock()

    def is_configured(self) -> bool:
        return self._configured

    async def generate(self, messages, **kwargs) -> AIProviderResult:
        return await self.generate_mock(messages, **kwargs)

    async def search_grounded(self, query: str, **kwargs) -> AIProviderResult:
        return await self.search_mock(query, **kwargs)

    async def health_check(self) -> bool:
        return True


class TestAIEngineHardening(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.config = Config()
        self.mock_or = MockProvider("openrouter", True)
        self.mock_gem = MockProvider("gemini", True)

        self.registry = ProviderRegistry(self.config)
        self.registry.register(self.mock_or)
        self.registry.register(self.mock_gem)

        self.engine = AIEngine(config=self.config, registry=self.registry)

    async def test_search_429_fails_fast_and_reports_fallback(self):
        """Regression test for observed production issue:
        Gemini Search receives 429 -> must fail fast (no 15s wait) ->
        capability state becomes RATE_LIMITED -> OpenRouter succeeds ->
        telemetry accurately reports fallback_used=True, executed_route='degraded_hybrid',
        and verified_current_information=False.
        """
        # Gemini search returns HTTP 429
        self.mock_gem.search_mock.return_value = AIProviderResult(
            text="",
            provider="gemini",
            used_search=True,
            error="Google GenAI 429 Rate Limited: Resource has been exhausted (e.g. check quota)",
            error_details=ProviderError(
                provider="gemini",
                capability="search",
                category=ErrorCategory.RATE_LIMITED,
                status_code=429,
                retryable=False
            ),
            latency_ms=45.0
        )

        # OpenRouter responds to degraded request
        self.mock_or.generate_mock.return_value = AIProviderResult(
            text="I can't verify the current Steam price right now.",
            provider="openrouter",
            latency_ms=120.0
        )

        req = AIEngineRequest(
            text="Rukiya GTA 5 ka price kitna hai Steam me",
            author="viewer1",
            intent="question"
        )

        start = time.time()
        res = await self.engine.process(req)
        elapsed = time.time() - start

        # 1. Fail fast: elapsed time must be well under 1.5 seconds (NOT 15 seconds)
        self.assertLess(elapsed, 2.0, f"Expected fail-fast execution, took {elapsed:.2f}s")

        # 2. Telemetry must be accurate
        self.assertEqual(res.planned_route, RouteType.HYBRID.value)
        self.assertEqual(res.executed_route, "degraded_hybrid")
        self.assertEqual(res.final_provider, "openrouter")
        self.assertTrue(res.fallback_used, "fallback_used must be True when search failed")
        self.assertEqual(res.fallback_reason, "gemini_search_rate_limited")
        self.assertTrue(res.used_search)
        self.assertFalse(res.search_succeeded)
        self.assertFalse(res.verified_current_information)

        # 3. Search capability state is RATE_LIMITED
        self.assertEqual(
            self.engine.health_tracker.get_capability_state("gemini", capability="search"),
            CapabilityState.RATE_LIMITED
        )

        # 4. Gemini generation capability remains AVAILABLE (independent capability!)
        self.assertTrue(self.engine.health_tracker.is_available("gemini:generation"))
        self.assertEqual(
            self.engine.health_tracker.get_capability_state("gemini", capability="generation"),
            CapabilityState.AVAILABLE
        )

    async def test_capability_independence_search_failure_does_not_break_generation(self):
        """Trip search circuit breaker to OPEN; verify Gemini generation still works normally."""
        # Trip search breaker with 3 failures
        for _ in range(3):
            self.engine.health_tracker.record_call(
                "gemini:search",
                success=False,
                latency_ms=50.0,
                error=ProviderError("gemini", "search", ErrorCategory.RATE_LIMITED, 429)
            )

        self.assertFalse(self.engine.health_tracker.is_available("gemini:search"))
        self.assertTrue(self.engine.health_tracker.is_available("gemini:generation"))

        # Test direct generation with Gemini
        self.mock_gem.generate_mock.return_value = AIProviderResult(
            text="Gemini generation output.",
            provider="gemini",
            latency_ms=100.0
        )

        # Simulate OpenRouter unavailable so it routes to Gemini
        self.engine.health_tracker.record_call("openrouter:generation", False, 100.0)
        self.engine.health_tracker.record_call("openrouter:generation", False, 100.0)
        self.engine.health_tracker.record_call("openrouter:generation", False, 100.0)

        req = AIEngineRequest(text="Hello", author="viewer", intent="greeting")
        res = await self.engine.process(req)
        self.assertEqual(res.final_provider, "gemini")
        self.assertIn("Gemini generation output", res.text)

    async def test_negative_cache_prevents_network_spam_on_429(self):
        """A 429 failure is cached negatively; subsequent queries fail immediately without network call."""
        self.mock_gem.search_mock.return_value = AIProviderResult(
            text="",
            provider="gemini",
            used_search=True,
            error="429 Too Many Requests",
            error_details=ProviderError("gemini", "search", ErrorCategory.RATE_LIMITED, 429, retryable=False)
        )
        self.mock_or.generate_mock.return_value = AIProviderResult(
            text="I can't verify that right now.",
            provider="openrouter"
        )

        req1 = AIEngineRequest(text="What is the latest Genshin update?", author="user1", intent="question")
        await self.engine.process(req1)
        self.assertEqual(self.mock_gem.search_mock.await_count, 1)

        # Immediate repeat query from another user
        req2 = AIEngineRequest(text="What is the latest Genshin update?", author="user2", intent="question")
        await self.engine.process(req2)
        # Search mock was NOT called again because negative cache intercepted it!
        self.assertEqual(self.mock_gem.search_mock.await_count, 1)

    async def test_in_flight_deduplication(self):
        """Simultaneous search requests for the same query join a single in-flight task."""
        dedup = InFlightDeduplicator()
        call_count = 0

        async def slow_search():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return SearchResult(query="test query", text="search answer", sources=[])

        # 10 concurrent requests
        tasks = [dedup.execute_or_join("test query", slow_search) for _ in range(10)]
        results = await asyncio.gather(*tasks)

        self.assertEqual(len(results), 10)
        self.assertEqual(call_count, 1, "Expected exactly 1 network execution for 10 concurrent callers")
        self.assertEqual(dedup.active_count, 0, "All completed tasks should be cleaned up")

    def test_error_classification_all_categories(self):
        """Verify centralized error classification across all provider and tool error types."""
        cases = [
            ("Resource has been exhausted 429", 429, ErrorCategory.RATE_LIMITED, False),
            ("quota exceeded for current quota metric", 403, ErrorCategory.QUOTA_EXHAUSTED, False),
            ("401 Unauthorized API key invalid", 401, ErrorCategory.AUTH_ERROR, False),
            ("400 Bad Request invalid json", 400, ErrorCategory.BAD_REQUEST, False),
            ("500 Internal Server Error", 500, ErrorCategory.SERVER_ERROR, True),
            ("503 Service Unavailable backend down", 503, ErrorCategory.SERVER_ERROR, True),
            ("ClientConnectorError connection refused", None, ErrorCategory.NETWORK_ERROR, True),
            ("asyncio.TimeoutError request timed out after 15s", 408, ErrorCategory.TIMEOUT, True),
            ("some unknown exception", None, ErrorCategory.UNKNOWN, False),
        ]
        for msg, status, expected_cat, expected_retry in cases:
            err = classify_provider_error(msg, status_code=status, provider="gemini", capability="search")
            self.assertEqual(err.category, expected_cat, f"Failed for '{msg}'")
            self.assertEqual(err.retryable, expected_retry, f"Retry mismatch for '{msg}'")

    def test_anti_hallucination_critic(self):
        """Critic flags fabricated prices or live claims when verified_current_information is False."""
        critic = Critic(self.config)
        plan = Plan(intent="question", search_required=True, freshness_required=True)
        req = AIEngineRequest(text="GTA 5 Steam price", author="viewer")

        # 1. Hallucinated response claiming price without verification
        hallucinated_text = "GTA 5 costs ₹999 on Steam right now."
        report = critic.evaluate(
            hallucinated_text,
            request=req,
            plan=plan,
            verified_current_information=False,
            search_failed=True
        )
        self.assertEqual(report.verdict, CriticVerdict.REPAIR)
        self.assertIn("Fabricated current price", report.reasons[0])

        # 2. Honest transparent response acknowledging lack of live verification
        honest_text = "I can't verify the current Steam price right now, check the store page."
        report2 = critic.evaluate(
            honest_text,
            request=req,
            plan=plan,
            verified_current_information=False,
            search_failed=True
        )
        self.assertEqual(report2.verdict, CriticVerdict.PASS)

    def test_priority_handling(self):
        """Constrained search budget drops optional search for LOW priority requests."""
        budget = BudgetManager(self.config)
        # Artificially consume 85% of daily searches
        budget.daily_searches = int(budget.max_daily_searches * 0.85)

        health = ProviderHealthTracker()
        router = self.engine.router
        plan = Plan(intent="question", search_required=True, freshness_required=True)

        low_req = AIEngineRequest(text="random trivia", author="idle_user", priority=RequestPriority.LOW)
        route_low = router.route(plan, low_req, health, budget)
        # Search dropped to save budget for direct questions
        self.assertEqual(route_low, RouteType.OPENROUTER_DIRECT)

        high_req = AIEngineRequest(text="random trivia", author="active_user", priority=RequestPriority.HIGH)
        route_high = router.route(plan, high_req, health, budget)
        # Search retained for high priority user
        self.assertEqual(route_high, RouteType.HYBRID)

    def test_adaptive_circuit_breaker_transitions(self):
        """Adaptive circuit breaker uses category-specific cooldowns and state transitions."""
        tracker = ProviderHealthTracker()
        
        # 3 rate-limit failures
        err_429 = ProviderError("gemini", "search", ErrorCategory.RATE_LIMITED, 429)
        tracker.record_call("gemini:search", False, 50.0, error=err_429)
        tracker.record_call("gemini:search", False, 50.0, error=err_429)
        tracker.record_call("gemini:search", False, 50.0, error=err_429)

        # Circuit should be OPEN
        self.assertEqual(tracker.get_circuit_state("gemini:search"), CircuitState.OPEN)
        self.assertFalse(tracker.is_available("gemini:search"))

        # Fast forward time past 60s cooldown
        with patch("time.time", return_value=time.time() + 65.0):
            # Probe allowed (HALF_OPEN)
            self.assertTrue(tracker.is_available("gemini:search"))
            self.assertEqual(tracker.get_circuit_state("gemini:search"), CircuitState.HALF_OPEN)

            # Successful probe closes circuit
            tracker.record_call("gemini:search", True, 80.0)
            self.assertEqual(tracker.get_circuit_state("gemini:search"), CircuitState.CLOSED)
            self.assertTrue(tracker.is_available("gemini:search"))

    async def test_clean_shutdown(self):
        """aclose() cleans up in-flight deduplicator tasks without throwing."""
        await self.engine.aclose()


if __name__ == "__main__":
    unittest.main()
