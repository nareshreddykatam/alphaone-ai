"""Phase 3, section 27: re-run a candidate strategy's signal_func under
several realistic cost multipliers to see whether its performance survives
being wrong about fees/slippage in the pessimistic direction. Never picks
"the cost assumption that produces the best result" -- it runs a fixed,
pre-declared ladder and reports all of them.
"""
from dataclasses import dataclass

import pandas as pd

from services.backtester.engine import Backtester, BacktestConfig, BacktestResult
from services.backtester.exchange_spec import ExchangeSpec

COST_MULTIPLIERS = {
    "base": 1.0,
    "base_plus_25pct": 1.25,
    "base_plus_50pct": 1.50,
    "base_plus_100pct": 2.00,
}


@dataclass
class CostSensitivityResult:
    scenario: str
    multiplier: float
    result: BacktestResult


def run_cost_sensitivity(
    df: pd.DataFrame,
    signal_func,
    base_config: BacktestConfig,
    funding_rates: pd.DataFrame | None = None,
) -> list[CostSensitivityResult]:
    results = []
    base_spec = base_config.exchange_spec
    for scenario, mult in COST_MULTIPLIERS.items():
        spec = ExchangeSpec(
            maker_fee=base_spec.maker_fee * mult,
            taker_fee=base_spec.taker_fee * mult,
            funding_interval_hours=base_spec.funding_interval_hours,
            slippage_bps=base_spec.slippage_bps * mult,
            spread_bps=base_spec.spread_bps * mult,
            tick_size=base_spec.tick_size,
            qty_precision=base_spec.qty_precision,
            min_qty=base_spec.min_qty,
            max_leverage=base_spec.max_leverage,
            maintenance_margin_pct=base_spec.maintenance_margin_pct,
        )
        config = BacktestConfig(
            initial_capital=base_config.initial_capital,
            exchange_spec=spec,
            funding_rate_avg=base_config.funding_rate_avg * mult,
            risk_config=base_config.risk_config,
        )
        bt = Backtester(config)
        result = bt.run(df, signal_func, funding_rates=funding_rates)
        results.append(CostSensitivityResult(scenario=scenario, multiplier=mult, result=result))
    return results


def format_cost_sensitivity_table(results: list[CostSensitivityResult]) -> pd.DataFrame:
    rows = []
    for r in results:
        rows.append({
            "scenario": r.scenario, "cost_multiplier": r.multiplier,
            "trades": r.result.total_trades, "win_rate": r.result.win_rate,
            "profit_factor": r.result.profit_factor, "net_return_pct": r.result.total_pnl_pct,
            "sharpe": r.result.sharpe_ratio, "max_dd_pct": r.result.max_drawdown_pct,
            "total_fees": r.result.total_fees, "total_funding": r.result.total_funding,
        })
    return pd.DataFrame(rows)
