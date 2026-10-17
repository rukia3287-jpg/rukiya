"""Optional local generation provider for Ollama/OpenAI-compatible endpoints.

This adapter is disabled unless LOCAL_LLM_BASE_URL is configured. It performs
generation only; it does not claim to provide web search or current evidence.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from services.ai_engine.errors import ErrorCategory, ProviderError
from services.ai_engine.models import AIProviderResult
from services.ai_engine.providers.base import AIProvider
from services.config import Config

logger = logging.getLogger(__name__)


class LocalLLMProvider(AIProvider):
    """OpenAI-compatible local/private endpoint, commonly Ollama's /v1 API."""

    name = "local_llm"

    def __init__(
        self,
        config: Optional[Config] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        self.config = config or Config()
        self.http_client = http_client
        self.base_url = str(getattr(self.config, "local_llm_base_url", "") or "").strip().rstrip("/")
        self.model = str(getattr(self.config, "local_llm_model", "qwen2.5:3b") or "qwen2.5:3b").strip()
        self.api_key = str(getattr(self.config, "local_llm_api_key", "") or "").strip()
        self.timeout = max(1.0, float(getattr(self.config, "local_llm_timeout", 8.0)))

    def is_configured(self) -> bool:
        return bool(self.base_url and self.model)

    async def generate(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 150,
        temperature: float = 0.85,
        **kwargs: Any,
    ) -> AIProviderResult:
        if not self.is_configured():
            return AIProviderResult(text="", provider=self.name, error="Local LLM endpoint is not configured")

        endpoint = self.base_url
        if not endpoint.endswith("/chat/completions"):
            endpoint = endpoint + ("" if endpoint.endswith("/v1") else "/v1") + "/chat/completions"

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max(16, int(max_tokens)),
            "temperature": min(2.0, max(0.0, float(temperature))),
            "stream": False,
        }
        started = time.time()
        client = self.http_client
        should_close = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout)
        try:
            response = await client.post(endpoint, json=payload, headers=headers, timeout=self.timeout)
            latency = (time.time() - started) * 1000.0
            if response.status_code >= 400:
                err = ProviderError(
                    provider=self.name,
                    capability="generation",
                    category=ErrorCategory.SERVER_ERROR if response.status_code >= 500 else ErrorCategory.BAD_REQUEST,
                    status_code=response.status_code,
                    retryable=response.status_code >= 500,
                    message=response.text[:240],
                )
                return AIProviderResult(
                    text="", provider=self.name, latency_ms=latency,
                    error=str(err), error_category=err.category, status_code=response.status_code,
                    error_details=err,
                )
            try:
                data = response.json()
                choices = data.get("choices") or []
                content = (choices[0].get("message") or {}).get("content") if choices else ""
                if isinstance(content, list):
                    content = "".join(
                        item.get("text", "") for item in content if isinstance(item, dict)
                    )
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("No text in choices[0].message.content")
            except (ValueError, TypeError, AttributeError, IndexError) as exc:
                err = ProviderError(
                    provider=self.name, capability="generation",
                    category=ErrorCategory.INVALID_RESPONSE, message=str(exc),
                )
                return AIProviderResult(
                    text="", provider=self.name, latency_ms=latency,
                    error=str(err), error_category=err.category, error_details=err,
                )
            return AIProviderResult(
                text=content.strip(), provider=self.name, latency_ms=latency,
                confidence=0.65, raw_response=data,
            )
        except (httpx.HTTPError, TimeoutError) as exc:
            latency = (time.time() - started) * 1000.0
            err = ProviderError(
                provider=self.name, capability="generation",
                category=ErrorCategory.TIMEOUT if isinstance(exc, (TimeoutError, httpx.TimeoutException)) else ErrorCategory.NETWORK_ERROR,
                retryable=True, message=str(exc)[:240],
            )
            logger.info("Local LLM fallback unavailable: %s", err)
            return AIProviderResult(
                text="", provider=self.name, latency_ms=latency,
                error=str(err), error_category=err.category, error_details=err,
            )
        finally:
            if should_close:
                await client.aclose()

    async def search_grounded(
        self,
        query: str,
        system_instruction: Optional[str] = None,
        context: Optional[str] = None,
        max_tokens: int = 250,
        **kwargs: Any,
    ) -> AIProviderResult:
        return AIProviderResult(
            text="", provider=self.name,
            error="Local LLM provider does not support grounded web search",
            error_category=ErrorCategory.UNKNOWN,
            error_details=ProviderError(
                provider=self.name, capability="search", category=ErrorCategory.UNKNOWN,
                retryable=False, message="Search capability is not supported",
            ),
        )

    async def health_check(self) -> bool:
        if not self.is_configured():
            return False
        endpoint = self.base_url
        if not endpoint.endswith("/models"):
            endpoint = endpoint + ("" if endpoint.endswith("/v1") else "/v1") + "/models"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        client = self.http_client
        should_close = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=min(self.timeout, 3.0))
        try:
            response = await client.get(endpoint, headers=headers, timeout=min(self.timeout, 3.0))
            return response.status_code < 400
        except httpx.HTTPError:
            return False
        finally:
            if should_close:
                await client.aclose()
