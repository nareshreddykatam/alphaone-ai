"""Phase 3: the assembled multi-group, multi-timeframe feature set must
remain leak-free -- truncation-invariance extended to the new columns
(regime one-hot, trend_slope_20, atr_pct_14) and to the 1h-context merge
specifically (a 4h bar must never see a 1h reading from after its own
timestamp)."""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from ml.features.feature_groups import assemble_features, get_feature_groups
from tests.leakage.conftest import make_funding_rates, make_open_interest
from tests.leakage.test_no_lookahead_features import _assert_prefix_identical

TRUNCATE_AT = 150


def make_4h_ohlcv(n: int = 300, seed: int = 42) -> pd.DataFrame:
    """Like tests.leakage.conftest.make_ohlcv but genuinely 4h-spaced --
    needed here because the 1h context fixture below packs 4 sub-bars into
    each primary bar's own span, which would overlap into the NEXT
    primary bar's timestamp if the primary were only 1h-spaced itself."""
    rng = np.random.default_rng(seed)
    t0 = datetime(2024, 1, 1)
    timestamps = [t0 + timedelta(hours=4 * i) for i in range(n)]
    close = 40000 + np.cumsum(rng.standard_normal(n) * 100)
    high = close + np.abs(rng.standard_normal(n) * 150)
    low = close - np.abs(rng.standard_normal(n) * 150)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    volume = rng.uniform(100, 1000, n)
    return pd.DataFrame({
        "timestamp": timestamps, "open": open_, "high": high, "low": low,
        "close": close, "volume": volume, "symbol": "BTC/USDT", "timeframe": "4h",
    })


def make_1h_ohlcv(df_4h: pd.DataFrame, seed: int = 5) -> pd.DataFrame:
    """4 bars of 1h data per 4h bar, staying within [4h_open, 4h_close) time span."""
    rng = np.random.default_rng(seed)
    rows = []
    for _, row in df_4h.iterrows():
        for h in range(4):
            ts = row["timestamp"] + timedelta(hours=h)
            close = row["close"] + rng.standard_normal() * 5
            rows.append({
                "timestamp": ts, "open": close, "high": close + 2, "low": close - 2,
                "close": close, "volume": rng.uniform(10, 100),
            })
    return pd.DataFrame(rows)


def test_assembled_features_no_lookahead_across_full_group_set():
    df = make_4h_ohlcv(300)
    funding = make_funding_rates(df)
    oi = make_open_interest(df)
    df_1h = make_1h_ohlcv(df)

    full, _ = assemble_features(df.copy(), df_1h.copy(), funding, oi)
    truncated_primary = df.iloc[:TRUNCATE_AT].copy()
    cutoff = truncated_primary["timestamp"].max()
    truncated_1h = df_1h[df_1h["timestamp"] <= cutoff].copy()
    truncated_funding = funding[funding["timestamp"] <= cutoff]
    truncated_oi = oi[oi["timestamp"] <= cutoff]

    truncated, _ = assemble_features(truncated_primary, truncated_1h, truncated_funding, truncated_oi)
    _assert_prefix_identical(full, truncated, "assembled Phase 3 feature set")


def test_context_1h_never_uses_a_1h_bar_after_the_4h_bars_own_timestamp():
    df = make_4h_ohlcv(100)
    df_1h = make_1h_ohlcv(df)
    full, _ = assemble_features(df.copy(), df_1h.copy())

    for i in range(50, len(full)):
        bar_ts = full["timestamp"].iloc[i]
        eligible_1h = df_1h[df_1h["timestamp"] <= bar_ts]
        if eligible_1h.empty:
            continue
        # the ctx_1h_uptrend value must come from a 1h feature row computed
        # using ONLY 1h bars up to bar_ts -- recompute independently and compare.
        from services.feature_engine.engine import FeatureEngine
        recomputed = FeatureEngine().compute_features(eligible_1h.copy())
        expected = recomputed["uptrend"].iloc[-1]
        actual = full["ctx_1h_uptrend"].iloc[i]
        if pd.notna(expected) and pd.notna(actual):
            assert actual == expected, f"row {i}: ctx_1h_uptrend does not match the as-of-T 1h computation"


def test_regime_one_hot_columns_sum_to_at_most_one_per_row():
    df = make_4h_ohlcv(200)
    full, _ = assemble_features(df.copy())
    groups = get_feature_groups(full)
    regime_cols = groups["regime"]
    row_sums = full[regime_cols].sum(axis=1)
    assert (row_sums <= 1).all(), "each row should be classified into at most one regime"
