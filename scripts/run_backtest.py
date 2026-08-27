"""Run a single baseline strategy through the shared Backtester, print/save
its structured report, and persist the run + metrics to the database for
later reproducible inspection (see generate_report.py).

Usage:
    python scripts/run_backtest.py --strategy ema_crossover --symbol BTC/USDT --timeframe 1h --days 365
"""
import argparse
import asyncio
import json
from datetime import datetime, timedelta

from scripts._common import new_db_session, load_candles, load_funding_rates, get_dataset_version, get_code_version
from database.schema.models import BacktestRun, BacktestMetric
from services.backtester.engine import BacktestConfig
from services.backtester.report import RunMetadata, to_text, write_report
from services.backtester.sanity_checks import assert_result_sane, BacktestSanityError
from ml.evaluation.baselines import run_baseline, BASELINE_STRATEGIES


async def persist_run(db, meta: RunMetadata, config: BacktestConfig, result) -> str:
    run = BacktestRun(
        strategy_name=meta.strategy_name,
        strategy_version=meta.strategy_version,
        symbol=meta.symbol,
        timeframe=meta.timeframe,
        config_json=json.loads(json.dumps({
            "initial_capital": config.initial_capital,
            "taker_fee": config.exchange_spec.taker_fee,
            "maker_fee": config.exchange_spec.maker_fee,
            "slippage_bps": config.exchange_spec.slippage_bps,
            "funding_interval_hours": config.exchange_spec.funding_interval_hours,
            "funding_rate_avg": config.funding_rate_avg,
            "max_leverage": config.exchange_spec.max_leverage,
            "funding_coverage": meta.funding_coverage,
        })),
        dataset_start=meta.period_start,
        dataset_end=meta.period_end,
        dataset_version=meta.dataset_version,
        code_version=meta.code_version,
    )
    db.add(run)
    await db.flush()

    metric = BacktestMetric(
        run_id=run.id,
        total_pnl=result.total_pnl, total_pnl_pct=result.total_pnl_pct,
        total_trades=result.total_trades, winning_trades=result.winning_trades,
        losing_trades=result.losing_trades, win_rate=result.win_rate,
        profit_factor=result.profit_factor, expectancy=result.expectancy,
        average_r=result.average_r, sharpe_ratio=result.sharpe_ratio,
        sortino_ratio=result.sortino_ratio, max_drawdown=result.max_drawdown,
        max_drawdown_pct=result.max_drawdown_pct, recovery_factor=result.recovery_factor,
        average_trade_pnl=result.average_trade_pnl, average_winning_trade=result.average_winning_trade,
        average_losing_trade=result.average_losing_trade, largest_win=result.largest_win,
        largest_loss=result.largest_loss, consecutive_wins=result.consecutive_wins,
        consecutive_losses=result.consecutive_losses, total_fees=result.total_fees,
        total_funding=result.total_funding, initial_capital=result.initial_capital,
        final_capital=result.final_capital,
    )
    db.add(metric)
    await db.commit()
    return str(run.id)


async def main(strategy: str, symbol: str, timeframe: str, days: int | None, out_dir: str, persist: bool):
    start = datetime.utcnow() - timedelta(days=days) if days else None

    async with new_db_session() as db:
        df = await load_candles(db, symbol, timeframe, start=start)
        if df.empty:
            print(f"No valid candles found for {symbol} {timeframe}. Run download_data.py first.")
            return

        funding = await load_funding_rates(db, symbol, start=df["timestamp"].iloc[0], end=df["timestamp"].iloc[-1])
        funding_rates = funding if len(funding) > 0 else None
        if funding_rates is None:
            print("WARNING: no real funding data available for this period -- falling back to the flat-average estimate.\n")
            funding_coverage = None
        else:
            funding_coverage = f"{len(funding)} events, {funding['timestamp'].min()} -> {funding['timestamp'].max()}"

        config = BacktestConfig(initial_capital=10000)
        display_name, result = run_baseline(strategy, df, config, funding_rates=funding_rates)

        try:
            assert_result_sane(result, config)
        except BacktestSanityError as e:
            print(f"REFUSING TO GENERATE REPORT -- sanity checks failed:\n{e}")
            return

        meta = RunMetadata(
            strategy_name=display_name,
            symbol=symbol,
            timeframe=timeframe,
            period_start=df["timestamp"].iloc[0],
            period_end=df["timestamp"].iloc[-1],
            dataset_version=get_dataset_version(symbol, timeframe, df),
            code_version=get_code_version(),
            funding_coverage=funding_coverage,
        )

        print(to_text(meta, result))
        paths = write_report(meta, result, out_dir)
        print(f"\nWrote report files: {paths}")

        if persist:
            run_id = await persist_run(db, meta, config, result)
            print(f"\nPersisted backtest_runs.id = {run_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", required=True, choices=list(BASELINE_STRATEGIES))
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--out-dir", default="./reports")
    parser.add_argument("--no-persist", action="store_true")
    args = parser.parse_args()

    asyncio.run(main(args.strategy, args.symbol, args.timeframe, args.days, args.out_dir, not args.no_persist))
