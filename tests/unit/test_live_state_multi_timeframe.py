"""Tests for services/market_data/live_state.py's live_candle_aggregators
registry -- generalized from a single 4h-only instance to one
LiveCandleAggregator per supported timeframe (1m/5m/15m/1h/4h/1d), so the
Live Chart can show a correct forming candle for any timeframe tab, not
just the one the validated strategy trades. Pure in-process checks, no
real WebSocket connection or database.
"""
from services.market_data.live_state import SUPPORTED_LIVE_TIMEFRAMES, live_candle_aggregators
from services.signal_engine.live_breakout import LiveCandleAggregator


def test_registry_has_exactly_the_six_supported_timeframes():
    assert set(live_candle_aggregators.keys()) == {"1m", "5m", "15m", "1h", "4h", "1d"}
    assert set(SUPPORTED_LIVE_TIMEFRAMES) == set(live_candle_aggregators.keys())


def test_every_registry_entry_is_a_live_candle_aggregator_for_its_own_timeframe():
    for tf, agg in live_candle_aggregators.items():
        assert isinstance(agg, LiveCandleAggregator)
        assert agg._interval_seconds == {
            "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400,
        }[tf]


def test_registry_entries_are_distinct_objects_not_one_instance_reused():
    aggregators = list(live_candle_aggregators.values())
    assert len(aggregators) == len(set(id(a) for a in aggregators))


def test_registry_is_a_module_level_singleton_across_imports():
    """Every importer must see the exact same dict/objects -- re-importing
    the module must not construct a second registry."""
    from services.market_data import live_state as live_state_again
    assert live_state_again.live_candle_aggregators is live_candle_aggregators
    for tf in SUPPORTED_LIVE_TIMEFRAMES:
        assert live_state_again.live_candle_aggregators[tf] is live_candle_aggregators[tf]


def test_ticks_on_one_timeframes_aggregator_never_affect_another():
    """A tick fed to the 1m aggregator (e.g. via the chart polling a 1m
    tab) must not perturb the 4h aggregator the live signal engine reads,
    and vice versa -- they are independent objects with independent state."""
    from datetime import datetime

    one_min = live_candle_aggregators["1m"]
    four_hour = live_candle_aggregators["4h"]
    before = four_hour.current

    one_min.on_tick(123456.0, ts=datetime(2030, 1, 1, 0, 0, 30))

    assert four_hour.current is before  # untouched by the 1m tick
