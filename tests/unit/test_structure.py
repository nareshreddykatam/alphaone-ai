import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from services.feature_engine.structure import detect_market_structure, _detect_swings


@pytest.fixture
def sample_df():
    np.random.seed(42)
    n = 200
    dates = [datetime(2024, 1, 1) + timedelta(hours=i) for i in range(n)]
    base = 42000
    prices = base + np.cumsum(np.random.randn(n) * 100)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices + np.random.randn(n) * 50,
        "high": prices + abs(np.random.randn(n) * 100),
        "low": prices - abs(np.random.randn(n) * 100),
        "close": prices,
        "volume": np.random.uniform(100, 1000, n),
        "symbol": "BTC/USDT",
        "timeframe": "1h",
    })
    return df


def test_detect_swings(sample_df):
    swing_highs, swing_lows = _detect_swings(sample_df, lookback=5)
    assert len(swing_highs) == len(sample_df)
    assert len(swing_lows) == len(sample_df)


def test_market_structure_columns(sample_df):
    result = detect_market_structure(sample_df)
    expected = ["higher_high", "higher_low", "lower_high", "lower_low",
                "uptrend", "downtrend", "break_of_structure",
                "near_resistance", "near_support", "consolidation"]
    for col in expected:
        assert col in result.columns, f"Missing: {col}"


def test_uptrend_or_downtrend(sample_df):
    result = detect_market_structure(sample_df)
    for i in range(len(result)):
        if result.iloc[i]["uptrend"] == 1:
            assert result.iloc[i]["downtrend"] == 0
