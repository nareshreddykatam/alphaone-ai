"""Render the structured report for an already-persisted backtest run,
without re-running anything -- useful for revisiting a past result.

Usage:
    python scripts/generate_report.py --run-id <uuid>
"""
import argparse
import asyncio

from sqlalchemy import select

from scripts._common import new_db_session
from database.schema.models import BacktestRun, BacktestMetric
from services.backtester.engine import BacktestResult
from services.backtester.report import RunMetadata, to_text


async def main(run_id: str):
    async with new_db_session() as db:
        run = (await db.execute(select(BacktestRun).where(BacktestRun.id == run_id))).scalar_one_or_none()
        if run is None:
            print(f"No backtest_runs row found for id={run_id}")
            return
        metric = (await db.execute(select(BacktestMetric).where(BacktestMetric.run_id == run_id))).scalar_one_or_none()
        if metric is None:
            print(f"No backtest_metrics row found for run_id={run_id}")
            return

        meta = RunMetadata(
            strategy_name=run.strategy_name,
            symbol=run.symbol,
            timeframe=run.timeframe,
            period_start=run.dataset_start,
            period_end=run.dataset_end,
            dataset_version=run.dataset_version,
            code_version=run.code_version,
            strategy_version=run.strategy_version,
        )

        result = BacktestResult(
            trades=[], equity_curve=[],
            total_pnl=metric.total_pnl, total_pnl_pct=metric.total_pnl_pct,
            total_trades=metric.total_trades, winning_trades=metric.winning_trades,
            losing_trades=metric.losing_trades, win_rate=metric.win_rate,
            profit_factor=metric.profit_factor, expectancy=metric.expectancy,
            average_r=metric.average_r, sharpe_ratio=metric.sharpe_ratio,
            sortino_ratio=metric.sortino_ratio, max_drawdown=metric.max_drawdown,
            max_drawdown_pct=metric.max_drawdown_pct, recovery_factor=metric.recovery_factor,
            average_trade_pnl=metric.average_trade_pnl, average_winning_trade=metric.average_winning_trade,
            average_losing_trade=metric.average_losing_trade, largest_win=metric.largest_win,
            largest_loss=metric.largest_loss, consecutive_wins=metric.consecutive_wins,
            consecutive_losses=metric.consecutive_losses, total_fees=metric.total_fees,
            total_funding=metric.total_funding, training_period="", test_period="",
            initial_capital=metric.initial_capital, final_capital=metric.final_capital,
        )

        print(to_text(meta, result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    asyncio.run(main(args.run_id))
