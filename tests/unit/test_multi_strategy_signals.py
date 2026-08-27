"""Tests for the 9 NEW strategy signal functions
(ml/evaluation/multi_strategy_signals.py) against deterministic SYNTHETIC
OHLCV (seeded, reproducible -- same convention as tests/leakage/conftest.py's
make_ohlcv). Real historical BTC/USDT data is used for the actual research
verdict (scripts/research_multi_strategy.py, reports/
MULTI_STRATEGY_RESEARCH_RESULTS.txt) -- these unit tests only need to prove
each strategy's LONG/SHORT/NO_TRADE logic is reachable and produces valid
levels, which a synthetic series with genuine trend and volatility swings
exercises just as validly as real data, and portably (no dependency on the
non-committed local research DB file).
"""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from ml.evaluation.multi_strategy_signals import MULTI_STRATEGIES, precompute_mtf_trend, mtf_trend_signal_func
from services.feature_engine.indicators import supertrend


def _make_ohlcv(n: int, seed: int, freq_minutes: int, start_price: float = 40000.0) -> pd.DataFrame:
    """A random walk modulated by a slow sine wave -- guarantees several
    full up-legs, down-legs, and ranging stretches within one series
    (a pure random walk can go a very long time without a clean reversal,
    which would starve a strategy of its SHORT or LONG condition)."""
    rng = np.random.default_rng(seed)
    t0 = datetime(2024, 1, 1)
    timestamps = [t0 + timedelta(minutes=freq_minutes * i) for i in range(n)]

    trend = 3000 * np.sin(np.linspace(0, 6 * np.pi, n))
    noise = np.cumsum(rng.standard_normal(n) * 60)
    close = start_price + trend + noise
    close = np.maximum(close, 1000)  # keep strictly positive

    high = close + np.abs(rng.standard_normal(n) * 80) + 20
    low = close - np.abs(rng.standard_normal(n) * 80) - 20
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    volume = rng.uniform(50, 2000, n)
    # Occasional volume spikes so volume-confirmation logic (S01) has
    # something real to key off.
    spike_idx = rng.choice(n, size=max(1, n // 40), replace=False)
    volume[spike_idx] *= rng.uniform(3, 6, len(spike_idx))

    return pd.DataFrame({
        "timestamp": timestamps, "open": open_, "high": high, "low": low,
        "close": close, "volume": volume,
    })


@pytest.fixture(scope="module")
def synthetic_15m():
    return _make_ohlcv(n=3000, seed=101, freq_minutes=15)


@pytest.fixture(scope="module")
def synthetic_4h():
    return _make_ohlcv(n=2500, seed=202, freq_minutes=240)


@pytest.fixture(scope="module")
def synthetic_1d():
    return _make_ohlcv(n=400, seed=303, freq_minutes=1440)


def _signal_types_seen(df: pd.DataFrame, precompute_fn, signal_func, step: int) -> set:
    prepared = precompute_fn(df)
    seen = set()
    for i in range(50, len(prepared), step):
        raw = signal_func(prepared.iloc[:i + 1])
        if raw is not None:
            seen.add(raw["signal_type"])
    return seen


# ---- 1/2/3. Every strategy can return LONG, SHORT (where directionally
# symmetric), and NO_TRADE. ----

@pytest.mark.parametrize("strategy_id", ["S01_MOMENTUM_BREAKOUT_15M", "S02_EMA_PULLBACK_15M", "S03_VWAP_REVERSION_15M", "S04_RSI_BB_15M"])
def test_15m_strategy_produces_both_directions(strategy_id, synthetic_15m):
    spec = MULTI_STRATEGIES[strategy_id]
    seen = _signal_types_seen(synthetic_15m, spec["precompute"], spec["factory"](), step=3)
    assert "LONG" in seen, f"{strategy_id} never fired LONG"
    assert "SHORT" in seen, f"{strategy_id} never fired SHORT"


@pytest.mark.parametrize("strategy_id", ["S06_SUPERTREND_ATR_4H", "S07_MACD_MOMENTUM_4H", "S08_EMA_ADX_4H", "S09_ATR_BREAKOUT_4H"])
def test_4h_strategy_produces_both_directions(strategy_id, synthetic_4h):
    spec = MULTI_STRATEGIES[strategy_id]
    seen = _signal_types_seen(synthetic_4h, spec["precompute"], spec["factory"](), step=1)
    assert "LONG" in seen, f"{strategy_id} never fired LONG"
    assert "SHORT" in seen, f"{strategy_id} never fired SHORT"


def test_s10_mtf_trend_produces_both_directions(synthetic_4h, synthetic_1d):
    prepared = precompute_mtf_trend(synthetic_4h, synthetic_1d)
    signal_func = mtf_trend_signal_func(20, 20)
    seen = set()
    for i in range(60, len(prepared)):
        raw = signal_func(prepared.iloc[:i + 1])
        if raw is not None:
            seen.add(raw["signal_type"])
    assert "LONG" in seen
    assert "SHORT" in seen


@pytest.mark.parametrize("strategy_id", list(MULTI_STRATEGIES.keys()))
def test_every_strategy_returns_no_trade_with_barely_any_data(strategy_id, synthetic_15m, synthetic_4h, synthetic_1d):
    """A tiny slice of data (well under any strategy's warm-up requirement)
    must never fabricate a signal -- every signal_func returns None."""
    spec = MULTI_STRATEGIES[strategy_id]
    tiny = (synthetic_15m if spec["timeframe"] == "15m" else synthetic_4h).head(5).reset_index(drop=True)
    if strategy_id == "S10_MTF_TREND_4H":
        prepared = precompute_mtf_trend(tiny, synthetic_1d)
        raw = mtf_trend_signal_func(20, 20)(prepared)
    else:
        prepared = spec["precompute"](tiny)
        raw = spec["factory"]()(prepared)
    assert raw is None


# ---- 16. Entry/SL/TP are valid: SL on the loss side, TPs on the profit
# side, for every signal any strategy produces. ----

def _assert_valid_levels(signal_type: str, entry: float, sl, tp1, tp2, tp3):
    if sl is not None:
        if signal_type == "LONG":
            assert sl < entry, "LONG stop-loss must be below entry"
        else:
            assert sl > entry, "SHORT stop-loss must be above entry"
    for tp in (tp1, tp2, tp3):
        if tp is None:
            continue
        if signal_type == "LONG":
            assert tp > entry, "LONG take-profit must be above entry"
        else:
            assert tp < entry, "SHORT take-profit must be below entry"


@pytest.mark.parametrize("strategy_id", ["S01_MOMENTUM_BREAKOUT_15M", "S02_EMA_PULLBACK_15M", "S03_VWAP_REVERSION_15M", "S04_RSI_BB_15M"])
def test_15m_strategy_entry_sl_tp_are_valid(strategy_id, synthetic_15m):
    spec = MULTI_STRATEGIES[strategy_id]
    prepared = spec["precompute"](synthetic_15m)
    signal_func = spec["factory"]()
    checked = 0
    for i in range(50, len(prepared), 3):
        raw = signal_func(prepared.iloc[:i + 1])
        if raw is None:
            continue
        entry = float(prepared.iloc[i]["close"])
        _assert_valid_levels(raw["signal_type"], entry, raw.get("stop_loss"), raw.get("take_profit_1"), raw.get("take_profit_2"), raw.get("take_profit_3"))
        checked += 1
    assert checked > 0, f"{strategy_id} never produced a signal to validate levels against"


@pytest.mark.parametrize("strategy_id", ["S06_SUPERTREND_ATR_4H", "S07_MACD_MOMENTUM_4H", "S08_EMA_ADX_4H", "S09_ATR_BREAKOUT_4H"])
def test_4h_strategy_entry_sl_tp_are_valid(strategy_id, synthetic_4h):
    spec = MULTI_STRATEGIES[strategy_id]
    prepared = spec["precompute"](synthetic_4h)
    signal_func = spec["factory"]()
    checked = 0
    for i in range(50, len(prepared)):
        raw = signal_func(prepared.iloc[:i + 1])
        if raw is None:
            continue
        entry = float(prepared.iloc[i]["close"])
        _assert_valid_levels(raw["signal_type"], entry, raw.get("stop_loss"), raw.get("take_profit_1"), raw.get("take_profit_2"), raw.get("take_profit_3"))
        checked += 1
    assert checked > 0


# ---- Supertrend indicator regression test -- a real bug found and fixed
# this session: the direction series got stuck at +1 forever because the
# transition row (first row with a valid ATR) tried to tighten against the
# PREVIOUS row's still-NaN bands instead of starting fresh. ----

def test_supertrend_direction_is_not_stuck(synthetic_4h):
    line, direction = supertrend(synthetic_4h["high"], synthetic_4h["low"], synthetic_4h["close"], 10, 3.0)
    flips = int((direction != direction.shift(1)).sum())
    assert flips > 5, "Supertrend direction must flip meaningfully often over a trending+reverting series"
    assert direction.iloc[-1] in (1, -1)
    assert not pd.isna(line.iloc[-1])


def test_supertrend_never_produces_nan_line_after_warmup(synthetic_4h):
    line, _ = supertrend(synthetic_4h["high"], synthetic_4h["low"], synthetic_4h["close"], 10, 3.0)
    assert line.iloc[30:].isna().sum() == 0


def test_supertrend_first_row_is_a_clean_reset_not_partial_nan():
    df = _make_ohlcv(n=60, seed=7, freq_minutes=240)
    line, direction = supertrend(df["high"], df["low"], df["close"], 10, 3.0)
    # Before ATR warms up (first `period` rows), the line is genuinely
    # unknown -- NaN is correct there, not a bug. After warmup it must
    # never be NaN (the original bug produced NaN forever after this point).
    assert line.iloc[:9].isna().all()
    assert line.iloc[10:].isna().sum() == 0
