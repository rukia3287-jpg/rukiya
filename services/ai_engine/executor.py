"""services/ai_engine/executor.py
Execution engine enforcing concurrency semaphores, granular timeouts, and provider tracking.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from services.ai_engine.errors import ErrorCategory, ProviderError
from services.ai_engine.models import AIProviderResult
from services.ai_engine.providers.base import AIProvider
from services.config import Config

logger = logging.getLogger(__name__)


class Executor:
    """Executes provider generation and search calls protected by semaphores and timeouts."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        max_ai = int(getattr(self.config, "max_ai_concurrency", 3))
        max_search = int(getattr(self.config, "max_search_concurrency", 2))

        self.ai_semaphore = asyncio.Semaphore(max_ai)
        self.search_semaphore = asyncio.Semaphore(max_search)

        self.openrouter_timeout = float(getattr(self.config, "openrouter_timeout", 20.0))
        self.gemini_timeout = float(getattr(self.config, "gemini_timeout", 20.0))
        self.search_timeout = float(getattr(self.config, "search_timeout", 15.0))
        self.temperature = min(2.0, max(0.0, float(getattr(self.config, "ai_temperature", 0.85))))
        self.reasoning_enabled = bool(getattr(self.config, "ai_reasoning_enabled", True))
        self.reasoning_effort = str(getattr(self.config, "ai_reasoning_effort", "medium"))
        self.max_completion_tokens = int(getattr(self.config, "ai_max_completion_tokens", 320))

    async def execute_generate(
        self,
        provider: AIProvider,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        **kwargs: Any
    ) -> AIProviderResult:
        timeout = self.openrouter_timeout if provider.name == "openrouter" else self.gemini_timeout
        resolved_temperature = self.temperature if temperature is None else min(2.0, max(0.0, float(temperature)))
        resolved_max_tokens = self.max_completion_tokens if max_tokens is None else max(16, int(max_tokens))
        async with self.ai_semaphore:
            try:
                return await asyncio.wait_for(
                    provider.generate(messages=messages, max_tokens=resolved_max_tokens, temperature=resolved_temperature, reasoning_enabled=self.reasoning_enabled, reasoning_effort=self.reasoning_effort, **kwargs),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                logger.warning("Provider %s generate timed out after %.1fs", provider.name, timeout)
                return AIProviderResult(
                    text="",
                    provider=provider.name,
                    error=f"Timeout ({timeout}s)",
                    error_details=ProviderError(
                        provider=provider.name,
                        capability="generation",
                        category=ErrorCategory.TIMEOUT,
                        status_code=408,
                        retryable=True,
                    )
                )

    async def execute_search(
        self,
        provider: AIProvider,
        query: str,
        system_instruction: Optional[str] = None,
        context: Optional[str] = None,
        max_tokens: int = 250,
        **kwargs: Any
    ) -> AIProviderResult:
        async with self.search_semaphore:
            try:
                return await asyncio.wait_for(
                    provider.search_grounded(
                        query=query,
                        system_instruction=system_instruction,
                        context=context,
                        max_tokens=max_tokens,
                        **kwargs
                    ),
                    timeout=self.search_timeout
                )
            except asyncio.TimeoutError:
                logger.warning("Provider %s search timed out after %.1fs", provider.name, self.search_timeout)
                return AIProviderResult(
                    text="",
                    provider=provider.name,
                    used_search=True,
                    error=f"Search timeout ({self.search_timeout}s)",
                    error_details=ProviderError(
                        provider=provider.name,
                        capability="search",
                        category=ErrorCategory.TIMEOUT,
                        status_code=408,
                        retryable=True,
                    )
                )
