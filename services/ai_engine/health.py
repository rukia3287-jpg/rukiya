"""services/ai_engine/health.py
Independent capability tracking, adaptive circuit breakers, error-category cooldowns,
and time-decayed provider health metrics.
"""
from __future__ import annotations

import collections
import logging
import time
from typing import Dict, List, Optional, Tuple

from services.ai_engine.errors import ErrorCategory
from services.ai_engine.models import CapabilityState, CircuitState

logger = logging.getLogger(__name__)

# Default cooldowns by error category in seconds
ADAPTIVE_COOLDOWNS: Dict[ErrorCategory, float] = {
    ErrorCategory.RATE_LIMITED: 60.0,
    ErrorCategory.QUOTA_EXHAUSTED: 300.0,
    ErrorCategory.AUTH_ERROR: 600.0,
    ErrorCategory.BAD_REQUEST: 60.0,
    ErrorCategory.SERVER_ERROR: 30.0,
    ErrorCategory.NETWORK_ERROR: 10.0,
    ErrorCategory.TIMEOUT: 15.0,
    ErrorCategory.INVALID_RESPONSE: 20.0,
    ErrorCategory.UNKNOWN: 30.0,
}


class CircuitBreaker:
    """Three-state adaptive circuit breaker (CLOSED -> OPEN -> HALF_OPEN)."""

    def __init__(
        self,
        failure_threshold: int = 3,
        base_recovery_timeout: float = 30.0,
        half_open_success_threshold: int = 1,
        recovery_timeout: Optional[float] = None,
    ):
        if recovery_timeout is not None:
            base_recovery_timeout = recovery_timeout
        self.failure_threshold = failure_threshold
        self.base_recovery_timeout = base_recovery_timeout
        self.recovery_timeout = base_recovery_timeout
        self.half_open_success_threshold = half_open_success_threshold

        self.state: CircuitState = CircuitState.CLOSED
        self.consecutive_failures: int = 0
        self.consecutive_successes: int = 0
        self.last_failure_time: float = 0.0
        self.current_cooldown: float = base_recovery_timeout

    def can_attempt(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True

        now = time.time()
        if self.state == CircuitState.OPEN:
            if now - self.last_failure_time >= self.current_cooldown:
                logger.info("Circuit breaker cooldown (%.1fs) expired; entering HALF_OPEN state.", self.current_cooldown)
                self.state = CircuitState.HALF_OPEN
                self.consecutive_successes = 0
                return True
            return False

        if self.state == CircuitState.HALF_OPEN:
            return True

        return False

    def record_success(self) -> None:
        self.consecutive_failures = 0
        if self.state == CircuitState.HALF_OPEN:
            self.consecutive_successes += 1
            if self.consecutive_successes >= self.half_open_success_threshold:
                logger.info("Circuit breaker probe succeeded; returning to CLOSED state.")
                self.state = CircuitState.CLOSED
                self.consecutive_successes = 0
                self.current_cooldown = self.base_recovery_timeout

    def record_failure(self, cooldown: Optional[float] = None) -> None:
        self.consecutive_failures += 1
        self.last_failure_time = time.time()
        self.consecutive_successes = 0
        if cooldown is not None:
            self.current_cooldown = cooldown
        else:
            self.current_cooldown = self.base_recovery_timeout

        if self.state == CircuitState.HALF_OPEN:
            logger.warning("Failure in HALF_OPEN state; tripping circuit breaker back to OPEN (cooldown=%.1fs).", self.current_cooldown)
            self.state = CircuitState.OPEN
        elif self.consecutive_failures >= self.failure_threshold:
            logger.warning(
                "Failure threshold reached (%d); tripping circuit breaker to OPEN (cooldown=%.1fs).",
                self.consecutive_failures, self.current_cooldown
            )
            self.state = CircuitState.OPEN


class ProviderHealthRecord:
    """Metrics and statistics for a specific provider capability with time decay."""

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        stale_window_seconds: float = 600.0  # 10 minutes decay window
    ):
        self.failure_threshold = failure_threshold
        self.stale_window_seconds = stale_window_seconds
        # Stores (timestamp, success, latency_ms)
        self.history: collections.deque[Tuple[float, bool, float]] = collections.deque(maxlen=50)
        self.breaker = CircuitBreaker(failure_threshold, recovery_timeout)
        self.capability_state: CapabilityState = CapabilityState.AVAILABLE
        self.last_error_category: Optional[ErrorCategory] = None

    def _prune_stale(self) -> None:
        """Decay stale history beyond the reset window."""
        now = time.time()
        cutoff = now - self.stale_window_seconds
        while self.history and self.history[0][0] < cutoff:
            self.history.popleft()

    @property
    def total_calls(self) -> int:
        self._prune_stale()
        return len(self.history)

    @property
    def success_count(self) -> int:
        self._prune_stale()
        return sum(1 for _, s, _ in self.history if s)

    @property
    def success_rate(self) -> float:
        self._prune_stale()
        if not self.history:
            return 1.0
        return self.success_count / len(self.history)

    @property
    def average_latency(self) -> float:
        self._prune_stale()
        lats = [lat for _, s, lat in self.history if s]
        if not lats:
            return 500.0  # default nominal ms
        return sum(lats) / len(lats)

    def record(
        self,
        success: bool,
        latency_ms: float,
        error_category: Optional[ErrorCategory] = None,
        retry_after: Optional[float] = None
    ) -> None:
        now = time.time()
        self.history.append((now, success, latency_ms))

        if success:
            self.capability_state = CapabilityState.AVAILABLE
            self.last_error_category = None
            self.breaker.record_success()
        else:
            self.last_error_category = error_category
            # Determine adaptive cooldown
            cooldown = retry_after
            if cooldown is None and error_category:
                cooldown = ADAPTIVE_COOLDOWNS.get(error_category, 30.0)

            # Update capability state
            if error_category == ErrorCategory.RATE_LIMITED:
                self.capability_state = CapabilityState.RATE_LIMITED
            elif error_category == ErrorCategory.QUOTA_EXHAUSTED:
                self.capability_state = CapabilityState.QUOTA_EXHAUSTED
            elif error_category == ErrorCategory.AUTH_ERROR:
                self.capability_state = CapabilityState.AUTH_FAILED
            else:
                self.capability_state = CapabilityState.TEMPORARILY_UNAVAILABLE

            self.breaker.record_failure(cooldown=cooldown)

    def calculate_health_score(self) -> float:
        """
        Calculates health score (0.0 to 1.0) using Section 14 formula:
        health_score = success_rate * 0.45 + latency_score * 0.20 + availability * 0.20 + reliability_trend * 0.15
        """
        if self.breaker.state == CircuitState.OPEN:
            return 0.0
        if self.breaker.state == CircuitState.HALF_OPEN:
            return 0.25

        s_rate = self.success_rate

        # Latency score: 1.0 for <=500ms, decays down to 0.0 at 5000ms
        avg_lat = self.average_latency
        lat_score = max(0.0, min(1.0, 1.0 - ((avg_lat - 500.0) / 4500.0)))

        # Availability: 1.0 if CLOSED
        avail_score = 1.0

        # Reliability trend: last 5 calls ratio
        recent_5 = list(self.history)[-5:]
        trend_score = (sum(1 for _, s, _ in recent_5 if s) / len(recent_5)) if recent_5 else 1.0

        score = (s_rate * 0.45) + (lat_score * 0.20) + (avail_score * 0.20) + (trend_score * 0.15)
        return max(0.05, min(1.0, round(score, 3)))


