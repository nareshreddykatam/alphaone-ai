"""Shared helpers for the Phase 2 reproducibility scripts.

Every script that produces a result records: the dataset it ran against
(symbol/timeframe/row-count/time-range hash), a content hash standing in
for a code version (this repo has no git history to pin a commit to), and
the exact config used -- so a result can be reproduced or at least
attributed to a specific state of the code and data.
"""
import hashlib
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema import async_session
from database.schema.models import Candle, FundingRate as FundingRateRow, OpenInterestRecord, LiquidationEvent
from services.market_data.binance import BinanceExchange
from apps.api.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ["services", "database", "ml", "apps"]


def get_code_version() -> str:
    """Prefer the git commit hash; fall back to a content hash of the
    tracked source directories when this isn't a git repository (it isn't,
    as of Phase 2 -- see project notes)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return f"git:{result.stdout.strip()}"
    except Exception:
        pass

    hasher = hashlib.sha256()
    for d in SOURCE_DIRS:
        base = REPO_ROOT / d
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            hasher.update(path.read_bytes())
    return f"content:{hasher.hexdigest()[:16]}"


def get_dataset_version(symbol: str, timeframe: str, df: pd.DataFrame) -> str:
    if df.empty:
        return f"{symbol}:{timeframe}:empty"
    hasher = hashlib.sha256()
    hasher.update(f"{symbol}:{timeframe}:{len(df)}:{df['timestamp'].min()}:{df['timestamp'].max()}".encode())
    return f"{symbol}:{timeframe}:{len(df)}rows:{hasher.hexdigest()[:12]}"


def new_db_session() -> AsyncSession:
    return async_session()


def new_exchange() -> BinanceExchange:
    settings = get_settings()
    # Historical public market data (klines/funding/OI) is available on
    # mainnet without authentication; testnet data is sparse and not
    # representative, so downloads always use mainnet regardless of the
    # configured TRADING_MODE/testnet flag (no order placement ever occurs).
    return BinanceExchange(api_key="", api_secret="", testnet=False)


def utcnow() -> datetime:
    return datetime.utcnow()


async def load_candles(
    db: AsyncSession, symbol: str, timeframe: str,
    start: Optional[datetime] = None, end: Optional[datetime] = None,
    valid_only: bool = True,
) -> pd.DataFrame:
    stmt = select(Candle).where(Candle.symbol == symbol, Candle.timeframe == timeframe).order_by(Candle.timestamp.asc())
    if start:
        stmt = stmt.where(Candle.timestamp >= start)
    if end:
        stmt = stmt.where(Candle.timestamp <= end)
    if valid_only:
        stmt = stmt.where(Candle.quality_status == "valid")
    result = await db.execute(stmt)
    rows = result.scalars().all()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([{
        "timestamp": r.timestamp, "open": r.open, "high": r.high, "low": r.low,
        "close": r.close, "volume": r.volume, "symbol": r.symbol, "timeframe": r.timeframe,
    } for r in rows])


async def load_funding_rates(db: AsyncSession, symbol: str, start=None, end=None) -> pd.DataFrame:
    stmt = select(FundingRateRow).where(FundingRateRow.symbol == symbol).order_by(FundingRateRow.timestamp.asc())
    if start:
        stmt = stmt.where(FundingRateRow.timestamp >= start)
    if end:
        stmt = stmt.where(FundingRateRow.timestamp <= end)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    if not rows:
        return pd.DataFrame(columns=["timestamp", "rate"])
    return pd.DataFrame([{"timestamp": r.timestamp, "rate": r.rate} for r in rows])


async def load_open_interest(db: AsyncSession, symbol: str, start=None, end=None) -> pd.DataFrame:
    stmt = select(OpenInterestRecord).where(OpenInterestRecord.symbol == symbol).order_by(OpenInterestRecord.timestamp.asc())
    if start:
        stmt = stmt.where(OpenInterestRecord.timestamp >= start)
    if end:
        stmt = stmt.where(OpenInterestRecord.timestamp <= end)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    if not rows:
        return pd.DataFrame(columns=["timestamp", "value"])
    return pd.DataFrame([{"timestamp": r.timestamp, "value": r.value} for r in rows])


async def load_liquidations(db: AsyncSession, symbol: str, start=None, end=None) -> pd.DataFrame:
    stmt = select(LiquidationEvent).where(LiquidationEvent.symbol == symbol).order_by(LiquidationEvent.timestamp.asc())
    if start:
        stmt = stmt.where(LiquidationEvent.timestamp >= start)
    if end:
        stmt = stmt.where(LiquidationEvent.timestamp <= end)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    if not rows:
        return pd.DataFrame(columns=["timestamp", "side", "quantity"])
    return pd.DataFrame([{"timestamp": r.timestamp, "side": r.side, "quantity": r.quantity} for r in rows])
