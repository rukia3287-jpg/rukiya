"""services/ai_engine/budget.py
Budget manager tracking global and per-user limits, with daily search safety caps.
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
        self.user_counts: Dict[str, int] = {}
        self.current_date: str = self._get_today()

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

    def record_search(self, user_id: Optional[str] = None) -> None:
        self._check_rollover()
        self.search_count += 1
        self.gemini_count += 1
        if user_id:
            self.user_counts[user_id] = self.user_counts.get(user_id, 0) + 1

    def record_generation(self, provider: str, user_id: Optional[str] = None) -> None:
        self._check_rollover()
        if provider == "openrouter":
            self.openrouter_count += 1
        elif provider == "gemini":
            self.gemini_count += 1

        if user_id:
            self.user_counts[user_id] = self.user_counts.get(user_id, 0) + 1

    def get_remaining_searches(self) -> int:
        self._check_rollover()
        return max(0, self.daily_search_limit - self.search_count)
