from datetime import datetime, timedelta

import pandas as pd
import pytest

from ml.evaluation.cost_sensitivity import run_cost_sensitivity, format_cost_sensitivity_table, COST_MULTIPLIERS
from services.backtester.engine import BacktestConfig
from services.backtester.exchange_spec import ExchangeSpec


def _bars(n=20):
    rows = []
    price = 100.0
    t = datetime(2024, 1, 1)
    for i in range(n):
        price *= 1.01 if i % 2 == 0 else 0.995
        rows.append({"timestamp": t, "open": price, "high": price * 1.01, "low": price * 0.99, "close": price})
        t += timedelta(hours=4)
    return pd.DataFrame(rows)


def _signal_func(d):
    if len(d) == 2:
        entry = d.iloc[-1]["close"]
        return {"signal_type": "LONG", "stop_loss": entry * 0.9, "take_profit_1": entry * 1.3, "leverage": 1}
    return None


def test_higher_cost_multiplier_never_improves_net_return():
    df = _bars(20)
    config = BacktestConfig(initial_capital=10000, exchange_spec=ExchangeSpec(taker_fee=0.0004, slippage_bps=1))
    results = run_cost_sensitivity(df, _signal_func, config)

    by_scenario = {r.scenario: r.result.total_pnl_pct for r in results}
    assert by_scenario["base"] >= by_scenario["base_plus_25pct"] >= by_scenario["base_plus_50pct"] >= by_scenario["base_plus_100pct"] - 1e-9


def test_all_declared_scenarios_are_run():
    df = _bars(20)
    config = BacktestConfig(initial_capital=10000)
    results = run_cost_sensitivity(df, _signal_func, config)
    scenarios = {r.scenario for r in results}
    assert scenarios == set(COST_MULTIPLIERS.keys())


def test_format_table_has_one_row_per_scenario():
    df = _bars(20)
    config = BacktestConfig(initial_capital=10000)
    results = run_cost_sensitivity(df, _signal_func, config)
    table = format_cost_sensitivity_table(results)
    assert len(table) == len(COST_MULTIPLIERS)
