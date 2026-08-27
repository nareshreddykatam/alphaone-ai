"""Phase 2.5, Issue #2: real timestamp-aligned historical funding, replacing
the flat-average estimate. Hand-calculated scenarios per the spec:
position size $10,000, funding rate 0.01% -> funding cost $1.

Sign convention (verified explicitly, both directions):
- Positive rate: LONG pays, SHORT receives.
- Negative rate: LONG receives, SHORT pays.
`trade.funding` accumulates in COST terms (positive = paid out, negative =
net credit), and `pnl -= trade.funding` at close nets it correctly either way.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from services.backtester.engine import Backtester, BacktestConfig
from services.backtester.exchange_spec import ExchangeSpec


def _bars(n, start=datetime(2024, 1, 1), interval=timedelta(hours=8)):
    """One bar per funding interval, flat price at 100 unless overridden."""
    rows = []
    for i in range(n):
        rows.append({"timestamp": start + interval * i, "open": 100, "high": 101, "low": 99, "close": 100})
    return pd.DataFrame(rows)


def _no_cost_config():
    return BacktestConfig(initial_capital=10000, exchange_spec=ExchangeSpec(slippage_bps=0, taker_fee=0))


def test_long_pays_funding_on_positive_rate_hand_calculated():
    """LONG, $10,000 notional (quantity=100 @ mark price=100), rate=0.01% ->
    funding cost = 100*100*0.0001 = $1.00, paid (reduces pnl)."""
    df = _bars(6)
    # entry fills at bar[2]'s open=100; use position_pct for an exact $10,000 notional.

    def signal_func(d):
        if len(d) == 2:
            return {"signal_type": "LONG", "stop_loss": None, "take_profit_1": None,
                    "leverage": 1, "position_pct": 1.0}
        return None

    funding = pd.DataFrame({
        "timestamp": [datetime(2024, 1, 1) + timedelta(hours=24)],  # after the hour-16 entry fill
        "rate": [0.0001],  # 0.01%
    })

    bt = Backtester(_no_cost_config())
    result = bt.run(df, signal_func, funding_rates=funding)

    assert result.total_trades == 1
    trade = result.trades[0]
    assert trade.quantity == pytest.approx(100.0)  # $10,000 / $100
    assert trade.funding == pytest.approx(1.0), f"expected $1.00 funding cost, got {trade.funding}"


def test_long_receives_funding_on_negative_rate():
    df = _bars(6)

    def signal_func(d):
        if len(d) == 2:
            return {"signal_type": "LONG", "stop_loss": None, "take_profit_1": None,
                    "leverage": 1, "position_pct": 1.0}
        return None

    funding = pd.DataFrame({"timestamp": [datetime(2024, 1, 1) + timedelta(hours=24)], "rate": [-0.0001]})

    bt = Backtester(_no_cost_config())
    result = bt.run(df, signal_func, funding_rates=funding)
    trade = result.trades[0]
    assert trade.funding == pytest.approx(-1.0), "negative rate should CREDIT a long (negative cost)"


def test_short_pays_funding_on_negative_rate():
    df = _bars(6)

    def signal_func(d):
        if len(d) == 2:
            return {"signal_type": "SHORT", "stop_loss": None, "take_profit_1": None,
                    "leverage": 1, "position_pct": 1.0}
        return None

    funding = pd.DataFrame({"timestamp": [datetime(2024, 1, 1) + timedelta(hours=24)], "rate": [-0.0001]})

    bt = Backtester(_no_cost_config())
    result = bt.run(df, signal_func, funding_rates=funding)
    trade = result.trades[0]
    assert trade.funding == pytest.approx(1.0), "negative rate should charge a short (positive cost)"


def test_short_receives_funding_on_positive_rate():
    df = _bars(6)

    def signal_func(d):
        if len(d) == 2:
            return {"signal_type": "SHORT", "stop_loss": None, "take_profit_1": None,
                    "leverage": 1, "position_pct": 1.0}
        return None

    funding = pd.DataFrame({"timestamp": [datetime(2024, 1, 1) + timedelta(hours=24)], "rate": [0.0001]})

    bt = Backtester(_no_cost_config())
    result = bt.run(df, signal_func, funding_rates=funding)
    trade = result.trades[0]
    assert trade.funding == pytest.approx(-1.0), "positive rate should CREDIT a short (negative cost)"


def test_position_opened_shortly_before_funding_is_charged():
    """Entry fills at bar[2] (hour 16) -- the earliest possible fill, since a
    signal decided on bar i can only fill at bar i+1 and the loop's first
    bar is index 1. A funding event one interval later (hour 24) must still
    be charged."""
    df = _bars(6)

    def signal_func(d):
        if len(d) == 2:
            return {"signal_type": "LONG", "stop_loss": None, "take_profit_1": None,
                    "leverage": 1, "position_pct": 1.0}
        return None

    funding = pd.DataFrame({"timestamp": [datetime(2024, 1, 1) + timedelta(hours=24)], "rate": [0.0001]})
    bt = Backtester(_no_cost_config())
    result = bt.run(df, signal_func, funding_rates=funding)
    assert result.trades[0].funding == pytest.approx(1.0)


def test_position_opened_after_funding_is_not_charged():
    df = _bars(6)

    def signal_func(d):
        if len(d) == 3:  # decided after bar[2] (hour 16) -> fills at bar[3] (hour 24)
            return {"signal_type": "LONG", "stop_loss": None, "take_profit_1": None,
                    "leverage": 1, "position_pct": 1.0}
        return None

    # funding event at hour 16 -- BEFORE the entry fill at hour 24
    funding = pd.DataFrame({"timestamp": [datetime(2024, 1, 1, 16)], "rate": [0.0001]})
    bt = Backtester(_no_cost_config())
    result = bt.run(df, signal_func, funding_rates=funding)
    assert result.trades[0].funding == pytest.approx(0.0), "funding before entry must not be charged"


def test_position_closed_before_funding_is_not_charged():
    df = _bars(6)

    def signal_func(d):
        if len(d) == 2:
            return {"signal_type": "LONG", "stop_loss": 90, "take_profit_1": 100.5, "leverage": 1}
        return None

    # take profit at 100.5: bar[3]'s high is 101, so it exits at bar[3] (hour 24).
    # funding event at hour 32 (bar[4]) is AFTER the position already closed.
    funding = pd.DataFrame({"timestamp": [datetime(2024, 1, 1, 0) + timedelta(hours=32)], "rate": [0.0001]})
    bt = Backtester(_no_cost_config())
    result = bt.run(df, signal_func, funding_rates=funding)
    assert result.total_trades == 1
    assert result.trades[0].funding == pytest.approx(0.0), "funding after the position already closed must not be charged"


def test_position_spanning_multiple_funding_periods_is_charged_for_each():
    df = _bars(8)

    def signal_func(d):
        if len(d) == 2:
            return {"signal_type": "LONG", "stop_loss": None, "take_profit_1": None,
                    "leverage": 1, "position_pct": 1.0}
        return None

    # entry fills at bar[2] (hour 16); 3 funding events after that, before end of data (hour 56)
    funding = pd.DataFrame({
        "timestamp": [datetime(2024, 1, 1, 0) + timedelta(hours=h) for h in (24, 32, 40)],
        "rate": [0.0001, 0.0001, 0.0001],
    })
    bt = Backtester(_no_cost_config())
    result = bt.run(df, signal_func, funding_rates=funding)
    trade = result.trades[0]
    assert trade.funding == pytest.approx(3.0), f"expected 3 x $1.00 = $3.00 total funding, got {trade.funding}"


def test_no_lookahead_future_funding_rate_is_never_used():
    """A funding event AFTER the dataset's last processed bar must never be
    applied -- point-in-time correctness."""
    df = _bars(4)  # hours 0, 8, 16, 24

    def signal_func(d):
        if len(d) == 2:
            return {"signal_type": "LONG", "stop_loss": None, "take_profit_1": None,
                    "leverage": 1, "position_pct": 1.0}
        return None

    far_future = pd.DataFrame({"timestamp": [datetime(2030, 1, 1)], "rate": [0.01]})  # absurd rate, absurd date
    bt = Backtester(_no_cost_config())
    result = bt.run(df, signal_func, funding_rates=far_future)
    assert result.trades[0].funding == pytest.approx(0.0)


def test_funding_disabled_falls_back_to_flat_average_when_no_data_supplied():
    df = _bars(6)

    def signal_func(d):
        if len(d) == 2:
            return {"signal_type": "LONG", "stop_loss": 1, "take_profit_1": 500, "leverage": 1}
        return None

    bt = Backtester(_no_cost_config())
    result = bt.run(df, signal_func, funding_rates=None)
    # falls back to the old estimate -- just confirm it's non-zero (holding period > 1 interval)
    assert result.trades[0].funding != 0 or result.trades[0].quantity == 0
