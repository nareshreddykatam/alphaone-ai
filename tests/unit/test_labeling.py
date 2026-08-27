"""Phase 3: triple-barrier labeling correctness and leakage guarantees."""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from ml.labeling import compute_triple_barrier_labels, TripleBarrierConfig, LONG_LABEL, SHORT_LABEL, NO_TRADE_LABEL, label_distribution


def _bars(specs, start=datetime(2024, 1, 1)):
    rows = []
    for i, (o, h, l, c, atr) in enumerate(specs):
        rows.append({"timestamp": start + timedelta(hours=i), "open": o, "high": h, "low": l, "close": c, "atr_14": atr})
    return pd.DataFrame(rows)


def test_long_label_when_target_hit_before_stop():
    # bar0: signal bar (atr=1). entry at bar1 open=100. TP=100+2=102, SL=100-1=99.
    rows = [
        (100, 101, 99, 100, 1.0),
        (100, 100.5, 99.5, 100, 1.0),   # entry bar: open=100, quiet -- stays inside both barriers
        (100, 103, 99.5, 102, 1.0),  # hits TP (high=103>=102) before SL (low=99.5>99)
        (102, 103, 101, 102, 1.0),
    ]
    df = _bars(rows)
    labeled = compute_triple_barrier_labels(df, TripleBarrierConfig(horizon_bars=2, tp_atr_multiple=2.0, sl_atr_multiple=1.0, min_risk_reward=1.0))
    row0 = labeled[labeled.index == 0]
    assert len(row0) == 1
    assert row0.iloc[0]["label"] == LONG_LABEL


def test_short_label_when_target_hit_before_stop():
    rows = [
        (100, 101, 99, 100, 1.0),
        (100, 100.5, 99.5, 100, 1.0),    # entry bar: open=100, quiet -- short TP=98, SL=101, stays inside both
        (100, 100.5, 97, 98, 1.0),   # hits short TP (low=97<=98) before SL (high=100.5<101)
        (98, 99, 97, 98, 1.0),
    ]
    df = _bars(rows)
    labeled = compute_triple_barrier_labels(df, TripleBarrierConfig(horizon_bars=2, tp_atr_multiple=2.0, sl_atr_multiple=1.0, min_risk_reward=1.0))
    row0 = labeled[labeled.index == 0]
    assert row0.iloc[0]["label"] == SHORT_LABEL


def test_no_trade_when_neither_side_resolves_favorably_within_horizon():
    rows = [
        (100, 101, 99, 100, 1.0),
        (100, 101, 99, 100, 1.0),   # entry bar
        (100, 100.2, 99.8, 100, 1.0),  # flat, no barrier touched
        (100, 100.2, 99.8, 100, 1.0),
    ]
    df = _bars(rows)
    labeled = compute_triple_barrier_labels(df, TripleBarrierConfig(horizon_bars=2, tp_atr_multiple=2.0, sl_atr_multiple=1.0, min_risk_reward=1.0))
    row0 = labeled[labeled.index == 0]
    assert row0.iloc[0]["label"] == NO_TRADE_LABEL


def test_stop_wins_tie_when_both_barriers_touched_same_bar():
    """If a single bar's range would satisfy both TP and SL for the SAME
    side, the stop resolves first -- same convention as the backtester."""
    rows = [
        (100, 101, 99, 100, 1.0),
        (100, 101, 99, 100, 1.0),   # entry bar: long TP=102, SL=99
        (100, 200, 50, 100, 1.0),   # touches both long TP and SL in one bar
        (100, 101, 99, 100, 1.0),
    ]
    df = _bars(rows)
    labeled = compute_triple_barrier_labels(df, TripleBarrierConfig(horizon_bars=2, tp_atr_multiple=2.0, sl_atr_multiple=1.0, min_risk_reward=1.0))
    row0 = labeled[labeled.index == 0]
    # long stopped out; short (SL=101, TP=98) -- high=200 blows through short SL too -> short also stopped
    assert row0.iloc[0]["label"] == NO_TRADE_LABEL


def test_insufficient_forward_data_rows_are_dropped():
    df = _bars([(100, 101, 99, 100, 1.0)] * 5)
    labeled = compute_triple_barrier_labels(df, TripleBarrierConfig(horizon_bars=10))
    assert len(labeled) == 0, "no row has enough forward bars for a 10-bar horizon on a 5-bar dataset"


def test_invalid_or_missing_atr_produces_no_trade_not_a_crash():
    rows = [
        (100, 101, 99, 100, np.nan),
        (100, 101, 99, 100, np.nan),
        (100, 103, 99, 102, 1.0),
        (102, 103, 101, 102, 1.0),
    ]
    df = _bars(rows)
    labeled = compute_triple_barrier_labels(df, TripleBarrierConfig(horizon_bars=2))
    row0 = labeled[labeled.index == 0]
    assert row0.iloc[0]["label"] == NO_TRADE_LABEL


def test_risk_reward_floor_forces_no_trade_when_barriers_too_tight():
    rows = [(100, 101, 99, 100, 1.0)] * 6
    df = _bars(rows)
    # tp_multiple/sl_multiple = 1.0/1.0 = 1.0 R:R, below the 1.5 floor
    labeled = compute_triple_barrier_labels(df, TripleBarrierConfig(horizon_bars=2, tp_atr_multiple=1.0, sl_atr_multiple=1.0, min_risk_reward=1.5))
    assert (labeled["label"] == NO_TRADE_LABEL).all()


def test_label_end_idx_never_exceeds_available_data():
    df = _bars([(100, 101, 99, 100, 1.0)] * 20)
    labeled = compute_triple_barrier_labels(df, TripleBarrierConfig(horizon_bars=5))
    assert (labeled["label_end_idx"] < len(df)).all()


def test_label_only_uses_the_atr_at_time_t_not_a_future_atr():
    """Two datasets identical except for ATR values strictly AFTER bar 0
    must produce the IDENTICAL label at bar 0 -- the barrier width is
    fixed at T, not recomputed from later ATR readings."""
    base = [
        (100, 101, 99, 100, 1.0),
        (100, 101, 99, 100, 1.0),
        (100, 103, 99.5, 102, 1.0),
        (102, 103, 101, 102, 1.0),
    ]
    df_a = _bars(base)
    df_b = _bars(base)
    df_b.loc[2:, "atr_14"] = 999.0  # blow up ATR for every bar AFTER the signal bar

    config = TripleBarrierConfig(horizon_bars=2, tp_atr_multiple=2.0, sl_atr_multiple=1.0, min_risk_reward=1.0)
    label_a = compute_triple_barrier_labels(df_a, config)
    label_b = compute_triple_barrier_labels(df_b, config)
    assert label_a[label_a.index == 0].iloc[0]["label"] == label_b[label_b.index == 0].iloc[0]["label"]


def test_label_distribution_sums_to_100():
    rng = np.random.default_rng(3)
    n = 300
    closes = 100 + np.cumsum(rng.standard_normal(n) * 0.5)
    rows = []
    for i in range(n):
        c = closes[i]
        rows.append((c, c + abs(rng.standard_normal()), c - abs(rng.standard_normal()), c, 1.0 + abs(rng.standard_normal()) * 0.2))
    df = _bars(rows)
    labeled = compute_triple_barrier_labels(df, TripleBarrierConfig(horizon_bars=6))
    dist = label_distribution(labeled)
    assert abs(sum(dist.values()) - 100.0) < 0.05
    assert all(v >= 0 for v in dist.values())
