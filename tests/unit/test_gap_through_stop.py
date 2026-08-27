"""Phase 2.5, Issue #1: gap-through-stop must fill at the true available
price (the gapped-through open), never at the stale stop-loss level that
the market never actually offered.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from services.backtester.engine import Backtester, BacktestConfig
from services.backtester.exchange_spec import ExchangeSpec


def _bars(specs, start=datetime(2024, 1, 1)):
    rows = []
    for i, (o, h, l, c) in enumerate(specs):
        rows.append({"timestamp": start + timedelta(hours=i), "open": o, "high": h, "low": l, "close": c})
    return pd.DataFrame(rows)


def _no_cost_config():
    return BacktestConfig(initial_capital=10000, exchange_spec=ExchangeSpec(slippage_bps=0, taker_fee=0))


def test_long_gap_through_stop_exits_at_gapped_open_not_stale_stop():
    """LONG, SL=98, next candle opens at 94 -- must exit ~94, not 98."""
    rows = [
        (100, 101, 99, 100),
        (100, 101, 99, 100),   # signal fires (len(d)==2)
        (100, 101, 99, 100),   # fill bar: entry at open=100
        (94, 95, 90, 92),      # gap down -- opens at 94, well below SL=98
    ]
    df = _bars(rows)
    fired = {"done": False}

    def signal_func(d):
        if len(d) == 2 and not fired["done"]:
            fired["done"] = True
            return {"signal_type": "LONG", "stop_loss": 98, "take_profit_1": 200, "leverage": 1}
        return None

    bt = Backtester(_no_cost_config())
    result = bt.run(df, signal_func)

    assert result.total_trades == 1
    trade = result.trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == pytest.approx(94.0), (
        f"gap-through-stop filled at {trade.exit_price}, expected the gapped-through open (94), not the stale stop (98)"
    )


def test_short_gap_through_stop_exits_at_gapped_open_not_stale_stop():
    """SHORT, SL=102, next candle opens at 106 -- must exit ~106, not 102."""
    rows = [
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),   # fill bar: entry at open=100
        (106, 110, 105, 108),  # gap up -- opens at 106, well above SL=102
    ]
    df = _bars(rows)
    fired = {"done": False}

    def signal_func(d):
        if len(d) == 2 and not fired["done"]:
            fired["done"] = True
            return {"signal_type": "SHORT", "stop_loss": 102, "take_profit_1": 1, "leverage": 1}
        return None

    bt = Backtester(_no_cost_config())
    result = bt.run(df, signal_func)

    assert result.total_trades == 1
    trade = result.trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == pytest.approx(106.0), (
        f"gap-through-stop filled at {trade.exit_price}, expected the gapped-through open (106), not the stale stop (102)"
    )


def test_long_normal_stop_touch_still_fills_at_stop_price():
    """When the market does NOT gap through the stop (open stays on the
    favorable side), the stop must still fill at its own level -- the gap
    fix must not change ordinary intrabar stop behavior."""
    rows = [
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),   # fill bar: entry at open=100
        (99, 99.5, 97, 98),    # opens at 99 (above SL=98), dips intrabar to hit it
    ]
    df = _bars(rows)
    fired = {"done": False}

    def signal_func(d):
        if len(d) == 2 and not fired["done"]:
            fired["done"] = True
            return {"signal_type": "LONG", "stop_loss": 98, "take_profit_1": 200, "leverage": 1}
        return None

    bt = Backtester(_no_cost_config())
    result = bt.run(df, signal_func)
    trade = result.trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == pytest.approx(98.0)


def test_short_normal_stop_touch_still_fills_at_stop_price():
    rows = [
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),   # fill bar: entry at open=100
        (101, 102.5, 100.5, 102),  # opens at 101 (below SL=102), rises intrabar to hit it
    ]
    df = _bars(rows)
    fired = {"done": False}

    def signal_func(d):
        if len(d) == 2 and not fired["done"]:
            fired["done"] = True
            return {"signal_type": "SHORT", "stop_loss": 102, "take_profit_1": 1, "leverage": 1}
        return None

    bt = Backtester(_no_cost_config())
    result = bt.run(df, signal_func)
    trade = result.trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == pytest.approx(102.0)


def test_gap_through_stop_applies_slippage_to_the_gapped_price():
    rows = [
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (100, 101, 99, 100),
        (94, 95, 90, 92),
    ]
    df = _bars(rows)
    fired = {"done": False}

    def signal_func(d):
        if len(d) == 2 and not fired["done"]:
            fired["done"] = True
            return {"signal_type": "LONG", "stop_loss": 98, "take_profit_1": 200, "leverage": 1}
        return None

    config = BacktestConfig(initial_capital=10000, exchange_spec=ExchangeSpec(slippage_bps=100, taker_fee=0))  # 1%
    bt = Backtester(config)
    result = bt.run(df, signal_func)
    trade = result.trades[0]
    assert trade.exit_price == pytest.approx(94 * 0.99)
