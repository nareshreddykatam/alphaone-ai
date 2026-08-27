"""Run the data-quality validator against stored candles and print/save a
report. Also labels invalid rows in the DB (quality_status/quality_reason)
-- never repairs them.

Usage:
    python scripts/validate_data.py --symbol BTC/USDT --timeframe 1h
"""
import argparse
import asyncio
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import select, update

from scripts._common import new_db_session
from database.schema.models import Candle
from services.data_quality.validator import validate_candles, label_quality
from services.data_quality.report import to_text, to_json


async def load_candles_df(db, symbol: str, timeframe: str, start=None, end=None) -> pd.DataFrame:
    stmt = select(Candle).where(Candle.symbol == symbol, Candle.timeframe == timeframe).order_by(Candle.timestamp.asc())
    if start:
        stmt = stmt.where(Candle.timestamp >= start)
    if end:
        stmt = stmt.where(Candle.timestamp <= end)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "id": r.id, "timestamp": r.timestamp, "open": r.open, "high": r.high,
        "low": r.low, "close": r.close, "volume": r.volume,
    } for r in rows])


async def main(symbol: str, timeframe: str, days: int | None, out_dir: str, apply_labels: bool):
    start = datetime.utcnow() - timedelta(days=days) if days else None

    async with new_db_session() as db:
        df = await load_candles_df(db, symbol, timeframe, start=start)

        if df.empty:
            print(f"No candles found for {symbol} {timeframe}. Run scripts/download_data.py first.")
            return

        report = validate_candles(df, symbol, timeframe)
        print(to_text(report))

        if out_dir:
            from pathlib import Path
            out_path = Path(out_dir)
            out_path.mkdir(parents=True, exist_ok=True)
            json_path = out_path / f"data_quality_{symbol.replace('/', '-')}_{timeframe}.json"
            json_path.write_text(to_json(report), encoding="utf-8")
            print(f"\nWrote JSON report to {json_path}")

        if apply_labels:
            labeled = label_quality(df)
            invalid_rows = labeled[labeled["quality_status"] == "invalid"]
            for _, row in invalid_rows.iterrows():
                await db.execute(
                    update(Candle)
                    .where(Candle.id == row["id"])
                    .values(quality_status="invalid", quality_reason=row["quality_reason"])
                )
            await db.commit()
            print(f"\nLabeled {len(invalid_rows)} invalid candle(s) in the database (values unchanged, only status/reason set).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--days", type=int, default=None, help="Limit to the last N days; default is the full stored history")
    parser.add_argument("--out-dir", default="./reports")
    parser.add_argument("--apply-labels", action="store_true", help="Write quality_status/quality_reason back to the DB for invalid rows")
    args = parser.parse_args()

    asyncio.run(main(args.symbol, args.timeframe, args.days, args.out_dir, args.apply_labels))
