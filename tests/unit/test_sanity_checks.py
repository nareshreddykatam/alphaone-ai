"""Phase 2.5, section 17: automated checks for impossible backtest results.
Each test proves the checker actually catches the specific corruption named
(not just that it passes on good data)."""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from services.backtester.engine import Backtester, BacktestConfig, BacktestResult, BacktestTrade
from services.backtester.exchange_spec import ExchangeSpec
from services.backtester.sanity_checks import check_result_sanity, assert_result_sane, BacktestSanityError


def _bars(specs, start=datetime(2024, 1, 1)):
    rows = []
    for i, (o, h, l, c) in enumerate(specs):
        rows.append({"timestamp": start + timedelta(hours=i), "open": o, "high": h, "low": l, "close": c})
    return pd.DataFrame(rows)


def _real_result() -> BacktestResult:
    df = _bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100), (105, 106, 104, 105)])
    fired = {"done": False}

    def signal_func(d):
        if len(d) == 2 and not fired["done"]:
            fired["done"] = True
            return {"signal_type": "LONG", "stop_loss": 90, "take_profit_1": 105, "leverage": 1, "position_pct": 0.1}
        return None

    bt = Backtester(BacktestConfig(initial_capital=10000, exchange_spec=ExchangeSpec(slippage_bps=0)))
    return bt.run(df, signal_func)


def test_real_backtest_result_passes_all_checks():
    result = _real_result()
    violations = check_result_sanity(result)
    assert violations == [], f"a genuine backtest result should pass every sanity check, got: {violations}"
    assert_result_sane(result)  # must not raise


def test_negative_profit_factor_is_caught():
    result = _real_result()
    result.profit_factor = -1.0
    violations = check_result_sanity(result)
    assert any(v.check == "profit_factor_non_negative" for v in violations)


def test_win_rate_out_of_range_is_caught():
    result = _real_result()
    result.win_rate = 150.0
    violations = check_result_sanity(result)
    assert any(v.check == "win_rate_range" for v in violations)

    result2 = _real_result()
    result2.win_rate = -5.0
    violations2 = check_result_sanity(result2)
    assert any(v.check == "win_rate_range" for v in violations2)


def test_negative_drawdown_is_caught():
    result = _real_result()
    result.max_drawdown_pct = -1.0
    violations = check_result_sanity(result)
    assert any(v.check == "drawdown_non_negative" for v in violations)


def test_final_capital_mismatch_is_caught():
    result = _real_result()
    result.final_capital = result.initial_capital + result.total_pnl + 500  # corrupt it
    violations = check_result_sanity(result)
    assert any(v.check == "final_capital_matches_equity" for v in violations)


def test_negative_fees_are_caught():
    result = _real_result()
    result.total_fees = -10.0
    violations = check_result_sanity(result)
    assert any(v.check == "fees_non_negative" for v in violations)


def test_trade_count_mismatch_with_win_loss_split_is_caught():
    result = _real_result()
    result.total_trades = 99
    violations = check_result_sanity(result)
    assert any(v.check == "trade_count_matches_win_loss_split" for v in violations)


def test_trade_count_mismatch_with_recorded_trades_is_caught():
    result = _real_result()
    result.trades = []  # wipe recorded trades but leave total_trades=1
    violations = check_result_sanity(result)
    assert any(v.check == "trade_count_matches_recorded_trades" for v in violations)


def test_exit_before_entry_is_caught():
    result = _real_result()
    trade = result.trades[0]
    trade.exit_time = trade.entry_time - timedelta(hours=5)
    violations = check_result_sanity(result)
    assert any(v.check == "exit_not_before_entry" for v in violations)


def test_negative_position_size_is_caught():
    result = _real_result()
    result.trades[0].quantity = -5.0
    violations = check_result_sanity(result)
    assert any(v.check == "position_size_non_negative" for v in violations)


def test_leverage_exceeding_configured_max_is_caught():
    result = _real_result()
    result.trades[0].leverage = 999
    config = BacktestConfig(exchange_spec=ExchangeSpec(max_leverage=5))
    violations = check_result_sanity(result, config)
    assert any(v.check == "leverage_within_configured_limit" for v in violations)


def test_unexplained_equity_jump_is_caught():
    result = _real_result()
    result.equity_curve[-1]["equity"] += 99999  # inject an impossible jump
    violations = check_result_sanity(result)
    assert any(v.check == "equity_curve_explained_by_trades" for v in violations)


def test_assert_result_sane_raises_with_all_violations_listed():
    result = _real_result()
    result.win_rate = -1
    result.total_fees = -1
    with pytest.raises(BacktestSanityError) as exc_info:
        assert_result_sane(result)
    assert len(exc_info.value.violations) >= 2
