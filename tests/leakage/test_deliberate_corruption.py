"""Proves the truncation-invariance methodology used in
test_no_lookahead_features.py is not vacuous -- i.e. that it actually FAILS
when fed a feature that genuinely looks ahead, rather than passing no
matter what.

The "leaky" functions below deliberately reproduce the shape of the real
bug found in the Phase 1 audit (a centered rolling window in the original
services/feature_engine/structure.py:_detect_swings, and a naive
`shift(-1)` label-as-feature mistake) so this test also documents exactly
what kind of code pattern the detector is designed to catch.
"""
import numpy as np
import pandas as pd
import pytest

from tests.leakage.conftest import make_ohlcv
from tests.leakage.test_no_lookahead_features import _assert_prefix_identical, TRUNCATE_AT


def _leaky_centered_window_feature(df: pd.DataFrame, lookback: int = 5) -> pd.DataFrame:
    """Reproduces the pre-fix structure.py bug: bar i's value depends on
    bars up to `i + lookback`, which do not exist yet at time i."""
    df = df.copy()
    highs = df["high"].values
    n = len(df)
    out = np.zeros(n)
    for i in range(lookback, n - lookback):
        out[i] = float(highs[i] == max(highs[i - lookback:i + lookback + 1]))
    df["leaky_swing_flag"] = out
    return df


def _leaky_future_shift_feature(df: pd.DataFrame) -> pd.DataFrame:
    """A feature that's actually tomorrow's return, mislabeled as a feature."""
    df = df.copy()
    df["leaky_future_return"] = df["close"].shift(-1) / df["close"] - 1
    return df


def _make_spike_dataset(n: int = 40, spike_at: int = 27, spike_value: float = 999.0) -> pd.DataFrame:
    """A dataset engineered so a centered-window computation at `spike_at`
    can ONLY be classified correctly if it can see the spike -- used to
    guarantee (not just statistically hope for) a detectable mismatch,
    unlike relying on a random dataset where a boundary row might coincide
    by chance between the leaky and non-leaky computations.
    """
    base = make_ohlcv(n)
    base = base.copy()
    base["high"] = 10.0
    base.loc[spike_at, "high"] = spike_value
    return base


def test_centered_window_leak_is_detected():
    truncate_at = 30  # spike sits at index 27, inside [truncate_at - lookback, truncate_at - 1]
    df = _make_spike_dataset(n=40, spike_at=27)

    full = _leaky_centered_window_feature(df.copy(), lookback=5)
    truncated = _leaky_centered_window_feature(df.iloc[:truncate_at].copy(), lookback=5)

    # Sanity: confirm the engineered mismatch actually exists before asserting
    # the detector catches it (otherwise this test would pass vacuously).
    assert full["leaky_swing_flag"].iloc[27] == 1
    assert truncated["leaky_swing_flag"].iloc[27] == 0

    with pytest.raises(AssertionError, match="look-ahead bias detected"):
        _assert_prefix_identical(full, truncated, "deliberately leaky centered-window feature", truncate_at=truncate_at)


def test_future_shift_leak_is_detected():
    df = make_ohlcv(300)
    full = _leaky_future_shift_feature(df.copy())
    truncated = _leaky_future_shift_feature(df.iloc[:TRUNCATE_AT].copy())

    with pytest.raises(AssertionError, match="look-ahead bias detected"):
        _assert_prefix_identical(full, truncated, "deliberately leaky future-shift feature")


def test_corrupted_dataset_with_injected_future_value_is_caught():
    """Inject a known future OHLC value into an earlier row and confirm a
    feature computed straight off `close` (no leakage-safe design at all)
    is flagged by the truncation-invariance check -- i.e. the detector
    reacts to corrupted/leaked data, not just to a specific bug shape.
    """
    df = make_ohlcv(300)
    corrupted = df.copy()
    injection_row = 100
    future_row = 110
    corrupted.loc[injection_row, "close"] = df.loc[future_row, "close"]

    def naive_feature(d: pd.DataFrame) -> pd.DataFrame:
        d = d.copy()
        # A feature "as of" each row that a corrupted future-derived close
        # would silently distort for any row after the injection point that
        # depends on a rolling window covering it.
        d["rolling_mean_5"] = d["close"].rolling(5).mean()
        return d

    full = naive_feature(corrupted.copy())
    # Truncate BEFORE the injected corruption is overwritten in a resumed/streamed
    # scenario: recompute on the ORIGINAL (uncorrupted) prefix to simulate what the
    # feature pipeline would have produced before the corrupted row was written.
    truncated = naive_feature(df.iloc[:TRUNCATE_AT].copy())

    with pytest.raises(AssertionError, match="look-ahead bias detected"):
        _assert_prefix_identical(full, truncated, "corrupted dataset with injected future value")
