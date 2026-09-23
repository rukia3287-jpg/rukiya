"""services/ai_engine/models.py
Typed data models, enumerations, and telemetry containers for the Rukiya AI Engine.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class RouteType(str, Enum):
    OPENROUTER_DIRECT = "openrouter_direct"
    GEMINI_DIRECT = "gemini_direct"
    GEMINI_SEARCH = "gemini_search"
    HYBRID = "hybrid"
    CONSENSUS = "consensus"
    BACKUP = "backup"
    STATIC_FALLBACK = "static_fallback"


class CostClass(str, Enum):
    FREE = "free"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CriticVerdict(str, Enum):
    PASS = "pass"
    REPAIR = "repair"
    REJECT = "reject"


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class SearchQuery:
    """Planned search query with category for dynamic TTL and scoping."""
    query: str
    category: str = "general"  # "news", "game", "product", "stable", "general"
    time_scope: str = ""


@dataclass
class SearchResult:
    """Normalized search outcome returned from Google Search grounding."""
    query: str
    text: str
    sources: List[Dict[str, Any]] = field(default_factory=list)
    citations: List[Dict[str, Any]] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class EvidenceItem:
    """Atomic ranked evidence unit extracted from search grounding."""
    url: str
    title: str
    snippet: str
    score: float = 0.0
    authority: float = 0.5
    relevance: float = 0.5
    freshness: float = 0.5
    directness: float = 0.5
    agreement: float = 0.5


@dataclass
class AIProviderResult:
    """Normalized output from any underlying LLM provider adapter."""
    text: str
    provider: str
    latency_ms: float = 0.0
    used_search: bool = False
    citations: List[Dict[str, Any]] = field(default_factory=list)
    confidence: Optional[float] = None
    grounding_metadata: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    raw_response: Optional[Any] = None


@dataclass
class Plan:
    """Structured plan emitted by the Planner before routing/execution."""
    intent: str
    complexity: float = 0.0  # 0.0 to 1.0
    freshness_required: bool = False
    search_required: bool = False
    memory_required: bool = False
    preferred_provider: str = "openrouter"
    verification_required: bool = False
    search_queries: List[SearchQuery] = field(default_factory=list)
    cost_class: CostClass = CostClass.LOW


@dataclass
class CriticReport:
    """Report produced by the Self-Critic evaluating candidate response."""
    verdict: CriticVerdict
    score: float = 1.0
    reasons: List[str] = field(default_factory=list)
    repair_instruction: Optional[str] = None


@dataclass
class AIEngineRequest:
    """Standardized input payload for the AI Engine."""
    text: str
    author: str
    platform: str = "discord"
    user_id: Optional[str] = None
    canonical_id: Optional[str] = None
    intent: Optional[str] = None
    bypass_trigger: bool = False
    bypass_cooldown: bool = False
    persistent_memory: Optional[List[Any]] = None
    stream_memory: Optional[List[Any]] = None
    recent_messages: Optional[List[Dict[str, Any]]] = None
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])


@dataclass
class AIEngineResult:
    """Comprehensive, safe result returned by the AI Engine."""
    text: str
    provider: str
    route: str
    used_search: bool = False
    citations: List[Dict[str, Any]] = field(default_factory=list)
    evidence_quality: Optional[float] = None
    confidence: Optional[float] = None
    latency_ms: float = 0.0
    fallback_used: bool = False
    cache_hit: bool = False
    repair_count: int = 0
    cost_class: str = "low"
    request_id: str = ""
    error: Optional[str] = None
