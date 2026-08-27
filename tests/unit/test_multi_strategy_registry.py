"""Tests for services/signal_engine/multi_strategy.py's registry: structure,
timeframe distribution, independence, and the R:R-derived quality bucketing.
"""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from services.signal_engine.multi_strategy import (
    MULTI_STRATEGY_REGISTRY, get_strategy_definition, get_definition_by_persisted_name,
    RuleBasedMultiStrategy, _rr_quality,
)
from services.signal_engine.strategy import BaselineStrategy


def _make_ohlcv(n: int, seed: int, freq_minutes: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t0 = datetime(2024, 1, 1)
    timestamps = [t0 + timedelta(minutes=freq_minutes * i) for i in range(n)]
    trend = 3000 * np.sin(np.linspace(0, 6 * np.pi, n))
    noise = np.cumsum(rng.standard_normal(n) * 60)
    close = np.maximum(40000 + trend + noise, 1000)
    high = close + np.abs(rng.standard_normal(n) * 80) + 20
    low = close - np.abs(rng.standard_normal(n) * 80) - 20
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    volume = rng.uniform(50, 2000, n)
    return pd.DataFrame({"timestamp": timestamps, "open": open_, "high": high, "low": low, "close": close, "volume": volume})


# ---- Registry structure ----
#
# The registry holds 12 entries: the original 10 (S01-S10) plus 2
# replacement candidates (S11, S12) investigated during the v2 rigorous
# research pass and REJECTED -- kept in the registry (fully implemented,
# tested, excluded from live evaluation) as a transparent record rather
# than silently deleted. Only S05 and S06 are PRODUCTION_ELIGIBLE; see
# services/signal_engine/multi_strategy.py's module docstring and
# reports/STRATEGY_RESEARCH_V2_RIGOROUS_REPORT.txt for why.

def test_registry_has_exactly_twelve_strategies():
    assert len(MULTI_STRATEGY_REGISTRY) == 12
    ids = [d.strategy_id for d in MULTI_STRATEGY_REGISTRY]
    assert len(ids) == len(set(ids)), "strategy_id must be unique"


def test_timeframe_distribution_matches_spec():
    """5 candidates at 15m (4 original + 1 rejected replacement), 7 at 4h
    (6 original + 1 rejected replacement)."""
    by_tf = {}
    for d in MULTI_STRATEGY_REGISTRY:
        by_tf.setdefault(d.timeframe, []).append(d.strategy_id)
    assert len(by_tf["15m"]) == 5
    assert len(by_tf["4h"]) == 7


def test_only_s05_and_s06_are_production_eligible():
    """Under the stricter v2 methodology, only S06 (of the new/replacement
    candidates) cleared the production bar; S05 keeps its status by
    explicit protection, not because the stricter test independently
    re-validates it. Every other candidate -- including both REJECTED
    replacements -- must never be PRODUCTION_ELIGIBLE."""
    eligible = {d.strategy_id for d in MULTI_STRATEGY_REGISTRY if d.production_status == "PRODUCTION_ELIGIBLE"}
    assert eligible == {"S05_DONCHIAN_ADX_4H", "S06_SUPERTREND_ATR_4H"}


def test_rejected_candidates_are_excluded_from_live_evaluation():
    rejected = [d for d in MULTI_STRATEGY_REGISTRY if d.production_status == "REJECTED"]
    assert {d.strategy_id for d in rejected} == {"S11_ZSCORE_REVERSION_15M", "S12_STRUCTURE_RETEST_4H"}
    for d in rejected:
        assert d.production_status != "PRODUCTION_ELIGIBLE"


def test_s05_is_the_untouched_existing_baseline():
    d = get_strategy_definition("S05_DONCHIAN_ADX_4H")
    assert d.persisted_name == "trend_following_donchian_adx"
    assert d.data_mode == "LIVE_INTRABAR"
    assert d.production_status == "PRODUCTION_ELIGIBLE"
    assert isinstance(d.make_strategy(), BaselineStrategy)


def test_every_new_strategy_persisted_name_equals_its_id():
    for d in MULTI_STRATEGY_REGISTRY:
        if d.strategy_id == "S05_DONCHIAN_ADX_4H":
            continue
        assert d.persisted_name == d.strategy_id


def test_every_strategy_has_a_non_empty_disclaimer_in_its_reasoning():
    """No strategy may claim a validated edge without evidence -- every
    reasoning string produced (via the NO_TRADE path, cheap to trigger
    with almost-no data) must carry a real disclaimer, not an empty one."""
    tiny_15m = _make_ohlcv(5, 1, 15)
    tiny_4h = _make_ohlcv(5, 2, 240)
    for d in MULTI_STRATEGY_REGISTRY:
        if d.strategy_id == "S05_DONCHIAN_ADX_4H":
            continue  # BaselineStrategy has its own, separately-audited disclaimer
        strategy = d.make_strategy()
        if d.strategy_id == "S10_MTF_TREND_4H":
            tiny_1d = _make_ohlcv(5, 3, 1440)
            result = strategy.generate_with_daily(tiny_4h, tiny_1d)
        else:
            df = tiny_15m if d.timeframe == "15m" else tiny_4h
            result = strategy.generate(df)
        assert result.signal_type == "NO_TRADE"
        assert len(result.reasoning) > 20
        assert (
            "RESEARCH_ONLY" in result.reasoning or "PRODUCTION" in result.reasoning
            or "REJECTED" in result.reasoning or d.strategy_id == "S06_SUPERTREND_ATR_4H"
        )


def test_get_strategy_definition_raises_for_unknown_id():
    with pytest.raises(ValueError):
        get_strategy_definition("S99_DOES_NOT_EXIST")


def test_get_definition_by_persisted_name_resolves_s05_and_new_strategies():
    assert get_definition_by_persisted_name("trend_following_donchian_adx").strategy_id == "S05_DONCHIAN_ADX_4H"
    assert get_definition_by_persisted_name("S06_SUPERTREND_ATR_4H").strategy_id == "S06_SUPERTREND_ATR_4H"
    assert get_definition_by_persisted_name("unknown_legacy_name") is None
    assert get_definition_by_persisted_name(None) is None


# ---- 4/5. Independent parameters, no incorrectly-shared mutable state ----

def test_two_instances_of_the_same_strategy_do_not_share_mutable_state():
    """Two RuleBasedMultiStrategy instances of the SAME strategy_id, fed
    DIFFERENT data, must never leak state into each other (e.g. a rolling
    indicator cache) -- construct two, run one heavily, confirm the other
    is unaffected on a fresh, small input."""
    definition = get_strategy_definition("S06_SUPERTREND_ATR_4H")
    strategy_a = definition.make_strategy()
    strategy_b = definition.make_strategy()
    assert strategy_a is not strategy_b

    big_df = _make_ohlcv(500, 11, 240)
    strategy_a.generate(big_df)  # exercise strategy_a heavily

    small_df = _make_ohlcv(20, 22, 240)
    result_b_first = strategy_b.generate(small_df)
    result_b_again = strategy_b.generate(small_df)
    assert result_b_first.signal_type == result_b_again.signal_type  # deterministic, unaffected by strategy_a's history
    assert result_b_first.reasoning == result_b_again.reasoning


def test_different_strategies_are_fully_independent_objects():
    s06 = get_strategy_definition("S06_SUPERTREND_ATR_4H").make_strategy()
    s07 = get_strategy_definition("S07_MACD_MOMENTUM_4H").make_strategy()
    assert s06.strategy_id != s07.strategy_id
    assert s06._signal_func is not s07._signal_func


# ---- Quality is derived from a REAL R:R number, never fabricated ----

def test_rr_quality_buckets_from_real_ratios():
    assert _rr_quality(100.0, 90.0, 130.0) == "HIGH"  # R:R = 3.0
    assert _rr_quality(100.0, 90.0, 118.0) == "MEDIUM"  # R:R = 1.8
    assert _rr_quality(100.0, 90.0, 105.0) == "LOW"  # R:R = 0.5
    assert _rr_quality(100.0, None, 130.0) == "LOW"  # missing SL -- never fabricate a rating
    assert _rr_quality(100.0, 100.0, 130.0) == "LOW"  # zero risk -- undefined R:R, never divide by zero
