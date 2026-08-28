"""services/live_execution/timestamp_safety.py -- Contract Audit V2,
Phase 6. CoinDCX's own documented order-creation contract: "Orders with a
delay of more than 10 seconds will be rejected."
"""
from services.live_execution.timestamp_safety import (
    current_order_timestamp_ms, is_order_timestamp_fresh,
    ORDER_TIMESTAMP_MAX_AGE_SECONDS, SAFE_SUBMISSION_MARGIN_SECONDS,
)


def test_current_order_timestamp_is_milliseconds_not_seconds():
    ts = current_order_timestamp_ms(now=1700000000.0)
    assert ts == 1700000000000


def test_fresh_timestamp_passes():
    now = 1700000000.0
    built_at_ms = current_order_timestamp_ms(now=now)
    result = is_order_timestamp_fresh(built_at_ms, now=now + 1)  # 1s later
    assert result.ok is True


def test_timestamp_right_at_the_documented_cutoff_is_rejected_by_the_safety_margin():
    now = 1700000000.0
    built_at_ms = current_order_timestamp_ms(now=now)
    result = is_order_timestamp_fresh(built_at_ms, now=now + ORDER_TIMESTAMP_MAX_AGE_SECONDS)
    assert result.ok is False  # AlphaOne's own safety margin is tighter than the exchange's real cutoff


def test_timestamp_within_the_safe_submission_window_passes():
    now = 1700000000.0
    built_at_ms = current_order_timestamp_ms(now=now)
    safe_age = ORDER_TIMESTAMP_MAX_AGE_SECONDS - SAFE_SUBMISSION_MARGIN_SECONDS - 1
    result = is_order_timestamp_fresh(built_at_ms, now=now + safe_age)
    assert result.ok is True


def test_stale_timestamp_beyond_ten_seconds_is_rejected():
    now = 1700000000.0
    built_at_ms = current_order_timestamp_ms(now=now)
    result = is_order_timestamp_fresh(built_at_ms, now=now + 15)
    assert result.ok is False
    assert "old" in result.reason.lower()


def test_future_timestamp_clock_skew_is_rejected():
    now = 1700000000.0
    built_at_ms = current_order_timestamp_ms(now=now + 5)  # built "in the future" relative to `now`
    result = is_order_timestamp_fresh(built_at_ms, now=now)
    assert result.ok is False
    assert "future" in result.reason.lower() or "skew" in result.reason.lower()


def test_age_seconds_is_reported_for_diagnostics():
    now = 1700000000.0
    built_at_ms = current_order_timestamp_ms(now=now)
    result = is_order_timestamp_fresh(built_at_ms, now=now + 2.5)
    assert result.age_seconds == 2.5
