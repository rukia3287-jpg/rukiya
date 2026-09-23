"""tests/test_ai_engine_health_budget.py
Unit tests for AI Engine health, circuit breaker, budget manager, and caching/deduplication:
- test_provider_health
- test_circuit_breaker
- test_search_cache
- test_inflight_deduplication
- test_search_budget
"""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock

from services.ai_engine.budget import BudgetManager
from services.ai_engine.cache import InFlightDeduplicator, SearchResultCache, normalize_query
from services.ai_engine.health import CircuitBreaker, CircuitState, ProviderHealthTracker
from services.ai_engine.models import SearchResult
from services.config import Config


class TestAIEngineHealthAndBudget(unittest.IsolatedAsyncioTestCase):
    def test_provider_health(self):
        """Verify latency averages, success tracking, and health scores."""
        tracker = ProviderHealthTracker(failure_threshold=3)
        # Record 4 successful calls
        tracker.record_call("openrouter", success=True, latency_ms=100.0)
        tracker.record_call("openrouter", success=True, latency_ms=150.0)
        tracker.record_call("openrouter", success=True, latency_ms=200.0)
        tracker.record_call("openrouter", success=False, latency_ms=300.0)

        rec = tracker._get_or_create("openrouter")
        self.assertEqual(rec.total_calls, 4)
        self.assertEqual(rec.success_count, 3)
        self.assertEqual(rec.success_rate, 0.75)
        self.assertAlmostEqual(rec.average_latency, 150.0, delta=1.0)

        health_score = tracker.get_health_score("openrouter")
        self.assertGreater(health_score, 0.5)

    def test_circuit_breaker(self):
        """Verify state transitions: CLOSED -> OPEN -> HALF_OPEN -> CLOSED."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1, half_open_success_threshold=1)
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertTrue(cb.can_attempt())

        # First failure
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertTrue(cb.can_attempt())

        # Second failure -> trips to OPEN
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)
        self.assertFalse(cb.can_attempt())

        # Wait for recovery timeout
        time.sleep(0.12)
        # Calling can_attempt transitions to HALF_OPEN
        self.assertTrue(cb.can_attempt())
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)

        # Successful attempt in HALF_OPEN restores to CLOSED
        cb.record_success()
        self.assertEqual(cb.state, CircuitState.CLOSED)

    def test_search_cache(self):
        """Verify normalized cache keys and category TTL expiration."""
        cache = SearchResultCache(default_ttl=1, max_entries=10)
        sr = SearchResult(query="Genshin 5.0", text="Version 5.0 is Natlan", sources=[])

        # Store with custom TTL = 0.05s
        cache.set("Genshin  5.0?", sr, category="game", custom_ttl=0.05)

        # Immediate lookup succeeds with normalized query
        hit = cache.get("genshin 5.0")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.text, "Version 5.0 is Natlan")

        # After expiry
        time.sleep(0.08)
        miss = cache.get("genshin 5.0")
        self.assertIsNone(miss)

    async def test_inflight_deduplication(self):
        """Verify that multiple concurrent searches for the same query join a single in-flight task."""
        dedup = InFlightDeduplicator()
        call_count = 0

        async def _slow_search():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return "search result data"

        # Launch 5 concurrent calls for the same normalized query
        tasks = [
            dedup.execute_or_join("latest patch notes", _slow_search)
            for _ in range(5)
        ]
        results = await asyncio.gather(*tasks)

        self.assertEqual(len(results), 5)
        self.assertTrue(all(r == "search result data" for r in results))
        # The underlying search factory should have run only once!
        self.assertEqual(call_count, 1)

    def test_search_budget(self):
        """Verify daily search safety cap (default 450) and per-user limits."""
        budget = BudgetManager(daily_search_limit=3, user_daily_limit=2)
        self.assertEqual(budget.get_remaining_searches(), 3)
        self.assertTrue(budget.can_search("user_a"))

        # User A makes 2 searches
        budget.record_search("user_a")
        budget.record_search("user_a")
        self.assertEqual(budget.get_remaining_searches(), 1)

        # User A reaches user limit
        self.assertFalse(budget.can_search("user_a"))
        # User B still has budget
        self.assertTrue(budget.can_search("user_b"))

        # User B makes 1 search -> global limit reached (3/3)
        budget.record_search("user_b")
        self.assertEqual(budget.get_remaining_searches(), 0)
        self.assertFalse(budget.can_search("user_b"))


if __name__ == "__main__":
    unittest.main()
