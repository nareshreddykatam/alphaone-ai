"""Tests for services/signal_engine/live_breakout.py: LiveCandleAggregator
-- pure, stateful tick-to-forming-candle aggregation, never a real
connection or real time."""
from datetime import datetime

import pytest

from services.signal_engine.live_breakout import LiveCandleAggregator


def test_first_tick_opens_a_new_forming_candle():
    agg = LiveCandleAggregator(timeframe="4h")
    candle = agg.on_tick(100.0, ts=datetime(2026, 1, 1, 0, 5, 0))
    assert candle.open == 100.0
    assert candle.high == 100.0
    assert candle.low == 100.0
    assert candle.close == 100.0
    assert candle.tick_count == 1


def test_bucket_start_aligns_to_the_timeframe_boundary():
    agg = LiveCandleAggregator(timeframe="4h")
    candle = agg.on_tick(100.0, ts=datetime(2026, 1, 1, 5, 47, 33))
    assert candle.open_time == datetime(2026, 1, 1, 4, 0, 0)  # floored to the 4h boundary (00/04/08/...)


def test_subsequent_ticks_within_the_same_bucket_update_high_low_close_not_open():
    agg = LiveCandleAggregator(timeframe="4h")
    agg.on_tick(100.0, ts=datetime(2026, 1, 1, 0, 5, 0))
    agg.on_tick(105.0, ts=datetime(2026, 1, 1, 1, 0, 0))
    candle = agg.on_tick(98.0, ts=datetime(2026, 1, 1, 2, 0, 0))
    assert candle.open == 100.0  # unchanged from the first tick
    assert candle.high == 105.0
    assert candle.low == 98.0
    assert candle.close == 98.0
    assert candle.tick_count == 3


def test_a_tick_in_a_new_bucket_starts_a_fresh_forming_candle():
    agg = LiveCandleAggregator(timeframe="4h")
    agg.on_tick(100.0, ts=datetime(2026, 1, 1, 3, 59, 0))
    candle = agg.on_tick(200.0, ts=datetime(2026, 1, 1, 4, 0, 1))
    assert candle.open_time == datetime(2026, 1, 1, 4, 0, 0)
    assert candle.open == 200.0
    assert candle.high == 200.0
    assert candle.tick_count == 1  # reset, not accumulated across the boundary


def test_defaults_to_now_when_no_timestamp_given():
    agg = LiveCandleAggregator(timeframe="1h")
    before = datetime.utcnow()
    candle = agg.on_tick(50.0)
    after = datetime.utcnow()
    assert before.replace(minute=0, second=0, microsecond=0) <= candle.open_time <= after


def test_unsupported_timeframe_raises_rather_than_guessing():
    with pytest.raises(ValueError):
        LiveCandleAggregator(timeframe="7m")


@pytest.mark.parametrize("timeframe,ts,expected_open", [
    ("15m", datetime(2026, 1, 1, 0, 22, 0), datetime(2026, 1, 1, 0, 15, 0)),
    ("1h", datetime(2026, 1, 1, 5, 59, 59), datetime(2026, 1, 1, 5, 0, 0)),
    ("1d", datetime(2026, 1, 1, 23, 59, 0), datetime(2026, 1, 1, 0, 0, 0)),
])
def test_bucket_alignment_across_timeframes(timeframe, ts, expected_open):
    agg = LiveCandleAggregator(timeframe=timeframe)
    candle = agg.on_tick(1.0, ts=ts)
    assert candle.open_time == expected_open