class ProviderHealthTracker:
    """
    Manages provider capabilities independently:
    - openrouter:generation
    - gemini:generation
    - gemini:search
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        stale_window_seconds: float = 600.0
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.stale_window_seconds = stale_window_seconds
        # key -> ProviderHealthRecord
        self._records: Dict[str, ProviderHealthRecord] = {}

    def _normalize_key(self, provider: str, capability: Optional[str] = None) -> str:
        if ":" in provider:
            return provider.lower()
        cap = (capability or "generation").lower()
        return f"{provider.lower()}:{cap}"

    def _get_or_create(self, provider: str, capability: Optional[str] = None) -> ProviderHealthRecord:
        key = self._normalize_key(provider, capability)
        if key not in self._records:
            self._records[key] = ProviderHealthRecord(
                self.failure_threshold,
                self.recovery_timeout,
                self.stale_window_seconds
            )
        return self._records[key]

    @property
    def circuits(self) -> Dict[str, CircuitBreaker]:
        d = {}
        for k, rec in self._records.items():
            d[k] = rec.breaker
            prov = k.split(":")[0]
            if prov not in d:
                d[prov] = rec.breaker
        return d

    def record_call(
        self,
        provider: str,
        success: bool,
        latency_ms: float,
        error_category: Optional[ErrorCategory] = None,
        retry_after: Optional[float] = None,
        capability: Optional[str] = None,
        error: Optional[Any] = None,
    ) -> None:
        if error is not None and error_category is None:
            if hasattr(error, "category"):
                error_category = error.category
                if hasattr(error, "retry_after") and error.retry_after:
                    retry_after = error.retry_after
            elif isinstance(error, ErrorCategory):
                error_category = error
        rec = self._get_or_create(provider, capability)
        rec.record(success, latency_ms, error_category, retry_after)

    def is_available(self, provider: str, capability: Optional[str] = None) -> bool:
        """Check if capability circuit breaker allows attempts."""
        rec = self._get_or_create(provider, capability)
        return rec.breaker.can_attempt()

    def get_circuit_state(self, provider: str, capability: Optional[str] = None) -> CircuitState:
        return self._get_or_create(provider, capability).breaker.state

    def get_capability_state(self, provider: str, capability: Optional[str] = None) -> CapabilityState:
        rec = self._get_or_create(provider, capability)
        # If circuit breaker is OPEN due to rate limit, return RATE_LIMITED
        if rec.breaker.state == CircuitState.OPEN and rec.last_error_category == ErrorCategory.RATE_LIMITED:
            return CapabilityState.RATE_LIMITED
        if not rec.breaker.can_attempt():
            return CapabilityState.TEMPORARILY_UNAVAILABLE
        return rec.capability_state

    def get_health_score(self, provider: str, capability: Optional[str] = None) -> float:
        return self._get_or_create(provider, capability).calculate_health_score()
