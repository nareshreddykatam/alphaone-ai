import numpy as np
import pandas as pd

from services.feature_engine.engine import FeatureEngine
from ml.datasets.loader import DatasetLoader
from tests.leakage.conftest import make_ohlcv


def test_label_columns_never_appear_in_feature_names():
    df = make_ohlcv(300)
    engine = FeatureEngine()
    features_df = engine.compute_features(df)
    feature_names = engine.feature_names

    loader = DatasetLoader(db=None)
    labeled = loader.create_labels(features_df, forward_periods=12, threshold=0.005)

    assert "label" not in feature_names
    assert "future_return" not in feature_names
    # sanity: the label columns really were added on top, not present before
    assert "label" in labeled.columns
    assert "future_return" in labeled.columns


def test_no_feature_is_a_perfect_proxy_for_the_label():
    """A feature that is (near-)perfectly correlated with the forward label
    is a strong signal that it was accidentally computed using future data.
    This won't catch subtle partial leakage, but it catches the blatant case
    (e.g. a feature that IS the label under another name)."""
    df = make_ohlcv(500)
    engine = FeatureEngine()
    features_df = engine.compute_features(df)

    loader = DatasetLoader(db=None)
    labeled = loader.create_labels(features_df, forward_periods=12, threshold=0.005)

    feature_cols = [c for c in engine.feature_names if c in labeled.columns]
    valid = labeled.dropna(subset=feature_cols + ["future_return"])

    suspects = []
    for col in feature_cols:
        series = valid[col]
        if series.std() == 0 or series.isna().all():
            continue
        corr = series.corr(valid["future_return"])
        if pd.notna(corr) and abs(corr) > 0.98:
            suspects.append((col, round(float(corr), 4)))

    assert not suspects, f"Feature(s) suspiciously perfectly correlated with the forward label: {suspects}"
