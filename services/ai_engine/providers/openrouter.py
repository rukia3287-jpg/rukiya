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

from services.ai_engine.errors import ErrorCategory, ProviderError, classify_provider_error
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
        self.reasoning_enabled = bool(getattr(self.config, "ai_reasoning_enabled", True))
        self.reasoning_effort = str(getattr(self.config, "ai_reasoning_effort", "medium"))

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
            "max_completion_tokens": max_tokens,
            "temperature": temperature,
        }
        reasoning_enabled = kwargs.get("reasoning_enabled", self.reasoning_enabled)
        reasoning_effort = str(kwargs.get("reasoning_effort", self.reasoning_effort)).lower()
        if reasoning_enabled and reasoning_effort in {"low", "medium", "high", "xhigh"}:
            payload["reasoning"] = {"effort": reasoning_effort}

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
                    err = classify_provider_error(
                        e, provider=self.name, capability="generation"
                    )
                    if attempt < 3:
                        await asyncio.sleep(2 ** (attempt - 1))
                        continue
                    latency_ms = (time.time() - start_time) * 1000.0
                    return AIProviderResult(
                        text="",
                        provider=self.name,
                        latency_ms=latency_ms,
                        error=str(err),
                        error_category=err.category,
                        status_code=err.status_code,
                        error_details=err
                    )

                if resp.status_code == 429:
                    # Rate limits should fail fast so we do not hammer an already
                    # constrained provider with three immediate retries.
                    err = classify_provider_error(
                        "OpenRouter HTTP 429 rate limited",
                        status_code=429,
                        provider=self.name,
                        capability="generation"
                    )
                    logger.warning("OpenRouter rate-limited (429); failing fast")
                    latency_ms = (time.time() - start_time) * 1000.0
                    return AIProviderResult(
                        text="",
                        provider=self.name,
                        latency_ms=latency_ms,
                        error=str(err),
                        error_category=err.category,
                        status_code=err.status_code,
                        error_details=err
                    )

                if resp.status_code == 503:
                    err = classify_provider_error(
                        f"OpenRouter HTTP {resp.status_code}: {resp.text[:200]}",
                        status_code=resp.status_code,
                        provider=self.name,
                        capability="generation"
                    )
                    logger.warning("OpenRouter server error (attempt %d/3)", attempt)
                    if attempt < 3:
                        await asyncio.sleep(2 ** (attempt - 1))
                        continue
                    latency_ms = (time.time() - start_time) * 1000.0
                    return AIProviderResult(
                        text="",
                        provider=self.name,
                        latency_ms=latency_ms,
                        error=str(err),
                        error_category=err.category,
                        status_code=err.status_code,
                        error_details=err
                    )

                if resp.status_code >= 400:
                    err = classify_provider_error(
                        f"OpenRouter HTTP {resp.status_code}: {resp.text[:200]}",
                        status_code=resp.status_code,
                        provider=self.name,
                        capability="generation"
                    )
                    latency_ms = (time.time() - start_time) * 1000.0
                    return AIProviderResult(
                        text="",
                        provider=self.name,
                        latency_ms=latency_ms,
                        error=str(err),
                        error_category=err.category,
                        status_code=err.status_code,
                        error_details=err
                    )

                try:
                    data = resp.json()
                except Exception as e:
                    latency_ms = (time.time() - start_time) * 1000.0
                    err = ProviderError(
                        provider=self.name,
                        capability="generation",
                        category=ErrorCategory.INVALID_RESPONSE,
                        message=f"Invalid JSON: {e}",
                    )
                    return AIProviderResult(
                        text="",
                        provider=self.name,
                        latency_ms=latency_ms,
                        error=str(err),
                        error_category=err.category,
                        error_details=err
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
                    error_category=ErrorCategory.INVALID_RESPONSE,
                    error_details=ProviderError(
                        provider=self.name,
                        capability="generation",
                        category=ErrorCategory.INVALID_RESPONSE,
                        message="Empty response from OpenRouter",
                    ),
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
