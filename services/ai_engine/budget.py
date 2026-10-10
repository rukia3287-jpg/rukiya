"""services/ai_engine/budget.py
Budget manager tracking global and per-user limits, with daily search safety caps.

Accounting policy (all counters reset together at the UTC day boundary):

- search_count: grounded searches dispatched to Gemini, successful or not, because
  failed calls still consume provider quota. Positive-cache hits, in-flight
  deduplication joins and negative-cache hits are free. Capped by daily_search_limit.
- openrouter_count / gemini_count: provider operations dispatched (generations,
  including backups and repair calls, successful or not; gemini_count also includes
  searches). Their sum is capped by global_daily_limit.
- user_counts: logical AI requests per user. One AIEngine.process() call costs one
  unit, however many provider operations it needed, and only if a provider served it
  (unserved requests are refunded). Capped by user_daily_limit.

Every cap uses try_reserve_*(), which checks and increments in one synchronous step.
The engine calls them with no await between the check and the dispatch, so concurrent
asyncio requests cannot all pass a check before any of them is counted.
"""
from __future__ import annotations

import datetime
import logging
from typing import Dict, Optional

from services.config import Config

logger = logging.getLogger(__name__)


class BudgetManager:
    """Tracks provider call volumes and enforces daily search grounding safety limits."""

    def __init__(
        self,
        config: Optional[Config] = None,
        daily_search_limit: Optional[int] = None,
        global_daily_limit: int = 5000,
        user_daily_limit: int = 100
    ):
        self.config = config or Config()
        self.daily_search_limit = (
            daily_search_limit
            if daily_search_limit is not None
            else getattr(self.config, "gemini_search_daily_limit", 450)
        )
        self.global_daily_limit = global_daily_limit
        self.user_daily_limit = user_daily_limit

        self.search_count: int = 0
        self.openrouter_count: int = 0
        self.gemini_count: int = 0
        # Bounded by the number of distinct users seen today; cleared at rollover.
        self.user_counts: Dict[str, int] = {}
        self.current_date: str = self._get_today()

    @property
    def daily_searches(self) -> int:
        return self.search_count

    @daily_searches.setter
    def daily_searches(self, value: int) -> None:
        self.search_count = value

    @property
    def max_daily_searches(self) -> int:
        return self.daily_search_limit

    @staticmethod
    def _get_today() -> str:
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    def _check_rollover(self) -> None:
        today = self._get_today()
        if today != self.current_date:
            logger.info("BudgetManager rolling over to new day: %s", today)
            self.current_date = today
            self.search_count = 0
            self.openrouter_count = 0
            self.gemini_count = 0
            self.user_counts.clear()

    def can_search(self, user_id: Optional[str] = None) -> bool:
        self._check_rollover()
        if self.search_count >= self.daily_search_limit:
            logger.warning(
                "Gemini search daily limit reached: %d/%d",
                self.search_count, self.daily_search_limit
            )
            return False

        if user_id:
            count = self.user_counts.get(user_id, 0)
            if count >= self.user_daily_limit:
                logger.warning("User %s daily AI budget exhausted: %d", user_id, count)
                return False

        return True

    def can_execute_ai(self, provider: str, user_id: Optional[str] = None) -> bool:
        self._check_rollover()
        total = self.openrouter_count + self.gemini_count
        if total >= self.global_daily_limit:
            return False

        if user_id:
            count = self.user_counts.get(user_id, 0)
            if count >= self.user_daily_limit:
                return False

        return True

    def try_reserve_search(self) -> bool:
        """Count one grounded search about to be dispatched; False if the daily cap is reached."""
        self._check_rollover()
        if self.search_count >= self.daily_search_limit:
            logger.warning("Gemini search daily limit reached: %d/%d", self.search_count, self.daily_search_limit)
            return False
        self.search_count += 1
        self.gemini_count += 1
        return True

    def try_reserve_generation(self, provider: str) -> bool:
        """Count one generation about to be dispatched; False if the global daily cap is reached."""
        self._check_rollover()
        if self.openrouter_count + self.gemini_count >= self.global_daily_limit:
            logger.warning("Global daily AI limit reached: %d", self.global_daily_limit)
            return False
        if provider == "openrouter":
            self.openrouter_count += 1
        elif provider == "gemini":
            self.gemini_count += 1
        return True

    def try_reserve_user_request(self, user_id: Optional[str]) -> bool:
        """Charge one unit of the user's daily budget; False if the user's cap is reached.

        Requests without a user ID are not user-limited (global caps still apply).
        """
        self._check_rollover()
        if not user_id:
            return True
        count = self.user_counts.get(user_id, 0)
        if count >= self.user_daily_limit:
            logger.warning("User %s daily AI budget exhausted: %d", user_id, count)
            return False
        self.user_counts[user_id] = count + 1
        return True

    def refund_user_request(self, user_id: Optional[str]) -> None:
        """Return a unit reserved for a request that no provider ended up serving."""
        self._check_rollover()
        if user_id and self.user_counts.get(user_id, 0) > 0:
            self.user_counts[user_id] -= 1

    def get_remaining_searches(self) -> int:
        self._check_rollover()
        return max(0, self.daily_search_limit - self.search_count)
