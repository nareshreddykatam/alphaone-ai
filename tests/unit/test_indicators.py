import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from services.feature_engine.indicators import (
    compute_technical_indicators, ema, sma, rsi, macd, atr, adx, bollinger_bands, vwap
)


@pytest.fixture
def sample_ohlcv_df():
    np.random.seed(42)
    n = 200
    dates = [datetime(2024, 1, 1) + timedelta(hours=i) for i in range(n)]
    base_price = 42000

    returns = np.random.normal(0.0001, 0.01, n)
    prices = base_price * np.cumprod(1 + returns)

    df = pd.DataFrame({
        "timestamp": dates,
        "open": prices * (1 + np.random.uniform(-0.002, 0.002, n)),
        "high": prices * (1 + np.random.uniform(0, 0.01, n)),
        "low": prices * (1 - np.random.uniform(0, 0.01, n)),
        "close": prices,
        "volume": np.random.uniform(100, 1000, n),
        "symbol": "BTC/USDT",
        "timeframe": "1h",
    })
    return df


def test_ema():
    series = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=float)
    result = ema(series, 3)
    assert len(result) == 10
    assert not result.isna().all()


def test_sma():
    series = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=float)
    result = sma(series, 3)
    assert len(result) == 10
    assert result.iloc[2] == pytest.approx(2.0)


def test_rsi():
    np.random.seed(42)
    series = pd.Series(np.cumsum(np.random.randn(100)) + 100)
    result = rsi(series, 14)
    assert len(result) == 100
    valid = result.dropna()
    assert (valid >= 0).all()
    assert (valid <= 100).all()


def test_macd():
    np.random.seed(42)
    series = pd.Series(np.cumsum(np.random.randn(100)) + 100)
    macd_line, signal_line, histogram = macd(series)
    assert len(macd_line) == 100
    assert not macd_line.isna().all()


def test_atr(sample_ohlcv_df):
    result = atr(sample_ohlcv_df["high"], sample_ohlcv_df["low"], sample_ohlcv_df["close"])
    assert len(result) == len(sample_ohlcv_df)
    valid = result.dropna()
    assert (valid >= 0).all()


def test_bollinger_bands():
    np.random.seed(42)
    series = pd.Series(np.cumsum(np.random.randn(100)) + 100)
    upper, mid, lower = bollinger_bands(series)
    assert len(upper) == 100
    assert not upper.isna().all()


def test_compute_technical_indicators(sample_ohlcv_df):
    result = compute_technical_indicators(sample_ohlcv_df)
    expected_cols = ["ema_9", "ema_20", "ema_50", "ema_100", "ema_200",
                     "rsi_14", "macd", "atr_14", "bb_upper", "vwap"]
    for col in expected_cols:
        assert col in result.columns, f"Missing column: {col}"

    assert len(result) == len(sample_ohlcv_df)
    assert not result["ema_9"].isna().all()
