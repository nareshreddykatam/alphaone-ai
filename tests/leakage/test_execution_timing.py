"""Directly targets the critical bug found in the Phase 1 audit: the
backtester used to fill an entry at the SAME candle's close that generated
the signal, which is unrealistic look-ahead execution (you cannot act on a
candle's own close before it exists). This test hand-constructs a dataset
where the signal-bar close and the next-bar open are deliberately far apart,
so any regression back to same-bar-close fills is immediately obvious.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from services.backtester.engine import Backtester, BacktestConfig
from services.backtester.exchange_spec import ExchangeSpec


def _make_df():
    rows = [
        {"timestamp": datetime(2024, 1, 1, 0), "open": 100, "high": 101, "low": 99, "close": 100},
        {"timestamp": datetime(2024, 1, 1, 1), "open": 100, "high": 102, "low": 99, "close": 101},
        {"timestamp": datetime(2024, 1, 1, 2), "open": 101, "high": 103, "low": 100, "close": 102},
        {"timestamp": datetime(2024, 1, 1, 3), "open": 110, "high": 112, "low": 109, "close": 111},
        {"timestamp": datetime(2024, 1, 1, 4), "open": 111, "high": 113, "low": 110, "close": 112},
    ]
    return pd.DataFrame(rows)


def test_long_entry_fills_at_next_bar_open_not_signal_bar_close():
    df = _make_df()
    config = BacktestConfig(initial_capital=10000, exchange_spec=ExchangeSpec(slippage_bps=0, taker_fee=0))
    bt = Backtester(config)

    fired = {"done": False}

    def signal_func(d):
        if len(d) == 3 and not fired["done"]:
            fired["done"] = True
            return {"signal_type": "LONG", "stop_loss": 90, "take_profit_1": 200, "leverage": 1}
        return None

    result = bt.run(df, signal_func)

    assert result.total_trades == 1
    trade = result.trades[0]
    signal_bar_close = 102
    next_bar_open = 110
    assert trade.entry_price == pytest.approx(next_bar_open)
    assert trade.entry_price != pytest.approx(signal_bar_close)
    assert trade.entry_time == datetime(2024, 1, 1, 3)


def test_short_entry_also_fills_at_next_bar_open():
    df = _make_df()
    config = BacktestConfig(initial_capital=10000, exchange_spec=ExchangeSpec(slippage_bps=0, taker_fee=0))
    bt = Backtester(config)

    fired = {"done": False}

    def signal_func(d):
        if len(d) == 3 and not fired["done"]:
            fired["done"] = True
            return {"signal_type": "SHORT", "stop_loss": 200, "take_profit_1": 1, "leverage": 1}
        return None

    result = bt.run(df, signal_func)
    trade = result.trades[0]
    assert trade.entry_price == pytest.approx(110)
    assert trade.entry_time == datetime(2024, 1, 1, 3)


def test_signal_on_last_bar_is_never_executed():
    """A signal decided using the LAST bar's close has no next bar to fill
    at -- it must be dropped, not silently filled at its own close."""
    df = _make_df()
    bt = Backtester(BacktestConfig(initial_capital=10000))

    def signal_func(d):
        if len(d) == len(df):
            return {"signal_type": "LONG", "stop_loss": 90, "take_profit_1": 200, "leverage": 1}
        return None

    result = bt.run(df, signal_func)
    assert result.total_trades == 0


def test_slippage_is_applied_relative_to_the_fill_price_not_the_signal_price():
    df = _make_df()
    config = BacktestConfig(initial_capital=10000, exchange_spec=ExchangeSpec(slippage_bps=100, taker_fee=0))  # 1%
    bt = Backtester(config)

    fired = {"done": False}

    def signal_func(d):
        if len(d) == 3 and not fired["done"]:
            fired["done"] = True
            return {"signal_type": "LONG", "stop_loss": 90, "take_profit_1": 200, "leverage": 1}
        return None

    result = bt.run(df, signal_func)
    trade = result.trades[0]
    expected = 110 * 1.01
    assert trade.entry_price == pytest.approx(expected)
