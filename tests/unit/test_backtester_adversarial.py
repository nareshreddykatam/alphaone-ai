"""Adversarial backtester cases called out explicitly in the Phase 2 brief:
gaps through a stop, a single candle touching both SL and TP, extreme
volatility, an empty dataset, and risk-limit exhaustion mid-run."""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from services.backtester.engine import Backtester, BacktestConfig
from services.backtester.exchange_spec import ExchangeSpec
from services.risk_engine.engine import RiskConfig


def _bars(specs, start=datetime(2024, 1, 1)):
    rows = []
    for i, (o, h, l, c) in enumerate(specs):
        rows.append({"timestamp": start + timedelta(hours=i), "open": o, "high": h, "low": l, "close": c})
    return pd.DataFrame(rows)


def test_empty_dataset_does_not_crash():
    df = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close"])
    bt = Backtester(BacktestConfig(initial_capital=10000))
    result = bt.run(df, lambda d: None)
    assert result.total_trades == 0
    assert result.initial_capital == 10000
    assert result.final_capital == 10000


def test_single_bar_dataset_does_not_crash():
    df = _bars([(100, 101, 99, 100)])
    bt = Backtester(BacktestConfig(initial_capital=10000))
    result = bt.run(df, lambda d: {"signal_type": "LONG", "stop_loss": 90, "take_profit_1": 200, "leverage": 1})
    assert result.total_trades == 0  # no next bar to fill at


def test_gap_through_stop_still_exits_without_crashing():
    """Price gaps straight through the stop -- the bar's low is far below
    the stop level. The backtester must still register an exit (even if,
    per docs/execution_semantics.md, it uses the stop price as the fill
    reference rather than the true post-gap open)."""
    rows = [
        (100, 101, 99, 100),
        (100, 101, 99, 100),  # signal fires here
        (99, 100, 98, 99),    # fill bar (next-bar open)
        (70, 71, 60, 70),     # gap down THROUGH the stop (stop=90)
        (70, 72, 69, 71),
    ]
    df = _bars(rows)
    fired = {"done": False}

    def signal_func(d):
        if len(d) == 2 and not fired["done"]:
            fired["done"] = True
            return {"signal_type": "LONG", "stop_loss": 90, "take_profit_1": 200, "leverage": 1}
        return None

    bt = Backtester(BacktestConfig(initial_capital=10000))
    result = bt.run(df, signal_func)
    assert result.total_trades == 1
    trade = result.trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.pnl < 0


def test_simultaneous_sl_and_tp_in_same_bar_resolves_to_stop_loss():
    """A bar whose range covers both the stop and the target -- OHLC alone
    can't say which happened first, so the documented resolution is
    'assume the stop triggers first' (docs/execution_semantics.md)."""
    rows = [
        (100, 101, 99, 100),
        (100, 101, 99, 100),   # signal fires
        (100, 101, 99, 100),   # fill bar, entry at open=100
        (100, 200, 50, 100),   # touches both SL=90 and TP=150 in one bar
    ]
    df = _bars(rows)
    fired = {"done": False}

    def signal_func(d):
        if len(d) == 2 and not fired["done"]:
            fired["done"] = True
            return {"signal_type": "LONG", "stop_loss": 90, "take_profit_1": 150, "leverage": 1}
        return None

    bt = Backtester(BacktestConfig(initial_capital=10000))
    result = bt.run(df, signal_func)
    assert result.total_trades == 1
    assert result.trades[0].exit_reason == "stop_loss"


def test_extreme_volatility_single_candle_does_not_crash():
    rows = [
        (100, 101, 99, 100),
        (100, 105, 95, 100),
        (100, 10000, 1, 5000),   # 100x range candle
        (5000, 5100, 4900, 5000),
    ]
    df = _bars(rows)
    fired = {"done": False}

    def signal_func(d):
        if len(d) == 2 and not fired["done"]:
            fired["done"] = True
            return {"signal_type": "LONG", "stop_loss": 1, "take_profit_1": 20000, "leverage": 1}
        return None

    bt = Backtester(BacktestConfig(initial_capital=10000))
    result = bt.run(df, signal_func)  # should not raise
    assert result is not None


def test_signal_while_position_open_does_not_open_a_second_position():
    """A new signal firing while a position is already open must not create
    a duplicate/overlapping position -- the single-position model should
    simply ignore it for that bar."""
    rows = [(100, 101, 99, 100)] * 10
    df = _bars(rows)

    def always_signal(d):
        return {"signal_type": "LONG", "stop_loss": 1, "take_profit_1": 100000, "leverage": 1}

    bt = Backtester(BacktestConfig(initial_capital=10000, risk_config=RiskConfig(max_positions=1)))
    result = bt.run(df, always_signal)

    # Every bar re-fires the same signal, but only one position should ever
    # be open at a time -- with a target that's never hit and a stop that's
    # never hit, exactly one trade should open and ride to end-of-data.
    assert result.total_trades == 1


def test_risk_limit_exceeded_mid_run_blocks_further_trades():
    """Once the kill switch trips (max drawdown / daily loss), no further
    trades should open for the remainder of the run."""
    n = 40
    rows = []
    price = 100.0
    for i in range(n):
        # alternate sharp down moves to force repeated stop-outs
        o = price
        c = price * 0.90
        h = max(o, c) + 1
        l = min(o, c) - 1
        rows.append((o, h, l, c))
        price = c if c > 1 else 100.0  # reset if it decays too far
    df = _bars(rows)

    call_count = [0]

    def signal_func(d):
        call_count[0] += 1
        last = d.iloc[-1]
        return {
            "signal_type": "LONG",
            "stop_loss": last["close"] * 0.95,
            "take_profit_1": last["close"] * 1.5,
            "leverage": 1,
        }

    risk_config = RiskConfig(max_drawdown_pct=5.0, max_daily_loss_pct=5.0, max_positions=1, cooldown_consecutive_losses=100)
    config = BacktestConfig(initial_capital=10000, risk_config=risk_config, exchange_spec=ExchangeSpec(slippage_bps=0, taker_fee=0))
    bt = Backtester(config)
    result = bt.run(df, signal_func)

    assert bt.risk_engine.state.kill_switch is True
    # trades should have stopped well before the dataset ended
    assert result.total_trades < n
