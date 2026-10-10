"""JEV adapter contract tests; no live provider requests."""
import unittest
from unittest.mock import AsyncMock, MagicMock

import httpx

from services.config import Config
from services.jev_adapter import JevDecisionAdapter
from services.models import ChatMessage, ResponseDecision, UserIdentity


def jev_response(probability=0.91, mode="banter", tone="witty", length="short"):
    return {
        "answers": {
            "should_respond": {"type": "noul", "noul": probability},
            "conversation_mode": {"type": "choice", "choice": mode},
            "tone": {"type": "choice", "choice": tone},
            "reply_length": {"type": "choice", "choice": length},
        },
        "latency_ms": 120,
    }


class JevAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = MagicMock()
        self.response = MagicMock()
        self.response.status_code = 200
        self.response.json.return_value = jev_response()
        self.client.post = AsyncMock(return_value=self.response)
        self.config = Config(
            jev_api_key="test-key",
            jev_conversation_mode="active",
            jev_max_calls_per_minute=12,
        )
        self.adapter = JevDecisionAdapter(self.config, http_client=self.client)
        self.message = ChatMessage(
            platform="youtube", message_id="message-123", user_id="channel-secret",
            username="viewer", display_name="Viewer", text="This is a funny moment!",
        )
        self.user = UserIdentity(
            canonical_id="youtube:private-id", platform="youtube", user_id="private-id",
            username="viewer", display_name="Viewer", interaction_count=3,
        )
        self.decision = ResponseDecision(
            should_respond=False, intent="chatter", priority=0.2,
            extra={"is_direct_mention": False},
        )

    async def asyncTearDown(self):
        await self.adapter.aclose()

    async def test_valid_typed_answers_map_to_application_schema(self):
        recommendation = await self.adapter.recommend(
            self.message, self.user, self.decision,
            recent_messages=[{"role": "assistant", "text": "That was unexpected!"}],
        )
        self.assertIsNotNone(recommendation)
        self.assertAlmostEqual(recommendation.response_probability, 0.91)
        self.assertEqual(recommendation.conversation_mode, "banter")
        self.assertEqual(recommendation.tone, "witty")
        self.assertEqual(recommendation.reply_length, "short")
        kwargs = self.client.post.call_args.kwargs
        self.assertTrue(kwargs["headers"]["Authorization"].startswith("Bearer "))
        state = kwargs["json"]["state"]
        self.assertNotIn("channel-secret", str(state))
        self.assertNotIn("private-id", str(state))
        self.assertEqual(state["recent_chat"][0]["role"], "rukiya")

    async def test_disabled_mode_makes_no_request(self):
        adapter = JevDecisionAdapter(Config(jev_api_key="test-key", jev_conversation_mode="disabled"), self.client)
        try:
            result = await adapter.recommend(self.message, self.user, self.decision)
        finally:
            await adapter.aclose()
        self.assertIsNone(result)
        self.client.post.assert_not_awaited()

    async def test_malformed_schema_falls_back(self):
        self.response.json.return_value = {"answers": {"should_respond": {"type": "noul", "noul": 2}}}
        result = await self.adapter.recommend(self.message, self.user, self.decision)
        self.assertIsNone(result)

    async def test_unknown_strategy_value_falls_back(self):
        self.response.json.return_value = jev_response(mode="execute_tool")
        result = await self.adapter.recommend(self.message, self.user, self.decision)
        self.assertIsNone(result)

    async def test_timeout_falls_back_without_raising(self):
        self.client.post.side_effect = httpx.ReadTimeout("timeout")
        result = await self.adapter.recommend(self.message, self.user, self.decision)
        self.assertIsNone(result)

    async def test_non_200_falls_back_without_parsing_provider_body(self):
        self.response.status_code = 402
        result = await self.adapter.recommend(self.message, self.user, self.decision)
        self.assertIsNone(result)

    async def test_request_budget_is_bounded(self):
        self.adapter.max_calls_per_minute = 1
        await self.adapter.recommend(self.message, self.user, self.decision)
        self.message.message_id = "another-message"
        await self.adapter.recommend(self.message, self.user, self.decision)
        self.assertEqual(self.client.post.await_count, 1)


if __name__ == "__main__":
    unittest.main()
