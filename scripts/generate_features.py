"""Compute the full feature set for a symbol/timeframe and cache it to
parquet for reuse by baselines/backtests/training, recording a dataset
version so results referencing this file are traceable to the exact input.

Usage:
    python scripts/generate_features.py --symbol BTC/USDT --timeframe 1h --days 365
"""
import argparse
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from scripts._common import new_db_session, load_candles, load_funding_rates, load_open_interest, load_liquidations, get_dataset_version
from services.feature_engine.engine import FeatureEngine


async def main(symbol: str, timeframe: str, days: int | None, out_dir: str):
    start = datetime.utcnow() - timedelta(days=days) if days else None

    async with new_db_session() as db:
        df = await load_candles(db, symbol, timeframe, start=start)
        if df.empty:
            print(f"No valid candles found for {symbol} {timeframe}. Run download_data.py / validate_data.py first.")
            return

        funding = await load_funding_rates(db, symbol)
        oi = await load_open_interest(db, symbol)
        liqs = await load_liquidations(db, symbol)

        print(f"Loaded {len(df)} candles, {len(funding)} funding rows, {len(oi)} OI rows, {len(liqs)} liquidation rows")

        engine = FeatureEngine()
        features_df = engine.compute_features(df, funding, oi, liqs)

        version = get_dataset_version(symbol, timeframe, df)
        print(f"Dataset version: {version}")
        print(f"Computed {len(engine.feature_names)} feature columns over {len(features_df)} rows")

        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        # CSV, not parquet: pyarrow's native extension is unusable on some
        # locked-down Windows machines (blocked by Application Control
        # policy), and pandas itself fails to import once pyarrow is merely
        # installed there -- CSV has zero native-dependency risk.
        file_path = out_path / f"features_{symbol.replace('/', '-')}_{timeframe}.csv"
        features_df.to_csv(file_path, index=False)
        print(f"Wrote features to {file_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--out-dir", default="./ml/datasets/cache")
    args = parser.parse_args()

    asyncio.run(main(args.symbol, args.timeframe, args.days, args.out_dir))
