"""AI Trading V1, Phase 7: strategy-signal features must carry the exact
same no-lookahead guarantee as every other feature -- a signal computed at
row N must be identical whether or not more history exists after row N.
Reuses the general truncation-invariance pattern from
tests/leakage/test_no_lookahead_features.py.
"""
import numpy as np
import pandas as pd

from ml.features.strategy_features import assemble_strategy_features, STRATEGY_FEATURE_IDS
from tests.leakage.conftest import make_ohlcv

TRUNCATE_AT = 200


def test_strategy_signal_features_are_truncation_invariant():
    full_df = make_ohlcv(n=300, seed=7)
    truncated_df = full_df.iloc[:TRUNCATE_AT].reset_index(drop=True)

    full_assembled, cols = assemble_strategy_features(full_df)
    truncated_assembled, _ = assemble_strategy_features(truncated_df)

    for col in cols:
        a = full_assembled[col].iloc[:TRUNCATE_AT].to_numpy(dtype=float)
        b = truncated_assembled[col].to_numpy(dtype=float)
        assert np.allclose(a, b, equal_nan=True), f"{col} is not truncation-invariant -- possible lookahead"


def test_strategy_signal_columns_only_contain_valid_direction_values():
    df = make_ohlcv(n=300, seed=3)
    assembled, cols = assemble_strategy_features(df)
    for sid in STRATEGY_FEATURE_IDS:
        col = f"strat_{sid}"
        assert set(assembled[col].unique()).issubset({-1.0, 0.0, 1.0})


def test_aggregate_consensus_columns_are_consistent_with_individual_signals():
    df = make_ohlcv(n=300, seed=11)
    assembled, cols = assemble_strategy_features(df)
    per_strategy_cols = [f"strat_{sid}" for sid in STRATEGY_FEATURE_IDS]

    expected_net = assembled[per_strategy_cols].sum(axis=1)
    assert np.allclose(assembled["strat_net_direction"], expected_net)

    expected_long = (assembled[per_strategy_cols] == 1).sum(axis=1)
    assert np.array_equal(assembled["strat_agree_long"].to_numpy(), expected_long.to_numpy())
