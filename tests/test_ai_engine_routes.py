"""tests/test_ai_engine_routes.py
Unit tests for AI Engine multi-provider routing:
- test_openrouter_route
- test_gemini_route
- test_gemini_search
- test_hybrid_route
- test_backup_route
- test_final_fallback
All APIs are 100% mocked; no live keys or network required.
"""
import unittest
from unittest.mock import AsyncMock, MagicMock

from services.ai_engine import (
    AIEngine,
    AIEngineRequest,
    AIProviderResult,
    ProviderRegistry,
    RouteType,
)
from services.ai_engine.budget import BudgetManager
from services.ai_engine.health import ProviderHealthTracker
from services.ai_engine.providers.base import AIProvider
from services.config import Config


class MockProvider(AIProvider):
    def __init__(self, name: str, configured: bool = True):
        self.name = name
        self._configured = configured
        self.generate_mock = AsyncMock(return_value=AIProviderResult(
            text=f"Response from {name}",
            provider=name,
            latency_ms=10.0
        ))
        self.search_mock = AsyncMock(return_value=AIProviderResult(
            text=f"Search grounded result from {name}",
            provider=name,
            used_search=True,
            citations=[{"url": "https://genshin.hoyoverse.com", "title": "Genshin Official"}],
            latency_ms=15.0
        ))

    def is_configured(self) -> bool:
        return self._configured

    async def generate(self, messages, **kwargs):
        return await self.generate_mock(messages, **kwargs)

    async def search_grounded(self, query, **kwargs):
        return await self.search_mock(query, **kwargs)

    async def health_check(self) -> bool:
        return self._configured


class TestAIEngineRoutes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = Config(openrouter_api_key="mock_or", gemini_api_key="mock_gem")
        self.registry = ProviderRegistry(self.config)

        self.mock_or = MockProvider("openrouter", configured=True)
        self.mock_gem = MockProvider("gemini", configured=True)

        self.registry.register(self.mock_or)
        self.registry.register(self.mock_gem)

        self.engine = AIEngine(config=self.config, registry=self.registry)

    async def test_openrouter_route(self):
        """Casual chatting routes to OPENROUTER_DIRECT."""
        req = AIEngineRequest(
            text="hey rukiya how are you doing today?",
            author="viewer1",
            intent="chatter"
        )
        res = await self.engine.process(req)
        self.assertEqual(res.route, RouteType.OPENROUTER_DIRECT.value)
        self.assertIn("openrouter", res.provider)
        self.assertFalse(res.used_search)
        self.mock_or.generate_mock.assert_awaited()

    async def test_gemini_route(self):
        """When OpenRouter is unavailable, routes to GEMINI_DIRECT / BACKUP."""
        self.engine.health_tracker.record_call("openrouter", success=False, latency_ms=100.0)
        self.engine.health_tracker.record_call("openrouter", success=False, latency_ms=100.0)
        self.engine.health_tracker.record_call("openrouter", success=False, latency_ms=100.0)
        # OpenRouter breaker is now OPEN

        req = AIEngineRequest(
            text="hello rukiya tell me something interesting",
            author="viewer1",
            intent="chatter"
        )
        res = await self.engine.process(req)
        self.assertIn("gemini", res.provider)
        self.mock_gem.generate_mock.assert_awaited()

    async def test_gemini_search(self):
        """When OpenRouter is unavailable and current info is requested, routes to GEMINI_SEARCH directly."""
        self.engine.health_tracker.record_call("openrouter", success=False, latency_ms=100.0)
        self.engine.health_tracker.record_call("openrouter", success=False, latency_ms=100.0)
        self.engine.health_tracker.record_call("openrouter", success=False, latency_ms=100.0)

        req = AIEngineRequest(
            text="what is the latest update for the patch?",
            author="viewer2",
            intent="question"
        )
        res = await self.engine.process(req)
        self.assertEqual(res.route, RouteType.GEMINI_SEARCH.value)
        self.assertTrue(res.used_search)
        self.mock_gem.search_mock.assert_awaited()

    async def test_hybrid_route(self):
        """When current info is requested and both providers are available, routes to HYBRID."""
        req = AIEngineRequest(
            text="what's the latest Genshin update codes?",
            author="viewer3",
            intent="question"
        )
        res = await self.engine.process(req)
        self.assertEqual(res.route, RouteType.HYBRID.value)
        self.assertTrue(res.used_search)
        self.mock_gem.search_mock.assert_awaited()
        self.mock_or.generate_mock.assert_awaited()
        self.assertIn("hybrid", res.provider)

    async def test_backup_route(self):
        """If OpenRouter fails during OPENROUTER_DIRECT execution, engine switches to Gemini backup."""
        self.mock_or.generate_mock.return_value = AIProviderResult(
            text="",
            provider="openrouter",
            error="503 Service Unavailable"
        )
        self.mock_gem.generate_mock.return_value = AIProviderResult(
            text="Backup from gemini.",
            provider="gemini",
            latency_ms=20.0
        )

        req = AIEngineRequest(
            text="hey rukiya are you ready for stream?",
            author="viewer4",
            intent="chatter"
        )
        res = await self.engine.process(req)
        self.assertEqual(res.provider, "gemini_backup")
        self.assertIn("Backup from gemini", res.text)
        self.assertFalse(res.fallback_used)

    async def test_final_fallback(self):
        """When all providers fail or are unconfigured, return deterministic fallback."""
        self.mock_or.generate_mock.return_value = AIProviderResult(text="", provider="openrouter", error="Down")
        self.mock_gem.generate_mock.return_value = AIProviderResult(text="", provider="gemini", error="Down")

        req = AIEngineRequest(
            text="hey rukiya hello",
            author="viewer5",
            intent="greeting"
        )
        res = await self.engine.process(req)
        self.assertTrue(res.fallback_used)
        self.assertEqual(res.text, "Hm. Welcome in.")


if __name__ == "__main__":
    unittest.main()
