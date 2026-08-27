import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta


@pytest.fixture
def sample_candles():
    np.random.seed(42)
    n = 500
    dates = [datetime(2024, 1, 1) + timedelta(hours=i) for i in range(n)]
    prices = 42000 + np.cumsum(np.random.randn(n) * 100)

    return pd.DataFrame({
        "timestamp": dates,
        "open": prices + np.random.randn(n) * 50,
        "high": prices + abs(np.random.randn(n) * 150),
        "low": prices - abs(np.random.randn(n) * 150),
        "close": prices,
        "volume": np.random.uniform(100, 1000, n),
        "symbol": "BTC/USDT",
        "timeframe": "1h",
    })


@pytest.fixture
def sample_funding_rates():
    np.random.seed(42)
    return pd.Series(np.random.uniform(-0.0005, 0.0005, 200))


@pytest.fixture
def sample_open_interest():
    np.random.seed(42)
    return pd.Series(np.random.uniform(100000, 500000, 200))
