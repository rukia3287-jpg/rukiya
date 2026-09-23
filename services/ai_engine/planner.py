"""services/ai_engine/planner.py
Intelligent planning layer evaluating intent, complexity, freshness, and generating
optimized, date-aware search queries.
"""
from __future__ import annotations

import datetime
import logging
import re
from typing import List, Optional

from services.ai_engine.models import AIEngineRequest, CostClass, Plan, SearchQuery
from services.config import Config

logger = logging.getLogger(__name__)

FRESHNESS_TRIGGERS = {
    "latest", "newest", "current", "update", "updates", "patch", "news",
    "today", "yesterday", "tonight", "this week", "this month", "release date",
    "score", "codes", "banner", "what happened", "recent", "price", "schedule"
}

SEARCH_KEYWORDS = {
    "search", "google", "look up", "find out", "who is", "what is", "when did",
    "where is", "is it true", "how much", "tell me about"
}

GAME_KEYWORDS = {
    "genshin", "honkai", "bleach", "elden ring", "valorant", "fortnite",
    "game", "patch", "banner", "character", "nerf", "buff", "codes"
}


class Planner:
    """Produces structured plans for routing and query execution without answering the user."""

    def __init__(self, config: Optional[Config] = None, max_search_queries: int = 3):
        self.config = config or Config()
        self.max_search_queries = max_search_queries

    @staticmethod
    def _get_current_date_str() -> str:
        """Dynamically fetch current month and year."""
        now = datetime.datetime.now(datetime.timezone.utc)
        return now.strftime("%B %Y")

    def plan(self, request: AIEngineRequest) -> Plan:
        text_lower = request.text.lower()

        # 1. Determine Search & Freshness Requirement
        is_casual_conversation = (
            request.intent in ("greeting", "compliment", "offtopic")
            or any(g in text_lower for g in [
                "how are you", "how's it going", "how r u", "doing today",
                "hello", "hey", "hi rukiya", "welcome", "good morning", "good evening"
            ])
        ) and not any(kw in text_lower for kw in [
            "patch", "update", "news", "score", "release date", "codes", "search", "who won", "banner"
        ])

        freshness_required = any(word in text_lower for word in FRESHNESS_TRIGGERS) and not is_casual_conversation
        explicit_search = (
            any(kw in text_lower for kw in SEARCH_KEYWORDS)
            and ("?" in text_lower or len(text_lower.split()) > 3)
            and not is_casual_conversation
        )
        search_required = freshness_required or explicit_search

        # 2. Determine Memory Requirement
        personal_pronouns = any(p in text_lower.split() for p in ["my", "me", "i", "mine"])
        has_memory = bool(request.persistent_memory or request.stream_memory)
        memory_required = personal_pronouns or has_memory

        # 3. Calculate Complexity (0.0 to 1.0)
        word_count = len(text_lower.split())
        complexity = min(0.4, (word_count / 30.0) * 0.4)
        if any(cmp in text_lower for cmp in ["vs", "versus", "compare", "difference between", "why"]):
            complexity += 0.3
        if search_required and freshness_required:
            complexity += 0.2
        complexity = round(min(1.0, complexity), 2)

        # 4. Determine Intent
        intent = request.intent or "chatter"
        if freshness_required or explicit_search:
            intent = "current_information" if freshness_required else "question"

        # 5. Generate Search Queries if search required
        search_queries: List[SearchQuery] = []
        if search_required:
            search_queries = self._generate_search_queries(request)

        # 6. Preferred Provider
        if search_required:
            preferred_provider = "gemini"
        else:
            preferred_provider = "openrouter"

        # Cost Class
        if search_required and complexity > 0.6:
            cost_class = CostClass.HIGH
        elif search_required:
            cost_class = CostClass.MEDIUM
        else:
            cost_class = CostClass.LOW

        return Plan(
            intent=intent,
            complexity=complexity,
            freshness_required=freshness_required,
            search_required=search_required,
            memory_required=memory_required,
            preferred_provider=preferred_provider,
            verification_required=search_required or complexity > 0.6,
            search_queries=search_queries,
            cost_class=cost_class
        )

    def _generate_search_queries(self, request: AIEngineRequest) -> List[SearchQuery]:
        """Synthesize clean, date-augmented search queries from message and user memory."""
        text = request.text
        # Strip greeting prefixes and bot name calls
        cleaned = re.sub(
            r"^(hey\s+|hi\s+|hello\s+|rukiya\s+|rukia\s+|@rukiya\s+|please\s+|can\s+you\s+tell\s+me\s+)",
            "",
            text,
            flags=re.IGNORECASE
        ).strip(" ?.!/")

        # Memory contextualization: if user asks "in the game I play", inject known favorite game
        if "the game i play" in cleaned.lower() or "my game" in cleaned.lower():
            favorite_game = None
            if request.persistent_memory:
                for m in request.persistent_memory:
                    k = getattr(m, "key", "") if not isinstance(m, dict) else m.get("key", "")
                    v = getattr(m, "value", "") if not isinstance(m, dict) else m.get("value", "")
                    if "game" in k.lower():
                        favorite_game = v
                        break
            if favorite_game:
                cleaned = re.sub(r"\b(the game i play|my game)\b", favorite_game, cleaned, flags=re.IGNORECASE)

        # Detect category
        category = "general"
        if any(gk in cleaned.lower() for gk in GAME_KEYWORDS):
            category = "game"
        elif any(nk in cleaned.lower() for nk in ["news", "breaking", "happened"]):
            category = "news"
        elif any(pk in cleaned.lower() for pk in ["price", "buy", "specs", "phone", "rtx"]):
            category = "product"

        date_str = self._get_current_date_str()
        time_scope = date_str

        # Primary query
        primary_q = f"{cleaned} {date_str}".strip() if any(w in cleaned.lower() for w in FRESHNESS_TRIGGERS) else cleaned
        queries = [SearchQuery(query=primary_q, category=category, time_scope=time_scope)]

        # Secondary query for games or releases if complex
        if category == "game" and len(queries) < self.max_search_queries:
            alt_q = f"{cleaned} update patch notes {date_str}"
            queries.append(SearchQuery(query=alt_q, category=category, time_scope=time_scope))

        return queries[:self.max_search_queries]
