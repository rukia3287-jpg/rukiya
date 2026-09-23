"""services/ai_engine/provider_registry.py
Registry managing AI provider instances.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from services.ai_engine.providers.base import AIProvider
from services.ai_engine.providers.gemini import GeminiProvider
from services.ai_engine.providers.openrouter import OpenRouterProvider
from services.config import Config

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """Central registry for discovering and managing AI provider adapters."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self._providers: Dict[str, AIProvider] = {}
        self._initialize_default_providers()

    def _initialize_default_providers(self) -> None:
        self.register(OpenRouterProvider(self.config))
        self.register(GeminiProvider(self.config))

    def register(self, provider: AIProvider) -> None:
        self._providers[provider.name] = provider
        logger.debug("Registered AI provider: %s", provider.name)

    def get(self, name: str) -> Optional[AIProvider]:
        return self._providers.get(name)

    def list_available(self) -> List[str]:
        return [
            name for name, prov in self._providers.items()
            if prov.is_configured()
        ]

    def has_search_capability(self) -> bool:
        gemini = self.get("gemini")
        return bool(gemini and gemini.is_configured())
