"""Phase 3: chronological split with purge/embargo, and the model-dispatch
plumbing in ml/evaluation/ml_pipeline.py."""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from ml.evaluation.ml_pipeline import chronological_split_with_embargo


def _labeled_df(n=1000):
    return pd.DataFrame({
        "timestamp": [datetime(2024, 1, 1) + timedelta(hours=4 * i) for i in range(n)],
        "close": np.arange(n, dtype=float),
        "label": np.random.default_rng(0).integers(-1, 2, n),
    })


def test_split_is_chronological_and_non_overlapping():
    df = _labeled_df(1000)
    train, val, test = chronological_split_with_embargo(df, train_pct=0.6, val_pct=0.2, embargo=14)

    assert train["timestamp"].max() < val["timestamp"].min()
    assert val["timestamp"].max() < test["timestamp"].min()
    assert list(train["timestamp"]) == sorted(train["timestamp"])
    assert list(val["timestamp"]) == sorted(val["timestamp"])
    assert list(test["timestamp"]) == sorted(test["timestamp"])


def test_embargo_gap_is_respected_at_both_boundaries():
    df = _labeled_df(1000)
    embargo = 14
    train, val, test = chronological_split_with_embargo(df, train_pct=0.6, val_pct=0.2, embargo=embargo)

    interval = timedelta(hours=4)
    gap_train_val = (val["timestamp"].iloc[0] - train["timestamp"].iloc[-1]) / interval
    gap_val_test = (test["timestamp"].iloc[0] - val["timestamp"].iloc[-1]) / interval

    assert gap_train_val >= embargo, f"train->val gap is {gap_train_val} bars, expected >= {embargo}"
    assert gap_val_test >= embargo, f"val->test gap is {gap_val_test} bars, expected >= {embargo}"


def test_embargo_at_least_as_large_as_label_horizon_prevents_overlap():
    """The whole point of the embargo: if a training row's label depends on
    `horizon_bars` future rows, none of those future rows may appear in
    the validation split. Embargo >= horizon guarantees this by construction."""
    df = _labeled_df(500)
    horizon_bars = 12
    embargo = horizon_bars + 2  # matches ml_pipeline.run_single_combo's own margin
    train, val, _ = chronological_split_with_embargo(df, train_pct=0.6, val_pct=0.2, embargo=embargo)

    last_train_idx = df.index[df["timestamp"] == train["timestamp"].iloc[-1]][0]
    label_dependency_end_idx = last_train_idx + horizon_bars
    first_val_idx = df.index[df["timestamp"] == val["timestamp"].iloc[0]][0]

    assert first_val_idx > label_dependency_end_idx, (
        "validation split starts before the last training label's forward-looking "
        "window ends -- purge/embargo is not actually preventing overlap"
    )


def test_split_sizes_are_reasonable_fractions_of_total():
    df = _labeled_df(1000)
    train, val, test = chronological_split_with_embargo(df, train_pct=0.6, val_pct=0.2, embargo=5)
    assert 550 <= len(train) <= 600
    assert 150 <= len(val) <= 210
    assert len(test) > 0
