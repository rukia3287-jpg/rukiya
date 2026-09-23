"""services/ai_engine/health.py
Provider health tracking, latency moving averages, and circuit breaker pattern.
"""
from __future__ import annotations

import collections
import logging
import time
from typing import Dict, List

from services.ai_engine.models import CircuitState

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Three-state circuit breaker (CLOSED -> OPEN -> HALF_OPEN)."""

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
        half_open_success_threshold: int = 2
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_success_threshold = half_open_success_threshold

        self.state: CircuitState = CircuitState.CLOSED
        self.consecutive_failures: int = 0
        self.consecutive_successes: int = 0
        self.last_failure_time: float = 0.0

    def can_attempt(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True

        now = time.time()
        if self.state == CircuitState.OPEN:
            if now - self.last_failure_time >= self.recovery_timeout:
                logger.info("Circuit breaker cooldown expired; entering HALF_OPEN state.")
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
                logger.info("Circuit breaker recovered; returning to CLOSED state.")
                self.state = CircuitState.CLOSED
                self.consecutive_successes = 0

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self.last_failure_time = time.time()
        self.consecutive_successes = 0

        if self.state == CircuitState.HALF_OPEN:
            logger.warning("Failure in HALF_OPEN state; tripping circuit breaker back to OPEN.")
            self.state = CircuitState.OPEN
        elif self.consecutive_failures >= self.failure_threshold:
            logger.warning(
                "Failure threshold reached (%d); tripping circuit breaker to OPEN.",
                self.consecutive_failures
            )
            self.state = CircuitState.OPEN


class ProviderHealthRecord:
    """Metrics and statistics for a single provider."""

    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 30.0):
        self.total_calls: int = 0
        self.success_count: int = 0
        self.latencies: collections.deque[float] = collections.deque(maxlen=20)
        self.breaker = CircuitBreaker(failure_threshold, recovery_timeout)

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return self.success_count / self.total_calls

    @property
    def average_latency(self) -> float:
        if not self.latencies:
            return 500.0  # nominal default ms
        return sum(self.latencies) / len(self.latencies)

    def record(self, success: bool, latency_ms: float) -> None:
        self.total_calls += 1
        if success:
            self.success_count += 1
            self.latencies.append(latency_ms)
            self.breaker.record_success()
        else:
            self.breaker.record_failure()


class ProviderHealthTracker:
    """Global manager for provider health metrics and circuit breakers."""

    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._records: Dict[str, ProviderHealthRecord] = {}

    def _get_or_create(self, provider: str) -> ProviderHealthRecord:
        if provider not in self._records:
            self._records[provider] = ProviderHealthRecord(
                self.failure_threshold, self.recovery_timeout
            )
        return self._records[provider]

    def record_call(self, provider: str, success: bool, latency_ms: float) -> None:
        rec = self._get_or_create(provider)
        rec.record(success, latency_ms)

    def is_available(self, provider: str) -> bool:
        rec = self._get_or_create(provider)
        return rec.breaker.can_attempt()

    def get_circuit_state(self, provider: str) -> CircuitState:
        return self._get_or_create(provider).breaker.state

    def get_health_score(self, provider: str) -> float:
        """Returns health score between 0.0 (offline/broken) and 1.0 (optimal)."""
        rec = self._get_or_create(provider)
        if rec.breaker.state == CircuitState.OPEN:
            return 0.0
        if rec.breaker.state == CircuitState.HALF_OPEN:
            return 0.3

        # Weighted calculation based on success rate and latency penalty
        rate = rec.success_rate
        avg_lat = rec.average_latency
        lat_penalty = min(0.3, max(0.0, (avg_lat - 1000.0) / 10000.0))
        score = (rate * 0.7) + (0.3 - lat_penalty)
        return max(0.1, min(1.0, score))
