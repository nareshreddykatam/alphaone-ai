"""Run all baseline strategies over the same dataset and print a comparison
table. See docs/execution_semantics.md and docs/exchange_assumptions.md for
the shared assumptions every row below uses identically.

Usage:
    python scripts/run_baselines.py --symbol BTC/USDT --timeframe 1h --days 365
"""
import argparse
import asyncio
from datetime import datetime, timedelta

import pandas as pd

from scripts._common import new_db_session, load_candles, load_funding_rates
from services.backtester.engine import BacktestConfig
from ml.evaluation.baselines import run_all_baselines


async def main(symbol: str, timeframe: str, days: int | None):
    start = datetime.utcnow() - timedelta(days=days) if days else None

    async with new_db_session() as db:
        df = await load_candles(db, symbol, timeframe, start=start)
        if df.empty:
            print(f"No valid candles found for {symbol} {timeframe}. Run download_data.py first.")
            return

        funding = await load_funding_rates(db, symbol, start=df["timestamp"].iloc[0], end=df["timestamp"].iloc[-1])
        funding_note = f"{len(funding)} real funding events" if len(funding) > 0 else "NO funding data -- falling back to flat-average estimate"
        print(f"Running baselines on {len(df)} candles ({df['timestamp'].iloc[0]} -> {df['timestamp'].iloc[-1]}), {funding_note}\n")
        config = BacktestConfig(initial_capital=10000)
        results = run_all_baselines(df, config, funding_rates=funding if len(funding) > 0 else None)

        rows = []
        for name, r in results.items():
            rows.append({
                "strategy": name,
                "trades": r.total_trades,
                "win_rate_pct": r.win_rate,
                "profit_factor": r.profit_factor,
                "expectancy_R": r.average_r,
                "sharpe": r.sharpe_ratio,
                "sortino": r.sortino_ratio,
                "max_dd_pct": r.max_drawdown_pct,
                "net_return_pct": r.total_pnl_pct,
                "fees": r.total_fees,
                "funding": r.total_funding,
            })

        table = pd.DataFrame(rows)
        pd.set_option("display.width", 140)
        print(table.to_string(index=False))

        unprofitable = table[table["net_return_pct"] <= 0]
        if not unprofitable.empty:
            print(f"\n{len(unprofitable)} of {len(table)} baseline(s) did NOT show a positive net return after costs over this period.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--days", type=int, default=None)
    args = parser.parse_args()

    asyncio.run(main(args.symbol, args.timeframe, args.days))
