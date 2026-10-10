"""tests/test_ai_engine_budget_semantics.py
Budget accounting policy (see services/ai_engine/budget.py):
- global caps count dispatched provider operations (searches, generations, backups, repairs);
- the per-user cap counts logical requests served by a provider, reserved before dispatch;
- caps hold under concurrency and reset at the UTC day boundary.
"""
import asyncio
import unittest
import unittest.mock
from unittest.mock import AsyncMock, patch

from services.ai_engine.budget import BudgetManager
from services.ai_engine.engine import AIEngine
from services.ai_engine.models import AIEngineRequest, AIProviderResult, RouteType
from services.ai_engine.provider_registry import ProviderRegistry
from services.ai_engine.providers.base import AIProvider
from services.config import Config


class StubProvider(AIProvider):
    def __init__(self, name: str, configured: bool = True):
        self.name = name
        self._configured = configured
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


def _search_ok(query="q", **_kwargs):
    return AIProviderResult(
        text=f"Grounded answer for {query}.",
        provider="gemini",
        used_search=True,
        citations=[{"url": "https://example.com", "title": "Example"}],
    )


class BudgetSemanticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = Config()
        self.openrouter = StubProvider("openrouter")
        self.gemini = StubProvider("gemini")
        self.openrouter.generate_mock.return_value = AIProviderResult(text="Fine, here you go.", provider="openrouter")
        self.gemini.generate_mock.return_value = AIProviderResult(text="Gemini says hi.", provider="gemini")
        self.gemini.search_mock.side_effect = _search_ok

    def _engine(self, budget, openrouter=None):
        registry = ProviderRegistry(self.config)
        registry.register(openrouter or self.openrouter)
        registry.register(self.gemini)
        return AIEngine(config=self.config, registry=registry, budget_manager=budget)

    @staticmethod
    def _search_request(n, user="viewer"):
        return AIEngineRequest(text=f"latest genshin update number {n}", author=user, user_id=user, intent="question")

    @staticmethod
    def _chat_request(n, user="viewer"):
        return AIEngineRequest(text=f"hello there friend {n}", author=user, user_id=user, intent="chatter")

    async def test_fresh_search_costs_one_unit_and_cache_hit_costs_none(self):
        budget = BudgetManager(self.config, daily_search_limit=10, user_daily_limit=10)
        engine = self._engine(budget)

        await engine.process(self._search_request(1))
        await engine.process(self._search_request(1))  # identical query -> positive cache

        self.assertEqual(self.gemini.search_mock.await_count, 1)
        self.assertEqual(budget.search_count, 1)
        self.assertEqual(budget.user_counts["viewer"], 2)

    async def test_failed_search_still_consumes_the_search_cap(self):
        budget = BudgetManager(self.config, daily_search_limit=10, user_daily_limit=10)
        engine = self._engine(budget)
        self.gemini.search_mock.side_effect = None
        self.gemini.search_mock.return_value = AIProviderResult(text="", provider="gemini", used_search=True, error="500 boom")

        await engine.process(self._search_request(1))

        self.assertEqual(self.gemini.search_mock.await_count, 1)
        self.assertEqual(budget.search_count, 1)

    async def test_search_cap_blocks_dispatch_even_if_routing_allowed_it(self):
        budget = BudgetManager(self.config, daily_search_limit=1, user_daily_limit=10)
        engine = self._engine(budget)
        await engine.process(self._search_request(1))

        # Simulate a request that was routed to search before the cap filled up.
        with patch.object(budget, "can_search", return_value=True):
            result = await engine.process(self._search_request(2))

        self.assertEqual(self.gemini.search_mock.await_count, 1)
        self.assertFalse(result.search_succeeded)
        self.assertEqual(budget.search_count, 1)

    async def test_global_generation_cap_counts_failures_backups_and_stops_dispatch(self):
        budget = BudgetManager(self.config, global_daily_limit=2, user_daily_limit=10)
        engine = self._engine(budget)
        self.openrouter.generate_mock.return_value = AIProviderResult(text="", provider="openrouter", error="503 down")

        first = await engine.process(self._chat_request(1))
        self.assertEqual(first.final_provider, "gemini_backup")
        # The failed OpenRouter call and the Gemini backup were both dispatched.
        self.assertEqual(budget.openrouter_count, 1)
        self.assertEqual(budget.gemini_count, 1)

        second = await engine.process(self._chat_request(2))
        self.assertEqual(second.final_provider, "static_fallback")
        self.assertEqual(self.openrouter.generate_mock.await_count, 1)
        self.assertEqual(self.gemini.generate_mock.await_count, 1)
        self.assertEqual(budget.user_counts["viewer"], 1)

    async def test_repair_generations_count_toward_the_global_cap(self):
        budget = BudgetManager(self.config, user_daily_limit=10)
        engine = self._engine(budget)
        # An AI-disclaimer cliche cannot be fixed locally, so the critic triggers an LLM repair.
        self.openrouter.generate_mock.side_effect = [
            AIProviderResult(text="As an AI, I cannot say.", provider="openrouter"),
            AIProviderResult(text="Fine, welcome in.", provider="openrouter"),
        ]

        result = await engine.process(self._chat_request(1))

        self.assertGreaterEqual(result.repair_count, 1)
        self.assertEqual(self.openrouter.generate_mock.await_count, 2)
        self.assertEqual(budget.openrouter_count, 2)
        self.assertEqual(budget.user_counts["viewer"], 1)

    async def test_concurrent_requests_cannot_exceed_the_user_cap(self):
        budget = BudgetManager(self.config, user_daily_limit=2)
        engine = self._engine(budget)

        async def slow_generate(messages, **kwargs):
            await asyncio.sleep(0.05)
            return AIProviderResult(text="Fine, here you go.", provider="openrouter")

        self.openrouter.generate_mock.side_effect = slow_generate

        results = await asyncio.gather(*(engine.process(self._chat_request(i)) for i in range(6)))

        served = [r for r in results if r.final_provider != "static_fallback"]
        self.assertEqual(len(served), 2)
        self.assertEqual(self.openrouter.generate_mock.await_count, 2)
        self.assertEqual(budget.user_counts["viewer"], 2)

    async def test_concurrent_requests_cannot_exceed_the_search_cap(self):
        budget = BudgetManager(self.config, daily_search_limit=2, user_daily_limit=50)
        engine = self._engine(budget)

        async def slow_search(query, **kwargs):
            await asyncio.sleep(0.05)
            return _search_ok(query)

        self.gemini.search_mock.side_effect = slow_search

        await asyncio.gather(*(engine.process(self._search_request(i, user=f"u{i}")) for i in range(6)))

        self.assertEqual(self.gemini.search_mock.await_count, 2)
        self.assertEqual(budget.search_count, 2)

    async def test_unserved_request_refunds_the_user_reservation(self):
        budget = BudgetManager(self.config, user_daily_limit=1)
        engine = self._engine(budget)
        self.openrouter.generate_mock.return_value = AIProviderResult(text="", provider="openrouter", error="boom")
        self.gemini.generate_mock.return_value = AIProviderResult(text="", provider="gemini", error="boom")

        first = await engine.process(self._chat_request(1))
        self.assertEqual(first.final_provider, "static_fallback")
        self.assertEqual(budget.user_counts.get("viewer", 0), 0)

        # The refunded unit is still available once providers recover.
        self.openrouter.generate_mock.return_value = AIProviderResult(text="Back again.", provider="openrouter")
        second = await engine.process(self._chat_request(2))
        self.assertEqual(second.final_provider, "openrouter")
        self.assertEqual(budget.user_counts["viewer"], 1)

    async def test_concurrent_searches_cannot_exceed_the_global_cap(self):
        budget = BudgetManager(self.config, daily_search_limit=10, global_daily_limit=2, user_daily_limit=50)
        engine = self._engine(budget)

        async def slow_search(query, **kwargs):
            await asyncio.sleep(0.05)
            return _search_ok(query)

        self.gemini.search_mock.side_effect = slow_search

        await asyncio.gather(*(engine.process(self._search_request(i, user=f"u{i}")) for i in range(6)))

        self.assertEqual(self.gemini.search_mock.await_count, 2)
        self.assertLessEqual(budget.openrouter_count + budget.gemini_count, 2)

    async def test_generation_over_the_global_cap_is_refused_without_dispatch(self):
        budget = BudgetManager(self.config, global_daily_limit=0, user_daily_limit=10)
        engine = self._engine(budget)
        engine.health_tracker.record_call = unittest.mock.MagicMock()

        # Simulate a request that was routed before the cap filled up.
        with patch.object(budget, "can_execute_ai", return_value=True):
            result = await engine.process(self._chat_request(1))

        self.assertEqual(result.final_provider, "static_fallback")
        self.assertEqual(result.fallback_reason, "budget_exhausted")
        self.openrouter.generate_mock.assert_not_awaited()
        self.gemini.generate_mock.assert_not_awaited()
        engine.health_tracker.record_call.assert_not_called()
        self.assertEqual(budget.user_counts.get("viewer", 0), 0)

    async def test_cancelled_request_refunds_the_user_reservation(self):
        budget = BudgetManager(self.config, user_daily_limit=10)
        engine = self._engine(budget)
        started = asyncio.Event()

        async def hanging_generate(messages, **kwargs):
            started.set()
            await asyncio.sleep(30)

        self.openrouter.generate_mock.side_effect = hanging_generate

        task = asyncio.create_task(engine.process(self._chat_request(1)))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(budget.user_counts.get("viewer", 0), 0)

    async def test_unconfigured_provider_is_not_dispatched_and_backup_still_runs(self):
        budget = BudgetManager(self.config, user_daily_limit=10)
        unconfigured = StubProvider("openrouter", configured=False)
        engine = self._engine(budget, openrouter=unconfigured)

        with patch.object(engine.router, "route", return_value=RouteType.OPENROUTER_DIRECT):
            result = await engine.process(self._chat_request(1))

        self.assertEqual(result.final_provider, "gemini_backup")
        unconfigured.generate_mock.assert_not_awaited()
        self.assertEqual(budget.openrouter_count, 0)
        self.assertEqual(budget.gemini_count, 1)

    def test_day_rollover_resets_every_counter(self):
        # A search is also a Gemini operation, so it uses one of the two global slots.
        budget = BudgetManager(self.config, daily_search_limit=1, global_daily_limit=2, user_daily_limit=1)
        with patch.object(BudgetManager, "_get_today", return_value="2026-10-10"):
            budget.current_date = "2026-10-10"
            self.assertTrue(budget.try_reserve_search())
            self.assertTrue(budget.try_reserve_generation("openrouter"))
            self.assertTrue(budget.try_reserve_user_request("viewer"))
            self.assertFalse(budget.try_reserve_search())
            self.assertFalse(budget.try_reserve_generation("gemini"))
            self.assertFalse(budget.try_reserve_user_request("viewer"))

        with patch.object(BudgetManager, "_get_today", return_value="2026-10-11"):
            self.assertTrue(budget.try_reserve_search())
            self.assertTrue(budget.try_reserve_generation("gemini"))
            self.assertTrue(budget.try_reserve_user_request("viewer"))
            self.assertEqual(budget.current_date, "2026-10-11")

    def test_requests_without_a_user_id_are_not_user_limited(self):
        budget = BudgetManager(self.config, user_daily_limit=1)
        self.assertTrue(budget.try_reserve_user_request(None))
        self.assertTrue(budget.try_reserve_user_request(None))
        self.assertEqual(budget.user_counts, {})


if __name__ == "__main__":
    unittest.main()
