import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from services.feature_engine.indicators import (
    compute_technical_indicators, ema, sma, rsi, macd, atr, adx, bollinger_bands, vwap,
    wma, hma, kama,
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


def test_wma():
    series = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=float)
    result = wma(series, 3)
    assert len(result) == 10
    # WMA(3) of [1,2,3] with weights [1,2,3] = (1*1+2*2+3*3)/6 = 14/6
    assert result.iloc[2] == pytest.approx(14 / 6)
    assert result.iloc[:2].isna().all()


def test_wma_causal_no_lookahead():
    """Appending future rows must never change an already-computed WMA
    value at an earlier index -- a rolling().apply(raw=True) window can
    only ever see its own trailing values."""
    np.random.seed(11)
    series = pd.Series(np.cumsum(np.random.randn(60)) + 100)
    full = wma(series, 10)
    truncated = wma(series.iloc[:40], 10)
    pd.testing.assert_series_equal(full.iloc[:40], truncated, check_names=False)


def test_hma_reduces_lag_vs_sma():
    """HMA is specifically designed to track a sharp trend change faster
    than an equal-period SMA -- verify it actually does on a clean
    trend-reversal series, not just that it runs without error."""
    np.random.seed(5)
    up = np.linspace(100, 200, 60)
    down = np.linspace(200, 100, 60)
    series = pd.Series(np.concatenate([up, down]))
    period = 20
    h = hma(series, period)
    s = sma(series, period)
    # At the reversal point, HMA should have moved further back toward the
    # new (falling) price than the slower SMA has.
    idx = 70
    assert abs(h.iloc[idx] - series.iloc[idx]) < abs(s.iloc[idx] - series.iloc[idx])


def test_hma_causal_no_lookahead():
    np.random.seed(12)
    series = pd.Series(np.cumsum(np.random.randn(80)) + 100)
    full = hma(series, 20)
    truncated = hma(series.iloc[:50], 20)
    pd.testing.assert_series_equal(full.iloc[:50], truncated, check_names=False)


def test_kama_tracks_close_in_clean_trend():
    """A perfectly clean, monotonic trend has an efficiency ratio of ~1 --
    KAMA should track price very closely (fast smoothing constant)."""
    series = pd.Series(np.linspace(100, 200, 60))
    result = kama(series, er_period=10, fast=2, slow=30)
    valid = result.dropna()
    assert len(valid) > 0
    assert (abs(valid - series.loc[valid.index]) < 5).all()


def test_kama_causal_no_lookahead():
    """The recursive step at row i only reads row i-1's own KAMA value and
    row i's current inputs -- appending future rows must never change an
    already-computed value at an earlier index."""
    np.random.seed(13)
    series = pd.Series(np.cumsum(np.random.randn(80)) + 100)
    full = kama(series, 10, 2, 30)
    truncated = kama(series.iloc[:50], 10, 2, 30)
    pd.testing.assert_series_equal(full.iloc[:50], truncated, check_names=False)


def test_kama_handles_zero_volatility_without_crashing():
    """A flat (zero-volatility) series makes the efficiency-ratio
    denominator zero -- must not raise or produce inf, just fall back
    cleanly (handled via .replace(0, np.nan).fillna(0))."""
    series = pd.Series([100.0] * 30)
    result = kama(series, er_period=10, fast=2, slow=30)
    assert not np.isinf(result.dropna()).any()


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
