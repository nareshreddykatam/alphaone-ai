import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from services.backtester.engine import Backtester, BacktestConfig


@pytest.fixture
def sample_df():
    np.random.seed(42)
    n = 200
    dates = [datetime(2024, 1, 1) + timedelta(hours=i) for i in range(n)]
    prices = 42000 + np.cumsum(np.random.randn(n) * 200)

    return pd.DataFrame({
        "timestamp": dates,
        "open": prices + np.random.randn(n) * 50,
        "high": prices + abs(np.random.randn(n) * 200),
        "low": prices - abs(np.random.randn(n) * 200),
        "close": prices,
        "volume": np.random.uniform(100, 1000, n),
        "symbol": "BTC/USDT",
        "timeframe": "1h",
    })


def test_backtester_returns_result(sample_df):
    config = BacktestConfig(initial_capital=10000)
    bt = Backtester(config)

    def no_signal(df):
        return None

    result = bt.run(sample_df, no_signal)
    assert result is not None
    assert result.initial_capital == 10000
    assert result.total_trades == 0
    assert len(result.equity_curve) > 0


def test_backtester_with_signals(sample_df):
    config = BacktestConfig(initial_capital=10000)
    bt = Backtester(config)

    call_count = [0]

    def simple_signal(df):
        call_count[0] += 1
        if call_count[0] == 10:
            return {
                "signal_type": "LONG",
                "stop_loss": df.iloc[-1]["close"] * 0.98,
                "take_profit_1": df.iloc[-1]["close"] * 1.04,
                "leverage": 1,
            }
        return None

    result = bt.run(sample_df, simple_signal)
    assert result is not None


def test_backtest_result_fields(sample_df):
    config = BacktestConfig(initial_capital=10000)
    bt = Backtester(config)

    result = bt.run(sample_df, lambda df: None)

    assert hasattr(result, "total_pnl")
    assert hasattr(result, "win_rate")
    assert hasattr(result, "sharpe_ratio")
    assert hasattr(result, "max_drawdown")
    assert hasattr(result, "profit_factor")
    assert hasattr(result, "equity_curve")
    assert result.equity_curve[0]["equity"] == 10000
