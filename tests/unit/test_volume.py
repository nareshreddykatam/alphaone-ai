import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from services.feature_engine.volume import compute_volume_features


@pytest.fixture
def sample_df():
    np.random.seed(42)
    n = 100
    dates = [datetime(2024, 1, 1) + timedelta(hours=i) for i in range(n)]
    prices = 42000 + np.cumsum(np.random.randn(n) * 100)

    return pd.DataFrame({
        "timestamp": dates,
        "open": prices,
        "high": prices + abs(np.random.randn(n) * 100),
        "low": prices - abs(np.random.randn(n) * 100),
        "close": prices,
        "volume": np.random.uniform(100, 1000, n),
        "symbol": "BTC/USDT",
        "timeframe": "1h",
    })


def test_volume_features_columns(sample_df):
    result = compute_volume_features(sample_df)
    expected = ["relative_volume", "volume_change", "volume_spike", "volume_dry"]
    for col in expected:
        assert col in result.columns, f"Missing: {col}"


def test_volume_spike_values(sample_df):
    result = compute_volume_features(sample_df)
    assert result["volume_spike"].isin([0, 1]).all()
    assert result["volume_dry"].isin([0, 1]).all()


def test_obv(sample_df):
    result = compute_volume_features(sample_df)
    assert "obv" in result.columns
    assert not result["obv"].isna().all()
