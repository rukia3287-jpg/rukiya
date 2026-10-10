"""Orchestrator-level tests for JEV strategy integration."""
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from services.ai_service import AIService
from services.config import Config
from services.decision_service import DecisionService
from services.identity_service import IdentityService
from services.jev_adapter import JevRecommendation
from services.memory_service import MemoryService
from services.models import ChatMessage, ResponseDecision
from services.orchestrator import RukiyaOrchestrator
from services.rate_limiter import RateLimiter
from services.safety_service import SafetyService


class FakeJevAdapter:
    mode = "active"
    enabled = True
    configured = True
    respond_threshold = 0.72
    ignore_threshold = 0.20

    def __init__(self, recommendation):
        self.recommendation = recommendation
        self.recommend = AsyncMock(return_value=recommendation)
        self.aclose = AsyncMock()


class JevOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "jev_orch.db")
        self.config = Config(db_path=self.db_path, ai_cooldown=0)
        self.memory = MemoryService(self.config)
        self.identity = IdentityService(self.memory)
        self.safety = SafetyService(self.config)
        self.decision = DecisionService(self.config)
        self.limiter = RateLimiter(self.config)
        self.ai = AIService(self.config)
        self.ai._call_openrouter = AsyncMock(return_value="That was unexpectedly clever, chat.")
        self.fake_jev = FakeJevAdapter(
            JevRecommendation(0.94, "banter", "witty", "short", 14.0)
        )
        self.orchestrator = RukiyaOrchestrator(
            config=self.config,
            identity_service=self.identity,
            memory_service=self.memory,
            decision_service=self.decision,
            safety_service=self.safety,
            rate_limiter=self.limiter,
            ai_service=self.ai,
            jev_adapter=self.fake_jev,
        )

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def message(self, text="The stream is getting chaotic"):
        return ChatMessage(
            platform="youtube",
            message_id="jev-test-message",
            user_id="UC_jev_test",
            username="viewer",
            display_name="Viewer",
            text=text,
        )

    async def test_active_jev_can_recommend_joining_non_direct_chat(self):
        self.decision.decide = MagicMock(return_value=ResponseDecision(
            should_respond=False, intent="chatter", priority=0.2,
            reason="below threshold", extra={"is_direct_mention": False},
        ))
        result = await self.orchestrator.process_message(self.message())

        self.assertIsNotNone(result)
        self.assertTrue(self.fake_jev.recommend.await_count == 1)
        prompt_messages = self.ai._call_openrouter.await_args.args[0]
        self.assertIn("ADAPTIVE CONVERSATION STRATEGY", prompt_messages[-1]["content"])
        self.assertIn("dry, observant wit", prompt_messages[-1]["content"])

    async def test_jev_cannot_suppress_direct_mentions(self):
        self.fake_jev.recommend.return_value = JevRecommendation(
            0.02, "acknowledge", "neutral", "short", 10.0
        )
        self.decision.decide = MagicMock(return_value=ResponseDecision(
            should_respond=True, intent="question", priority=0.8,
            reason="direct mention", extra={"is_direct_mention": True},
        ))
        result = await self.orchestrator.process_message(self.message("rukiya what do you think?"))
        self.assertIsNotNone(result)

    async def test_jev_cannot_override_hard_denial(self):
        self.decision.decide = MagicMock(return_value=ResponseDecision(
            should_respond=False, intent="ignored", response_mode="ignore",
            reason="message contains banned word", extra={},
        ))
        result = await self.orchestrator.process_message(self.message("blocked input"))

        self.assertIsNone(result)
        self.fake_jev.recommend.assert_not_awaited()
        self.ai._call_openrouter.assert_not_awaited()

    async def test_low_jev_probability_can_suppress_non_direct_chat(self):
        self.fake_jev.recommend.return_value = JevRecommendation(
            0.05, "join_conversation", "neutral", "short", 10.0
        )
        self.decision.decide = MagicMock(return_value=ResponseDecision(
            should_respond=True, intent="chatter", priority=0.65,
            reason="heuristic response", extra={"is_direct_mention": False},
        ))
        result = await self.orchestrator.process_message(self.message())
        self.assertIsNone(result)
        self.ai._call_openrouter.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
