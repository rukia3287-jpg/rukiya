"""services/ai_engine/providers/base.py
Abstract base provider interface for the Rukiya AI Engine.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from services.ai_engine.models import AIProviderResult


class AIProvider(ABC):
    """Abstract interface for all underlying LLM and Search providers."""

    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """Check if required API credentials and dependencies are available."""
        pass

    @abstractmethod
    async def generate(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 150,
        temperature: float = 0.85,
        **kwargs: Any
    ) -> AIProviderResult:
        """Execute text generation from message context."""
        pass

    @abstractmethod
    async def search_grounded(
        self,
        query: str,
        system_instruction: Optional[str] = None,
        context: Optional[str] = None,
        max_tokens: int = 250,
        **kwargs: Any
    ) -> AIProviderResult:
        """Execute search-grounded generation/retrieval."""
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Perform a quick availability check."""
        pass
