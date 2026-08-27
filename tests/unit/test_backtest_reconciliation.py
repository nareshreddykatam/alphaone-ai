"""Phase 2.5, section 18: hand-computed reconciliation scenarios. Each test
independently derives the expected P&L/fees/funding by hand and asserts the
engine's actual trade and final equity match EXACTLY (to float tolerance).

Shared scenario shape: $10,000 initial capital, entry at $100, a $1,000
notional position (quantity=10, via position_pct=0.1) so the arithmetic is
easy to verify by hand, taker_fee=0.1% (a round, easy-to-check number, not
the default 0.04%), zero slippage unless a test specifically exercises it.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from services.backtester.engine import Backtester, BacktestConfig
from services.backtester.exchange_spec import ExchangeSpec

TAKER_FEE = 0.001  # 0.1%, a round number for easy hand-verification


def _bars(specs, start=datetime(2024, 1, 1), interval=timedelta(hours=1)):
    rows = []
    for i, (o, h, l, c) in enumerate(specs):
        rows.append({"timestamp": start + interval * i, "open": o, "high": h, "low": l, "close": c})
    return pd.DataFrame(rows)


def _config(slippage_bps=0.0):
    return BacktestConfig(
        initial_capital=10000,
        exchange_spec=ExchangeSpec(taker_fee=TAKER_FEE, slippage_bps=slippage_bps),
        funding_rate_avg=0.0,  # isolate fee/price P&L unless a test wants funding too
    )


def _fixed_notional_signal(side: str, stop: float, target: float):
    fired = {"done": False}

    def _signal(d):
        if len(d) == 2 and not fired["done"]:
            fired["done"] = True
            return {"signal_type": side, "stop_loss": stop, "take_profit_1": target,
                     "leverage": 1, "position_pct": 0.1}
        return None

    return _signal


def test_winning_long_reconciles_exactly():
    # entry 100 (open of bar[2]), TP=105 hit at bar[3] (high=106)
    df = _bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100), (105, 106, 104, 105)])
    bt = Backtester(_config())
    result = bt.run(df, _fixed_notional_signal("LONG", 90, 105))

    trade = result.trades[0]
    assert trade.quantity == pytest.approx(10.0)
    assert trade.entry_price == pytest.approx(100.0)
    assert trade.exit_price == pytest.approx(105.0)

    price_pnl = (105 - 100) * 10
    entry_fee = 100 * 10 * TAKER_FEE
    exit_fee = 105 * 10 * TAKER_FEE
    expected_pnl = price_pnl - entry_fee - exit_fee

    assert trade.fees == pytest.approx(entry_fee + exit_fee)
    assert trade.funding == pytest.approx(0.0)
    assert trade.pnl == pytest.approx(expected_pnl)
    assert result.final_capital == pytest.approx(10000 + expected_pnl)


def test_losing_long_reconciles_exactly():
    # entry 100, SL=95 hit normally (open=99 stays above SL, low=94 touches it)
    df = _bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100), (99, 99.5, 94, 95)])
    bt = Backtester(_config())
    result = bt.run(df, _fixed_notional_signal("LONG", 95, 200))

    trade = result.trades[0]
    assert trade.exit_price == pytest.approx(95.0)

    price_pnl = (95 - 100) * 10
    entry_fee = 100 * 10 * TAKER_FEE
    exit_fee = 95 * 10 * TAKER_FEE
    expected_pnl = price_pnl - entry_fee - exit_fee

    assert trade.pnl == pytest.approx(expected_pnl)
    assert result.final_capital == pytest.approx(10000 + expected_pnl)


def test_winning_short_reconciles_exactly():
    # entry 100 (short), TP=95 hit at bar[3] (low=94)
    df = _bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100), (95, 96, 94, 95)])
    bt = Backtester(_config())
    result = bt.run(df, _fixed_notional_signal("SHORT", 110, 95))

    trade = result.trades[0]
    assert trade.exit_price == pytest.approx(95.0)

    price_pnl = (100 - 95) * 10
    entry_fee = 100 * 10 * TAKER_FEE
    exit_fee = 95 * 10 * TAKER_FEE
    expected_pnl = price_pnl - entry_fee - exit_fee

    assert trade.pnl == pytest.approx(expected_pnl)
    assert result.final_capital == pytest.approx(10000 + expected_pnl)


def test_losing_short_reconciles_exactly():
    # entry 100 (short), SL=105 hit normally (open=101 stays below SL, high=106 touches it)
    df = _bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100), (101, 106, 100.5, 105)])
    bt = Backtester(_config())
    result = bt.run(df, _fixed_notional_signal("SHORT", 105, 1))

    trade = result.trades[0]
    assert trade.exit_price == pytest.approx(105.0)

    price_pnl = (100 - 105) * 10
    entry_fee = 100 * 10 * TAKER_FEE
    exit_fee = 105 * 10 * TAKER_FEE
    expected_pnl = price_pnl - entry_fee - exit_fee

    assert trade.pnl == pytest.approx(expected_pnl)
    assert result.final_capital == pytest.approx(10000 + expected_pnl)


def test_funding_payment_composes_correctly_with_price_pnl_and_fees():
    """A winning LONG that also crosses one funding event -- verifies price
    P&L, fees, and funding all combine correctly in the final number."""
    df = _bars([
        (100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100),
        (100, 101, 99, 100), (105, 106, 104, 105),
    ])
    funding = pd.DataFrame({"timestamp": [df["timestamp"].iloc[3]], "rate": [0.0002]})  # 0.02%

    bt = Backtester(_config())
    result = bt.run(df, _fixed_notional_signal("LONG", 90, 105), funding_rates=funding)

    trade = result.trades[0]
    price_pnl = (105 - 100) * 10
    entry_fee = 100 * 10 * TAKER_FEE
    exit_fee = 105 * 10 * TAKER_FEE
    funding_cost = 10 * 100 * 0.0002  # quantity * mark_price(open) * rate
    expected_pnl = price_pnl - entry_fee - exit_fee - funding_cost

    assert trade.funding == pytest.approx(funding_cost)
    assert trade.pnl == pytest.approx(expected_pnl)
    assert result.final_capital == pytest.approx(10000 + expected_pnl)


def test_gap_through_stop_reconciles_exactly():
    df = _bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100), (94, 95, 90, 92)])
    bt = Backtester(_config())
    result = bt.run(df, _fixed_notional_signal("LONG", 98, 200))

    trade = result.trades[0]
    assert trade.exit_price == pytest.approx(94.0)  # gapped-through open, not the stale stop=98

    price_pnl = (94 - 100) * 10
    entry_fee = 100 * 10 * TAKER_FEE
    exit_fee = 94 * 10 * TAKER_FEE
    expected_pnl = price_pnl - entry_fee - exit_fee

    assert trade.pnl == pytest.approx(expected_pnl)
    assert result.final_capital == pytest.approx(10000 + expected_pnl)


def test_fee_calculation_matches_configured_rate_exactly():
    df = _bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100), (105, 106, 104, 105)])
    bt = Backtester(_config())
    result = bt.run(df, _fixed_notional_signal("LONG", 90, 105))

    trade = result.trades[0]
    expected_entry_fee = 100 * 10 * TAKER_FEE
    expected_exit_fee = 105 * 10 * TAKER_FEE
    assert trade.fees == pytest.approx(expected_entry_fee + expected_exit_fee)
    assert result.total_fees == pytest.approx(expected_entry_fee + expected_exit_fee)
