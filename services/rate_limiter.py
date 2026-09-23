"""services/rate_limiter.py
Token-Bucket Rate Limiter for Rukiya V2.

Provides bounded rate limiting across multiple distinct scopes:
- global_ai: global AI inference budget
- user_ai: per-user AI response budget
- youtube_send: YouTube message send budget
- idle_chat: idle livestream chat interval
- discord_ai: Discord interaction budget
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

from services.config import Config
from services.models import RateLimitResult

logger = logging.getLogger(__name__)


class TokenBucket:
    """Individual token bucket with constant refill rate."""

    def __init__(self, capacity: float, refill_rate: float):
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)  # tokens per second
        self.tokens = float(capacity)
        self.last_refill = time.time()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.last_refill)
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + (elapsed * self.refill_rate))
            self.last_refill = now

    def consume(self, cost: float = 1.0) -> Tuple[bool, float]:
        """Attempt to consume tokens. Returns (allowed, wait_time_if_not_allowed)."""
        now = time.time()
        self._refill(now)
        if self.tokens >= cost:
            self.tokens -= cost
            return True, 0.0
        # Calculate time until enough tokens are available
        deficit = cost - self.tokens
        wait_time = deficit / self.refill_rate if self.refill_rate > 0 else 999.0
        return False, wait_time

    def get_remaining(self) -> float:
        self._refill(time.time())
        return max(0.0, self.tokens)


class RateLimiter:
    """Multi-bucket rate limiter managing global and per-user capacities."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self._buckets: Dict[str, TokenBucket] = {}

        # Default bucket configurations: (capacity, refill_rate_per_sec)
        global_cap = getattr(self.config, "rate_limit_global_capacity", 10)
        user_cap = getattr(self.config, "rate_limit_user_capacity", 2)
        idle_int = getattr(self.config, "rate_limit_idle_interval", 180.0)

        self._default_specs = {
            "global_ai": (global_cap, 1.0),                  # 10 burst, refill 1/s
            "user_ai": (user_cap, 0.2),                      # 2 burst, refill 1 every 5s
            "youtube_send": (5, 0.5),                        # 5 burst, refill 1 every 2s
            "idle_chat": (1, 1.0 / max(1.0, idle_int)),      # 1 token every idle_interval
            "discord_ai": (5, 0.5),                          # 5 burst, refill 1 every 2s
        }

    def _get_bucket_key(self, bucket_type: str, key: Optional[str] = None) -> str:
        return f"{bucket_type}:{key}" if key else bucket_type

    def _get_or_create_bucket(self, bucket_type: str, key: Optional[str] = None) -> TokenBucket:
        b_key = self._get_bucket_key(bucket_type, key)
        if b_key not in self._buckets:
            spec = self._default_specs.get(bucket_type, (5.0, 1.0))
            self._buckets[b_key] = TokenBucket(capacity=spec[0], refill_rate=spec[1])
        return self._buckets[b_key]

    def allow(self, bucket_type: str, key: Optional[str] = None, cost: float = 1.0) -> RateLimitResult:
        """
        Check if request is permitted under rate limit.
        If allowed, tokens are deducted automatically.
        """
        bucket = self._get_or_create_bucket(bucket_type, key)
        allowed, wait_time = bucket.consume(cost)
        b_key = self._get_bucket_key(bucket_type, key)

        if not allowed:
            logger.warning("Rate limit exceeded on '%s'; wait %.1fs", b_key, wait_time)

        return RateLimitResult(
            allowed=allowed,
            wait_time=wait_time,
            bucket_name=b_key
        )

    def get_remaining(self, bucket_type: str, key: Optional[str] = None) -> float:
        bucket = self._get_or_create_bucket(bucket_type, key)
        return bucket.get_remaining()

    def reset(self, bucket_type: str, key: Optional[str] = None) -> None:
        b_key = self._get_bucket_key(bucket_type, key)
        self._buckets.pop(b_key, None)
