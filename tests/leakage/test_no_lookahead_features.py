"""General look-ahead-bias detector: recomputing any feature on a truncated
prefix of the data must give IDENTICAL values to computing it on the full
dataset, for every row that exists in both. If a feature at row N depends
on rows after N, appending more history after N changes nothing about row
N's inputs -- so any difference proves a look-ahead bug.

This is the generic test that would have caught the pre-fix centered-window
swing-detection bug (services/feature_engine/structure.py) before it ever
reached a signal or a trained model.
"""
import numpy as np
import pandas as pd
import pytest

from services.feature_engine.engine import FeatureEngine
from services.feature_engine.structure import detect_market_structure
from services.feature_engine.indicators import compute_technical_indicators
from services.feature_engine.volume import compute_volume_features
from services.feature_engine.volatility import compute_volatility_features
from services.feature_engine.derivatives import compute_derivatives_features

from tests.leakage.conftest import make_ohlcv, make_funding_rates, make_open_interest, make_liquidations

TRUNCATE_AT = 150
NON_NUMERIC_COLS = {"timestamp", "symbol", "timeframe"}


def _assert_prefix_identical(full: pd.DataFrame, truncated: pd.DataFrame, label: str, truncate_at: int = TRUNCATE_AT):
    common_cols = [c for c in truncated.columns if c in full.columns and c not in NON_NUMERIC_COLS]
    assert common_cols, f"{label}: no comparable columns produced"

    mismatches = []
    for col in common_cols:
        a = full[col].iloc[:truncate_at].to_numpy(dtype=float, na_value=np.nan)
        b = truncated[col].to_numpy(dtype=float, na_value=np.nan)
        if not np.allclose(a, b, equal_nan=True, rtol=1e-9, atol=1e-9):
            bad_rows = np.where(~np.isclose(a, b, equal_nan=True, rtol=1e-9, atol=1e-9))[0]
            mismatches.append((col, bad_rows[:5].tolist()))

    assert not mismatches, (
        f"{label}: look-ahead bias detected -- these columns differ between the "
        f"full-history computation and a truncated recomputation at the same rows "
        f"(column, sample mismatched row indices): {mismatches}"
    )


def test_technical_indicators_no_lookahead():
    df = make_ohlcv(300)
    full = compute_technical_indicators(df.copy())
    truncated = compute_technical_indicators(df.iloc[:TRUNCATE_AT].copy())
    _assert_prefix_identical(full, truncated, "technical indicators")


def test_market_structure_no_lookahead():
    df = make_ohlcv(300)
    df = compute_technical_indicators(df)
    full = detect_market_structure(df.copy())
    truncated = detect_market_structure(df.iloc[:TRUNCATE_AT].copy())
    _assert_prefix_identical(full, truncated, "market structure")


def test_volume_features_no_lookahead():
    df = make_ohlcv(300)
    full = compute_volume_features(df.copy())
    truncated = compute_volume_features(df.iloc[:TRUNCATE_AT].copy())
    _assert_prefix_identical(full, truncated, "volume features")


def test_volatility_features_no_lookahead():
    df = make_ohlcv(300)
    full = compute_volatility_features(df.copy())
    truncated = compute_volatility_features(df.iloc[:TRUNCATE_AT].copy())
    _assert_prefix_identical(full, truncated, "volatility features")


def test_derivatives_features_no_lookahead():
    df = make_ohlcv(300)
    funding = make_funding_rates(df)
    oi = make_open_interest(df)
    liqs = make_liquidations(df)

    full = compute_derivatives_features(df.copy(), funding, oi, liqs)
    truncated_df = df.iloc[:TRUNCATE_AT].copy()
    truncated_funding = funding[funding["timestamp"] <= truncated_df["timestamp"].max()]
    truncated_oi = oi[oi["timestamp"] <= truncated_df["timestamp"].max()]
    truncated_liqs = liqs[liqs["timestamp"] <= truncated_df["timestamp"].max()]
    truncated = compute_derivatives_features(truncated_df, truncated_funding, truncated_oi, truncated_liqs)
    _assert_prefix_identical(full, truncated, "derivatives features")


def test_full_feature_engine_no_lookahead():
    df = make_ohlcv(300)
    funding = make_funding_rates(df)
    oi = make_open_interest(df)
    liqs = make_liquidations(df)

    engine = FeatureEngine()
    full = engine.compute_features(df.copy(), funding, oi, liqs)

    truncated_df = df.iloc[:TRUNCATE_AT].copy()
    cutoff = truncated_df["timestamp"].max()
    truncated = FeatureEngine().compute_features(
        truncated_df,
        funding[funding["timestamp"] <= cutoff],
        oi[oi["timestamp"] <= cutoff],
        liqs[liqs["timestamp"] <= cutoff],
    )
    _assert_prefix_identical(full, truncated, "full feature engine output")
