"""Download historical BTC/USDT perpetual futures data from Binance.

Usage:
    python scripts/download_data.py --symbol BTC/USDT --timeframes 1m,5m,15m,1h,4h,1d --days 365

Resumable: re-running with the same arguments only fetches candles after
whatever is already stored (see DataIngestionService.backfill). Also pulls
funding rate history (up to ~1000 periods back) and open interest history
(Binance limits this to ~30 days regardless of --days -- see
docs/known_limitations.md). Liquidations have no historical backfill
endpoint on Binance, so only a recent snapshot is captured.
"""
import argparse
import asyncio
from datetime import datetime, timedelta

from scripts._common import new_db_session, new_exchange, get_code_version
from services.market_data.ingestion import DataIngestionService, TIMEFRAMES


async def main(symbol: str, timeframes: list[str], days: int):
    end = datetime.utcnow()
    start = end - timedelta(days=days)

    exchange = new_exchange()
    try:
        async with new_db_session() as db:
            svc = DataIngestionService(exchange, db)

            print(f"Code version: {get_code_version()}")
            print(f"Downloading {symbol} from {start} to {end}")

            for tf in timeframes:
                print(f"\n[{tf}] backfilling candles...")
                stored = await svc.backfill(symbol, tf, start, end)
                print(f"[{tf}] stored {stored} new candles")

            print("\nBackfilling funding rate history...")
            funding_stored = await svc.backfill_funding_rates(symbol, start, end)
            print(f"Stored {funding_stored} new funding rate records")

            print("\nBackfilling open interest history (Binance caps this at ~30 days)...")
            oi_stored = await svc.backfill_open_interest(symbol, max(start, end - timedelta(days=30)), end)
            print(f"Stored {oi_stored} new open interest records")

            print("\nFetching recent liquidations snapshot (NOT a historical backfill)...")
            liq_stored = await svc.fetch_recent_liquidations(symbol)
            print(f"Stored {liq_stored} recent liquidation records")
    finally:
        await exchange.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTC/USDT", help="ccxt symbol for the USDT-M perpetual (resolves correctly under defaultType=future)")
    parser.add_argument("--timeframes", default=",".join(TIMEFRAMES))
    parser.add_argument("--days", type=int, default=365)
    args = parser.parse_args()

    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    asyncio.run(main(args.symbol, timeframes, args.days))
