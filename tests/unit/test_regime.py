from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from services.feature_engine.engine import FeatureEngine
from services.signal_engine.regime import MarketRegimeDetector, detect_regime_series


def _feature_df(n=250, trend=0.0, seed=1):
    rng = np.random.default_rng(seed)
    t0 = datetime(2024, 1, 1)
    close = 40000 + np.cumsum(rng.standard_normal(n) * 30 + trend)
    df = pd.DataFrame({
        "timestamp": [t0 + timedelta(hours=i) for i in range(n)],
        "open": close, "high": close + 15, "low": close - 15, "close": close,
        "volume": rng.uniform(10, 100, n),
    })
    return FeatureEngine().compute_features(df)


def test_too_short_dataframe_is_uncertain():
    df = _feature_df(n=10)
    detector = MarketRegimeDetector()
    assert detector.detect(df) == "UNCERTAIN"


def test_empty_dataframe_is_uncertain():
    detector = MarketRegimeDetector()
    assert detector.detect(pd.DataFrame()) == "UNCERTAIN"


def test_detect_returns_a_known_regime_label():
    df = _feature_df(n=250, trend=15)
    detector = MarketRegimeDetector()
    regime = detector.detect(df)
    assert regime in detector.REGIMES.values()


def test_detect_is_deterministic_for_the_same_input():
    df = _feature_df(n=250, trend=5)
    detector = MarketRegimeDetector()
    assert detector.detect(df) == detector.detect(df.copy())


def test_vectorized_regime_series_matches_scalar_detector_exactly():
    """detect_regime_series must agree with calling detector.detect() at
    every row -- it exists purely as a performance optimization for
    regime-bucketed analysis over large datasets, not a different
    definition of regime."""
    df = _feature_df(n=400, trend=8, seed=3)
    detector = MarketRegimeDetector()
    vectorized = detect_regime_series(df)

    sample_indices = [49, 50, 75, 100, 150, 199, 250, 300, 399]
    mismatches = []
    for i in sample_indices:
        scalar_regime = detector.detect(df.iloc[:i + 1])
        vec_regime = vectorized.iloc[i]
        if scalar_regime != vec_regime:
            mismatches.append((i, scalar_regime, vec_regime))

    assert not mismatches, f"vectorized regime disagrees with scalar detector at rows (idx, scalar, vectorized): {mismatches}"


def test_vectorized_regime_series_is_uncertain_before_minimum_history():
    df = _feature_df(n=100, seed=4)
    vectorized = detect_regime_series(df)
    assert (vectorized.iloc[:49] == "UNCERTAIN").all()
