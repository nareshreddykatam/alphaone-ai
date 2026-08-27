"""Circuit breaker (Phase 5, section 33): if the CoinDCX API repeatedly
fails, stop hammering it and back off exponentially instead. Pure,
clock-injectable logic (mirrors the RiskEngine's own `now` parameter
convention from Phase 2.6) so it's fully unit-testable without real
sleeps or real API calls.

States: CLOSED (normal) -> OPEN (failing, all calls short-circuited) ->
HALF_OPEN (one trial call allowed after the backoff window) -> CLOSED (on
success) or OPEN again (on failure).
"""
import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


class CircuitState(str, enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(Exception):
    """Raised by call_guard() when the circuit is open -- the caller must
    not attempt the underlying request."""


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5
    base_backoff_seconds: float = 5.0
    max_backoff_seconds: float = 300.0


@dataclass
class CircuitBreaker:
    config: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: Optional[datetime] = None
    next_retry_at: Optional[datetime] = None

    def _backoff_seconds(self) -> float:
        # Exponential: base * 2^(failures - threshold), capped.
        exponent = max(0, self.consecutive_failures - self.config.failure_threshold)
        seconds = self.config.base_backoff_seconds * (2 ** exponent)
        return min(seconds, self.config.max_backoff_seconds)

    def can_attempt(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.utcnow()
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if self.next_retry_at is not None and now >= self.next_retry_at:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        return True  # HALF_OPEN: allow the one trial call

    def record_success(self, now: Optional[datetime] = None) -> None:
        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.opened_at = None
        self.next_retry_at = None

    def record_failure(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.utcnow()
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.config.failure_threshold:
            self.state = CircuitState.OPEN
            self.opened_at = now
            self.next_retry_at = now + timedelta(seconds=self._backoff_seconds())
