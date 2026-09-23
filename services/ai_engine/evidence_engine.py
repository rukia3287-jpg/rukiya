"""services/ai_engine/evidence_engine.py
Deduplication, multi-factor scoring, conflict detection, and compression of web search evidence.
"""
from __future__ import annotations

import datetime
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from services.ai_engine.models import EvidenceItem

logger = logging.getLogger(__name__)

AUTHORITY_DOMAINS = {
    ".gov": 1.0,
    ".edu": 0.95,
    ".org": 0.85,
    "wikipedia.org": 0.85,
    "hoyoverse.com": 0.95,
    "genshin": 0.90,
    "ign.com": 0.80,
    "fandom.com": 0.75,
    "gamespot.com": 0.75,
    "reuters.com": 0.90,
    "bbc.com": 0.90,
}


class EvidenceEngine:
    """Evaluates, ranks, detects contradictions, and compresses search grounding evidence."""

    @staticmethod
    def deduplicate_sources(raw_sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen_urls = set()
        deduped: List[Dict[str, Any]] = []
        for src in raw_sources:
            url = src.get("url", "").strip()
            # Normalize trailing slashes
            norm_url = url.rstrip("/")
            if norm_url and norm_url in seen_urls:
                continue
            if norm_url:
                seen_urls.add(norm_url)
            deduped.append(src)
        return deduped

    def evaluate_sources(self, raw_sources: List[Dict[str, Any]], query: str) -> List[EvidenceItem]:
        deduped = self.deduplicate_sources(raw_sources)
        items: List[EvidenceItem] = []
        query_words = set(query.lower().split())
        current_year = str(datetime.datetime.now(datetime.timezone.utc).year)

        for src in deduped:
            url = src.get("url", "")
            title = src.get("title", "")
            snippet = src.get("snippet", src.get("text", ""))

            # 1. Authority Score
            authority = 0.50
            domain = urlparse(url).netloc.lower()
            for auth_key, score in AUTHORITY_DOMAINS.items():
                if auth_key in domain or auth_key in url.lower():
                    authority = max(authority, score)

            # 2. Relevance Score
            combined_text = f"{title} {snippet}".lower()
            matched = sum(1 for w in query_words if len(w) > 2 and w in combined_text)
            relevance = min(1.0, matched / max(1, len(query_words)))

            # 3. Freshness Score
            freshness = 0.50
            if current_year in combined_text:
                freshness = 0.90
            elif any(w in combined_text for w in ["today", "yesterday", "latest", "update", "patch"]):
                freshness = 0.80

            # 4. Directness
            directness = 0.60
            if any(w in title.lower() for w in query_words if len(w) > 3):
                directness = 0.85

            # 5. Agreement (nominal default)
            agreement = 0.70

            # Composite Score
            source_score = (
                authority * 0.30
                + relevance * 0.25
                + freshness * 0.20
                + directness * 0.15
                + agreement * 0.10
            )

            items.append(
                EvidenceItem(
                    url=url,
                    title=title,
                    snippet=snippet,
                    score=round(source_score, 3),
                    authority=authority,
                    relevance=relevance,
                    freshness=freshness,
                    directness=directness,
                    agreement=agreement
                )
            )

        # Sort descending by score
        items.sort(key=lambda x: x.score, reverse=True)
        return items

    @staticmethod
    def detect_conflicts(items: List[EvidenceItem]) -> Tuple[bool, List[str]]:
        """Detect numeric or release date conflicts between evidence items."""
        if len(items) < 2:
            return False, []

        version_regex = re.compile(r"\b(\d+\.\d+(\.\d+)?)\b")
        versions_found: Dict[str, str] = {}
        for item in items:
            combined = f"{item.title} {item.snippet}"
            for match in version_regex.finditer(combined):
                ver = match.group(1)
                versions_found[ver] = item.url

        if len(versions_found) > 2:
            conflicts = [f"Conflicting version {v} from {u}" for v, u in versions_found.items()]
            return True, conflicts

        return False, []

    def calculate_evidence_quality(self, items: List[EvidenceItem], has_conflict: bool) -> float:
        """
        Calculate overall evidence quality score (0.0 to 1.0):
        0.90+ : strong evidence
        0.70-0.89: good evidence
        0.50-0.69: uncertain
        <0.50: weak evidence
        """
        if not items:
            return 0.0

        top_scores = [item.score for item in items[:3]]
        avg_score = sum(top_scores) / len(top_scores)

        if has_conflict:
            avg_score = max(0.2, avg_score - 0.25)

        return round(min(1.0, max(0.0, avg_score)), 2)

    @staticmethod
    def compress_evidence(items: List[EvidenceItem], max_items: int = 5) -> str:
        """Compress top evidence into compact, safe context string."""
        if not items:
            return ""

        chunks: List[str] = []
        for i, item in enumerate(items[:max_items], 1):
            # Clean snippet to avoid control instructions
            clean_snippet = item.snippet.replace("\r", " ").replace("\n", " ").strip()
            chunks.append(f"[{i}] {item.title} ({item.url})\n    {clean_snippet[:200]}")

        return "\n\n".join(chunks)
