import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from services.ai_engine.providers.local_llm import LocalLLMProvider
from services.config import Config


class TestLocalLLMProvider(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_without_base_url(self):
        provider = LocalLLMProvider(Config())
        self.assertFalse(provider.is_configured())

    async def test_openai_compatible_generation(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "Tch. State your problem."}}]
        }
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        provider = LocalLLMProvider(
            Config(local_llm_base_url="http://localhost:11434/v1", local_llm_model="qwen2.5:3b"),
            http_client=client,
        )
        result = await provider.generate(
            [{"role": "user", "content": "hi"}], max_tokens=50, temperature=0.4
        )
        self.assertEqual(result.text, "Tch. State your problem.")
        self.assertEqual(result.provider, "local_llm")
        payload = client.post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "qwen2.5:3b")
        self.assertFalse(payload["stream"])

    async def test_http_error_returns_structured_failure(self):
        response = MagicMock()
        response.status_code = 503
        response.text = "model is loading"
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        provider = LocalLLMProvider(
            Config(local_llm_base_url="http://localhost:11434/v1"),
            http_client=client,
        )
        result = await provider.generate([{"role": "user", "content": "hi"}])
        self.assertFalse(result.text)
        self.assertTrue(result.error)
        self.assertEqual(result.status_code, 503)


if __name__ == "__main__":
    unittest.main()
