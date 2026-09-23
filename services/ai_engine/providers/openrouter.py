"""services/ai_engine/providers/openrouter.py
OpenRouter provider adapter for conversational and personality generation.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from services.ai_engine.models import AIProviderResult
from services.ai_engine.providers.base import AIProvider
from services.config import Config

logger = logging.getLogger(__name__)


class OpenRouterProvider(AIProvider):
    """Adapter wrapping OpenRouter API calls with resilience, metrics, and timeouts."""

    name: str = "openrouter"

    def __init__(
        self,
        config: Optional[Config] = None,
        http_client: Optional[httpx.AsyncClient] = None
    ):
        self.config = config or Config()
        self.http_client = http_client
        self.api_key = getattr(self.config, "openrouter_api_key", None) or os.getenv("OPENROUTER_API_KEY")
        self.model = getattr(self.config, "openrouter_model", "deepseek/deepseek-r1")
        self.endpoint = getattr(self.config, "openrouter_endpoint", "https://openrouter.ai/api/v1/chat/completions")
        self.timeout = float(getattr(self.config, "openrouter_timeout", 20.0))

    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_key.strip())

    async def generate(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 150,
        temperature: float = 0.85,
        **kwargs: Any
    ) -> AIProviderResult:
        if not self.is_configured():
            return AIProviderResult(
                text="",
                provider=self.name,
                error="OpenRouter API key is not configured"
            )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/rukia3287-jpg/rukiya-bot",
            "X-Title": "Rukiya Bot"
        }

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        start_time = time.time()
        should_close = False
        client = self.http_client
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout)
            should_close = True

        try:
            for attempt in range(1, 4):
                try:
                    resp = await client.post(self.endpoint, json=payload, headers=headers)
                except httpx.RequestError as e:
                    logger.warning("OpenRouter network error (attempt %d): %s", attempt, e)
                    if attempt < 3:
                        await asyncio.sleep(2 ** (attempt - 1))
                        continue
                    latency_ms = (time.time() - start_time) * 1000.0
                    return AIProviderResult(
                        text="",
                        provider=self.name,
                        latency_ms=latency_ms,
                        error=f"Network error: {str(e)}"
                    )

                if resp.status_code in (429, 503):
                    logger.warning("OpenRouter rate-limited (%d). Attempt %d/3", resp.status_code, attempt)
                    if attempt < 3:
                        await asyncio.sleep(2 ** (attempt - 1))
                        continue
                    latency_ms = (time.time() - start_time) * 1000.0
                    return AIProviderResult(
                        text="",
                        provider=self.name,
                        latency_ms=latency_ms,
                        error=f"Rate limited: {resp.status_code}"
                    )

                if resp.status_code >= 400:
                    latency_ms = (time.time() - start_time) * 1000.0
                    return AIProviderResult(
                        text="",
                        provider=self.name,
                        latency_ms=latency_ms,
                        error=f"HTTP {resp.status_code}: {resp.text[:200]}"
                    )

                try:
                    data = resp.json()
                except Exception as e:
                    latency_ms = (time.time() - start_time) * 1000.0
                    return AIProviderResult(
                        text="",
                        provider=self.name,
                        latency_ms=latency_ms,
                        error=f"Invalid JSON: {str(e)}"
                    )

                choices = data.get("choices") or []
                for choice in choices:
                    if isinstance(choice, dict):
                        text = (choice.get("message") or {}).get("content") or choice.get("text")
                        if isinstance(text, str) and text.strip():
                            latency_ms = (time.time() - start_time) * 1000.0
                            return AIProviderResult(
                                text=text.strip(),
                                provider=self.name,
                                latency_ms=latency_ms,
                                confidence=0.9,
                                raw_response=data
                            )

                if isinstance(data.get("text"), str) and data["text"].strip():
                    latency_ms = (time.time() - start_time) * 1000.0
                    return AIProviderResult(
                        text=data["text"].strip(),
                        provider=self.name,
                        latency_ms=latency_ms,
                        confidence=0.9,
                        raw_response=data
                    )

                latency_ms = (time.time() - start_time) * 1000.0
                return AIProviderResult(
                    text="",
                    provider=self.name,
                    latency_ms=latency_ms,
                    error="Empty response from OpenRouter",
                    raw_response=data
                )
        finally:
            if should_close:
                await client.aclose()

        latency_ms = (time.time() - start_time) * 1000.0
        return AIProviderResult(
            text="",
            provider=self.name,
            latency_ms=latency_ms,
            error="Exhausted retry attempts"
        )

    async def search_grounded(
        self,
        query: str,
        system_instruction: Optional[str] = None,
        context: Optional[str] = None,
        max_tokens: int = 250,
        **kwargs: Any
    ) -> AIProviderResult:
        """Search grounding is delegated to Gemini."""
        return AIProviderResult(
            text="",
            provider=self.name,
            error="Search grounding not supported by OpenRouter provider"
        )

    async def health_check(self) -> bool:
        return self.is_configured()
