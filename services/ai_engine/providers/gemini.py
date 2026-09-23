"""services/ai_engine/providers/gemini.py
Gemini provider adapter supporting standard generation, Google Search grounding,
and modular engines for search, grounding evaluation, and response normalization.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from services.ai_engine.models import AIProviderResult
from services.ai_engine.providers.base import AIProvider
from services.config import Config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 1. Client Manager
# ─────────────────────────────────────────────────────────────
class ClientManager:
    """Manages SDK import, client initialization, credentials, and health."""

    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-3.5-flash-lite"):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        raw_model = model or os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
        # Google GenAI accepts both "gemini-3.5-flash-lite" and "models/gemini-3.5-flash-lite"
        self.model = raw_model.removeprefix("models/") if raw_model.startswith("models/") else raw_model
        self._client: Optional[Any] = None
        self._sdk_available: Optional[bool] = None

    def is_sdk_available(self) -> bool:
        if self._sdk_available is not None:
            return self._sdk_available
        try:
            from google import genai  # type: ignore # noqa: F401
            self._sdk_available = True
        except ImportError:
            self._sdk_available = False
        return self._sdk_available

    def validate_credentials(self) -> bool:
        return bool(self.api_key and self.api_key.strip())

    def get_client(self) -> Optional[Any]:
        if self._client is not None:
            return self._client
        if not self.validate_credentials() or not self.is_sdk_available():
            return None
        try:
            from google import genai  # type: ignore
            self._client = genai.Client(api_key=self.api_key)
            return self._client
        except Exception as e:
            logger.warning("Failed to initialize Google GenAI Client: %s", e)
            return None

    def set_client(self, client: Any) -> None:
        """Allow injecting mock client for testing."""
        self._client = client

    def health_check(self) -> bool:
        return self.validate_credentials() and (self.is_sdk_available() or self._client is not None)


# ─────────────────────────────────────────────────────────────
# 2. Response Parser
# ─────────────────────────────────────────────────────────────
class ResponseParser:
    """Parses text, citations, and grounding metadata from Gemini response structures."""

    @staticmethod
    def parse_text(response: Any) -> str:
        if not response:
            return ""
        if isinstance(response, str):
            return response.strip()
        # genai SDK response
        text = getattr(response, "text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()
        # Check candidates
        candidates = getattr(response, "candidates", None) or []
        for cand in candidates:
            content = getattr(cand, "content", None)
            if content:
                parts = getattr(content, "parts", None) or []
                text_parts = [getattr(p, "text", "") for p in parts if getattr(p, "text", "")]
                if text_parts:
                    return "".join(text_parts).strip()
        # Check dict format
        if isinstance(response, dict):
            if "text" in response:
                return str(response["text"]).strip()
            choices = response.get("choices") or []
            if choices and isinstance(choices[0], dict):
                return choices[0].get("message", {}).get("content", "").strip()
        return ""

    @staticmethod
    def parse_citations(response: Any) -> List[Dict[str, Any]]:
        citations: List[Dict[str, Any]] = []
        if not response:
            return citations

        candidates = getattr(response, "candidates", None) or []
        for cand in candidates:
            gm = getattr(cand, "grounding_metadata", None)
            if not gm:
                continue

            # Grounding chunks contain web sources
            chunks = getattr(gm, "grounding_chunks", None) or []
            for chunk in chunks:
                web = getattr(chunk, "web", None)
                if web:
                    uri = getattr(web, "uri", "") or ""
                    title = getattr(web, "title", "") or ""
                    citations.append({
                        "url": uri,
                        "title": title,
                        "type": "web"
                    })

            # Check dict format for grounding_metadata
            if isinstance(gm, dict):
                for chunk in gm.get("grounding_chunks", []):
                    web = chunk.get("web", {})
                    if web:
                        citations.append({
                            "url": web.get("uri", ""),
                            "title": web.get("title", ""),
                            "type": "web"
                        })

        if isinstance(response, dict):
            metadata = response.get("grounding_metadata") or {}
            for chunk in metadata.get("grounding_chunks", []):
                web = chunk.get("web", {})
                if web:
                    citations.append({
                        "url": web.get("uri", ""),
                        "title": web.get("title", ""),
                        "type": "web"
                    })

        return citations

    @staticmethod
    def parse_metadata(response: Any) -> Dict[str, Any]:
        meta: Dict[str, Any] = {}
        if not response:
            return meta

        candidates = getattr(response, "candidates", None) or []
        for cand in candidates:
            gm = getattr(cand, "grounding_metadata", None)
            if gm:
                queries = getattr(gm, "web_search_queries", None) or []
                if queries:
                    meta["web_search_queries"] = queries
                supports = getattr(gm, "grounding_supports", None) or []
                if supports:
                    meta["supports_count"] = len(supports)
            if isinstance(gm, dict):
                meta["web_search_queries"] = gm.get("web_search_queries", [])
                meta["supports_count"] = len(gm.get("grounding_supports", []))

        if isinstance(response, dict):
            if "grounding_metadata" in response:
                meta["grounding_metadata"] = response["grounding_metadata"]

        return meta

    def normalize_response(
        self,
        response: Any,
        latency_ms: float,
        used_search: bool = False,
        error: Optional[str] = None
    ) -> AIProviderResult:
        if error:
            return AIProviderResult(
                text="",
                provider="gemini",
                latency_ms=latency_ms,
                used_search=used_search,
                error=error,
                raw_response=response
            )

        text = self.parse_text(response)
        citations = self.parse_citations(response)
        metadata = self.parse_metadata(response)

        return AIProviderResult(
            text=text,
            provider="gemini",
            latency_ms=latency_ms,
            used_search=used_search,
            citations=citations,
            confidence=0.9 if text else 0.0,
            grounding_metadata=metadata,
            raw_response=response
        )


# ─────────────────────────────────────────────────────────────
# 3. Grounding Engine
# ─────────────────────────────────────────────────────────────
class GroundingEngine:
    """Processes grounding snippets, calculates quality, and detects source conflicts."""

    @staticmethod
    def build_grounded_context(sources: List[Dict[str, Any]], search_queries: Optional[List[str]] = None) -> str:
        lines: List[str] = []
        if search_queries:
            lines.append(f"Queries: {', '.join(search_queries)}")
        for idx, src in enumerate(sources[:5], 1):
            title = src.get("title", f"Source {idx}")
            url = src.get("url", "")
            snippet = src.get("snippet", src.get("text", ""))
            lines.append(f"[{idx}] {title} ({url})\n{snippet}")
        return "\n\n".join(lines)

    @staticmethod
    def calculate_grounding_quality(citations: List[Dict[str, Any]], metadata: Optional[Dict[str, Any]] = None) -> float:
        if not citations:
            return 0.0
        score = 0.5
        # Higher score for multiple diverse citations
        count = len(citations)
        if count >= 3:
            score += 0.3
        elif count >= 1:
            score += 0.15

        # Check for authority domains (.org, .gov, .edu, official sites)
        has_authoritative = any(
            any(tld in c.get("url", "").lower() for tld in [".gov", ".edu", ".org", "wiki", "official"])
            for c in citations
        )
        if has_authoritative:
            score += 0.15

        return min(1.0, score)

    @staticmethod
    def detect_source_conflict(sources: List[Dict[str, Any]]) -> bool:
        """Detect numeric or date contradictions among sources."""
        if len(sources) < 2:
            return False

        # Extract dates or version patterns (e.g. v2.4 vs v2.5, or release dates)
        version_pattern = re.compile(r"\bv?(\d+\.\d+(\.\d+)?)\b", re.IGNORECASE)
        found_versions = set()
        for src in sources:
            text = f"{src.get('title', '')} {src.get('snippet', '')}"
            matches = version_pattern.findall(text)
            for m in matches:
                found_versions.add(m[0])

        return len(found_versions) > 2


# ─────────────────────────────────────────────────────────────
# 4. Search Engine
# ─────────────────────────────────────────────────────────────
class SearchEngine:
    """Executes Google Search grounding via Gemini API."""

    def __init__(self, client_manager: ClientManager, response_parser: ResponseParser):
        self.client_manager = client_manager
        self.response_parser = response_parser

    async def search(
        self,
        query: str,
        system_instruction: Optional[str] = None,
        context: Optional[str] = None,
        max_tokens: int = 250,
        timeout: float = 15.0
    ) -> AIProviderResult:
        start_time = time.time()
        client = self.client_manager.get_client()
        if not client:
            return self.response_parser.normalize_response(
                None,
                (time.time() - start_time) * 1000.0,
                used_search=True,
                error="Gemini client unavailable or unconfigured"
            )

        prompt = query
        if context:
            prompt = f"Context:\n{context}\n\nUser Question: {query}"

        try:
            # Official google-genai SDK call
            # GenerateContentConfig with google_search tool
            def _call_gemini_search():
                try:
                    from google.genai import types  # type: ignore
                    config = types.GenerateContentConfig(
                        tools=[{"google_search": {}}],
                        temperature=0.3,
                        max_output_tokens=max_tokens,
                        system_instruction=system_instruction
                    )
                    return client.models.generate_content(
                        model=self.client_manager.model,
                        contents=prompt,
                        config=config
                    )
                except Exception:
                    # Fallback syntax if mock or dict-based client
                    if hasattr(client, "generate_content"):
                        return client.generate_content(prompt, tools=["google_search"])
                    if hasattr(client, "models") and hasattr(client.models, "generate_content"):
                        return client.models.generate_content(
                            model=self.client_manager.model,
                            contents=prompt
                        )
                    raise

            loop = asyncio.get_running_loop()
            resp = await asyncio.wait_for(
                loop.run_in_executor(None, _call_gemini_search),
                timeout=timeout
            )
            latency_ms = (time.time() - start_time) * 1000.0
            return self.response_parser.normalize_response(resp, latency_ms, used_search=True)

        except asyncio.TimeoutError:
            latency_ms = (time.time() - start_time) * 1000.0
            return self.response_parser.normalize_response(
                None, latency_ms, used_search=True, error=f"Gemini search timeout ({timeout}s)"
            )
        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000.0
            logger.warning("Gemini search exception: %s", e)
            return self.response_parser.normalize_response(
                None, latency_ms, used_search=True, error=f"Gemini search error: {str(e)}"
            )


# ─────────────────────────────────────────────────────────────
# 5. Generation Engine
# ─────────────────────────────────────────────────────────────
class GenerationEngine:
    """Executes standard generation without search grounding."""

    def __init__(self, client_manager: ClientManager, response_parser: ResponseParser):
        self.client_manager = client_manager
        self.response_parser = response_parser

    async def generate(
        self,
        contents: Any,
        system_instruction: Optional[str] = None,
        max_tokens: int = 150,
        temperature: float = 0.85,
        timeout: float = 20.0
    ) -> AIProviderResult:
        start_time = time.time()
        client = self.client_manager.get_client()
        if not client:
            return self.response_parser.normalize_response(
                None,
                (time.time() - start_time) * 1000.0,
                used_search=False,
                error="Gemini client unavailable or unconfigured"
            )

        try:
            def _call_gemini():
                try:
                    from google.genai import types  # type: ignore
                    config = types.GenerateContentConfig(
                        temperature=temperature,
                        max_output_tokens=max_tokens,
                        system_instruction=system_instruction
                    )
                    return client.models.generate_content(
                        model=self.client_manager.model,
                        contents=contents,
                        config=config
                    )
                except Exception:
                    if hasattr(client, "generate_content"):
                        return client.generate_content(contents)
                    if hasattr(client, "models") and hasattr(client.models, "generate_content"):
                        return client.models.generate_content(
                            model=self.client_manager.model,
                            contents=contents
                        )
                    raise

            loop = asyncio.get_running_loop()
            resp = await asyncio.wait_for(
                loop.run_in_executor(None, _call_gemini),
                timeout=timeout
            )
            latency_ms = (time.time() - start_time) * 1000.0
            return self.response_parser.normalize_response(resp, latency_ms, used_search=False)

        except asyncio.TimeoutError:
            latency_ms = (time.time() - start_time) * 1000.0
            return self.response_parser.normalize_response(
                None, latency_ms, used_search=False, error=f"Gemini generation timeout ({timeout}s)"
            )
        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000.0
            logger.warning("Gemini generation exception: %s", e)
            return self.response_parser.normalize_response(
                None, latency_ms, used_search=False, error=f"Gemini generation error: {str(e)}"
            )


# ─────────────────────────────────────────────────────────────
# 6. Gemini Provider (Composite Adapter)
# ─────────────────────────────────────────────────────────────
class GeminiProvider(AIProvider):
    """
    Composite Gemini Provider coordinating ClientManager, GenerationEngine,
    SearchEngine, GroundingEngine, and ResponseParser.
    """

    name: str = "gemini"

    def __init__(
        self,
        config: Optional[Config] = None,
        client: Optional[Any] = None
    ):
        self.config = config or Config()
        api_key = getattr(self.config, "gemini_api_key", None) or os.getenv("GEMINI_API_KEY")
        model = getattr(self.config, "gemini_model", None) or os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite"
        self.timeout = float(getattr(self.config, "gemini_timeout", 20.0))
        self.search_timeout = float(getattr(self.config, "search_timeout", 15.0))

        self.client_manager = ClientManager(api_key=api_key, model=model)
        if client is not None:
            self.client_manager.set_client(client)

        self.response_parser = ResponseParser()
        self.grounding_engine = GroundingEngine()
        self.generation_engine = GenerationEngine(self.client_manager, self.response_parser)
        self.search_engine = SearchEngine(self.client_manager, self.response_parser)

    def is_configured(self) -> bool:
        return self.client_manager.health_check()

    async def generate(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 150,
        temperature: float = 0.85,
        **kwargs: Any
    ) -> AIProviderResult:
        """Format messages and execute generation."""
        # Convert messages list to prompt / contents
        system_instruction = None
        user_parts: List[str] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                system_instruction = content
            else:
                user_parts.append(f"{role.capitalize()}: {content}")

        contents = "\n\n".join(user_parts) if user_parts else (system_instruction or "")
        return await self.generation_engine.generate(
            contents=contents,
            system_instruction=system_instruction,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=self.timeout
        )

    async def search_grounded(
        self,
        query: str,
        system_instruction: Optional[str] = None,
        context: Optional[str] = None,
        max_tokens: int = 250,
        **kwargs: Any
    ) -> AIProviderResult:
        """Execute search-grounded content generation via Google Search grounding."""
        return await self.search_engine.search(
            query=query,
            system_instruction=system_instruction,
            context=context,
            max_tokens=max_tokens,
            timeout=self.search_timeout
        )

    async def health_check(self) -> bool:
        return self.client_manager.health_check()
