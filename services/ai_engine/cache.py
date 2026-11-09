"""services/ai_engine/cache.py
Search result caching with category-specific TTL, negative caching for rate limits/outages,
and resilient in-flight request deduplication.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Dict, Optional, Tuple

from services.ai_engine.errors import ErrorCategory
from services.ai_engine.models import SearchResult

logger = logging.getLogger(__name__)

# Category TTL in seconds
CATEGORY_TTLS: Dict[str, int] = {
    "breaking": 60,
    "news": 60,
    "game": 300,
    "product": 3600,
    "stable": 21600,
    "general": 300,
}


def normalize_query(query: str) -> str:
    """Normalize query for cache key consistency."""
    q = query.lower().strip()
    q = re.sub(r"[^\w\s]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


@dataclass
class NegativeCacheEntry:
    """Record of a recent search error to prevent hammering failing providers."""
    query: str
    reason: str
    error_category: ErrorCategory
    expiry: float
    time_scope: str = ""

    @property
    def category(self) -> ErrorCategory:
        return self.error_category

    @property
    def error_message(self) -> str:
        return self.reason

    @property
    def retry_after(self) -> float:
        return self.expiry


class SearchResultCache:
    """Bounded LRU cache for normalized search results and negative error caching."""

    def __init__(self, default_ttl: int = 300, max_entries: int = 500):
        self.default_ttl = default_ttl
        self.max_entries = max_entries
        # key -> (SearchResult, expiry_timestamp)
        self._entries: collections.OrderedDict[str, Tuple[SearchResult, float]] = collections.OrderedDict()
        # key -> NegativeCacheEntry
        self._negative_entries: collections.OrderedDict[str, NegativeCacheEntry] = collections.OrderedDict()

    def _make_key(self, query: str, time_scope: str = "") -> str:
        norm = normalize_query(query)
        return f"{norm}::{time_scope.strip()}" if time_scope else norm

    def get(self, query: str, time_scope: str = "") -> Optional[SearchResult]:
        key = self._make_key(query, time_scope)
        if key not in self._entries:
            return None

        result, expiry = self._entries[key]
        if time.time() > expiry:
            del self._entries[key]
            return None

        # Move to end for LRU
        self._entries.move_to_end(key)
        return result

    def get_negative(self, query: str, time_scope: str = "") -> Optional[NegativeCacheEntry]:
        key = self._make_key(query, time_scope)
        if key not in self._negative_entries:
            return None

        entry = self._negative_entries[key]
        if time.time() > entry.expiry:
            del self._negative_entries[key]
            return None

        self._negative_entries.move_to_end(key)
        return entry

    def set(
        self,
        query: str,
        result: SearchResult,
        category: str = "general",
        time_scope: str = "",
        custom_ttl: Optional[int] = None
    ) -> None:
        key = self._make_key(query, time_scope)
        ttl = custom_ttl or CATEGORY_TTLS.get(category.lower(), self.default_ttl)
        expiry = time.time() + ttl

        # Clean any negative cache entry on success
        self._negative_entries.pop(key, None)

        if key in self._entries:
            del self._entries[key]
        elif len(self._entries) >= self.max_entries:
            self._entries.popitem(last=False)

        self._entries[key] = (result, expiry)

    def set_negative(
        self,
        query: str,
        reason: str = "",
        error_category: Optional[ErrorCategory] = None,
        time_scope: str = "",
        ttl: Optional[float] = None,
        category: Optional[ErrorCategory] = None,
        error_message: Optional[str] = None
    ) -> None:
        cat = category or error_category or ErrorCategory.RATE_LIMITED
        msg = error_message or reason or "Unknown search error"
        key = self._make_key(query, time_scope)
        # Default negative cache TTL: 45s for rate limits, 30s for general errors
        cache_ttl = ttl if ttl is not None else (45.0 if cat == ErrorCategory.RATE_LIMITED else 30.0)
        expiry = time.time() + cache_ttl

        if key in self._negative_entries:
            del self._negative_entries[key]
        elif len(self._negative_entries) >= self.max_entries:
            self._negative_entries.popitem(last=False)

        self._negative_entries[key] = NegativeCacheEntry(
            query=query,
            reason=msg,
            error_category=cat,
            expiry=expiry,
            time_scope=time_scope
        )

    def clear(self) -> None:
        self._entries.clear()
        self._negative_entries.clear()

    def clear_partition(self, partition: str) -> None:
        for entries in (self._entries, self._negative_entries):
            for key in list(entries):
                if partition in key:
                    entries.pop(key, None)

    def size(self) -> int:
        return len(self._entries)

    def negative_size(self) -> int:
        return len(self._negative_entries)


class InFlightDeduplicator:
    """Deduplicates concurrent in-flight asynchronous search tasks for identical queries with safe cancellation."""

    def __init__(self):
        self._in_flight: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def execute_or_join(
        self,
        key: str,
        factory: Callable[[], Coroutine[Any, Any, Any]]
    ) -> Any:
        norm_key = normalize_query(key)
        async with self._lock:
            existing_task = self._in_flight.get(norm_key)
            if existing_task and not existing_task.done():
                logger.debug("Joining an existing in-flight search")
                task_to_await = existing_task
            else:
                if len(self._in_flight) >= 128:
                    raise RuntimeError('Search capacity exhausted')
                task_to_await = asyncio.create_task(factory())
                self._in_flight[norm_key] = task_to_await
                def completed(task):
                    if self._in_flight.get(norm_key) is task:
                        self._in_flight.pop(norm_key, None)
                    if not task.cancelled():
                        task.exception()  # consume failures even if every waiter left
                task_to_await.add_done_callback(completed)

        try:
            return await asyncio.shield(task_to_await)
        except asyncio.CancelledError:
            logger.debug("In-flight search waiter cancelled")
            raise
        finally:
            async with self._lock:
                if self._in_flight.get(norm_key) is task_to_await and task_to_await.done():
                    self._in_flight.pop(norm_key, None)

    async def cancel_all(self) -> None:
        """Cancel all in-flight tasks for graceful shutdown."""
        async with self._lock:
            tasks = [t for t in self._in_flight.values() if not t.done()]
            for t in tasks:
                t.cancel()
            self._in_flight.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def cancel_partition(self, partition):
        tasks = [task for key, task in self._in_flight.items() if key.startswith(partition)]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def active_count(self) -> int:
        return len([t for t in self._in_flight.values() if not t.done()])
