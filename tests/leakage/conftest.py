from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest


def make_ohlcv(n: int = 300, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t0 = datetime(2024, 1, 1)
    timestamps = [t0 + timedelta(hours=i) for i in range(n)]
    close = 40000 + np.cumsum(rng.standard_normal(n) * 100)
    high = close + np.abs(rng.standard_normal(n) * 150)
    low = close - np.abs(rng.standard_normal(n) * 150)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    volume = rng.uniform(100, 1000, n)
    return pd.DataFrame({
        "timestamp": timestamps, "open": open_, "high": high, "low": low,
        "close": close, "volume": volume, "symbol": "BTC/USDT", "timeframe": "1h",
    })


def make_funding_rates(df: pd.DataFrame, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = df["timestamp"].iloc[::8].reset_index(drop=True)
    return pd.DataFrame({"timestamp": ts, "rate": rng.uniform(-0.0005, 0.0005, len(ts))})


def make_open_interest(df: pd.DataFrame, seed: int = 2) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = df["timestamp"].iloc[::3].reset_index(drop=True)
    return pd.DataFrame({"timestamp": ts, "value": rng.uniform(1e5, 5e5, len(ts))})


def make_liquidations(df: pd.DataFrame, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = df["timestamp"].iloc[::5].reset_index(drop=True)
    return pd.DataFrame({
        "timestamp": ts,
        "side": rng.choice(["long", "short"], len(ts)),
        "quantity": rng.uniform(0, 10, len(ts)),
    })


@pytest.fixture
def ohlcv_df():
    return make_ohlcv()
