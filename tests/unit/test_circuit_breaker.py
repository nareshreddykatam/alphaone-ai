"""Phase 5, section 33: circuit breaker must stop hammering a failing API
after `failure_threshold` consecutive failures, back off exponentially,
and only allow a single trial call once the backoff window elapses."""
from datetime import datetime, timedelta

from services.scheduler.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState


def test_stays_closed_and_allows_calls_below_threshold():
    cb = CircuitBreaker(config=CircuitBreakerConfig(failure_threshold=3))
    now = datetime(2026, 1, 1)
    for _ in range(2):
        assert cb.can_attempt(now) is True
        cb.record_failure(now)
    assert cb.state == CircuitState.CLOSED


def test_opens_after_threshold_and_blocks_calls():
    cb = CircuitBreaker(config=CircuitBreakerConfig(failure_threshold=3, base_backoff_seconds=10))
    now = datetime(2026, 1, 1)
    for _ in range(3):
        cb.can_attempt(now)
        cb.record_failure(now)
    assert cb.state == CircuitState.OPEN
    assert cb.can_attempt(now) is False  # still within backoff window


def test_half_open_after_backoff_window_elapses():
    cb = CircuitBreaker(config=CircuitBreakerConfig(failure_threshold=3, base_backoff_seconds=10))
    now = datetime(2026, 1, 1)
    for _ in range(3):
        cb.can_attempt(now)
        cb.record_failure(now)

    later = now + timedelta(seconds=11)
    assert cb.can_attempt(later) is True
    assert cb.state == CircuitState.HALF_OPEN


def test_success_in_half_open_closes_the_circuit():
    cb = CircuitBreaker(config=CircuitBreakerConfig(failure_threshold=3, base_backoff_seconds=10))
    now = datetime(2026, 1, 1)
    for _ in range(3):
        cb.can_attempt(now)
        cb.record_failure(now)
    later = now + timedelta(seconds=11)
    cb.can_attempt(later)
    cb.record_success(later)
    assert cb.state == CircuitState.CLOSED
    assert cb.consecutive_failures == 0
    assert cb.can_attempt(later) is True


def test_failure_in_half_open_reopens_with_longer_backoff():
    cb = CircuitBreaker(config=CircuitBreakerConfig(failure_threshold=3, base_backoff_seconds=10, max_backoff_seconds=1000))
    now = datetime(2026, 1, 1)
    for _ in range(3):
        cb.can_attempt(now)
        cb.record_failure(now)
    first_retry_at = cb.next_retry_at

    later = now + timedelta(seconds=11)
    cb.can_attempt(later)  # -> HALF_OPEN
    cb.record_failure(later)  # fails again -> OPEN, longer backoff
    assert cb.state == CircuitState.OPEN
    assert cb.next_retry_at > first_retry_at


def test_backoff_is_capped_at_max():
    cb = CircuitBreaker(config=CircuitBreakerConfig(failure_threshold=1, base_backoff_seconds=100, max_backoff_seconds=150))
    now = datetime(2026, 1, 1)
    for _ in range(10):
        cb.can_attempt(now)
        cb.record_failure(now)
    backoff = (cb.next_retry_at - cb.opened_at).total_seconds()
    assert backoff <= 150
