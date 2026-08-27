import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import Candle

TIMEFRAMES_FOR_FEATURES = ["5m", "15m", "1h", "4h"]


class DatasetLoader:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def load_candles(
        self, symbol: str, timeframe: str,
        start: Optional[datetime] = None, end: Optional[datetime] = None,
    ) -> pd.DataFrame:
        stmt = select(Candle).where(
            Candle.symbol == symbol,
            Candle.timeframe == timeframe,
        ).order_by(Candle.timestamp.asc())

        if start:
            stmt = stmt.where(Candle.timestamp >= start)
        if end:
            stmt = stmt.where(Candle.timestamp <= end)

        result = await self.db.execute(stmt)
        rows = result.scalars().all()

        if not rows:
            return pd.DataFrame()

        data = [{
            "timestamp": r.timestamp,
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "volume": r.volume,
            "symbol": r.symbol,
            "timeframe": r.timeframe,
        } for r in rows]

        df = pd.DataFrame(data)
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df

    def create_labels(
        self, df: pd.DataFrame, forward_periods: int = 12, threshold: float = 0.005
    ) -> pd.DataFrame:
        df = df.copy()
        future_return = df["close"].shift(-forward_periods) / df["close"] - 1

        conditions = [
            future_return > threshold,
            future_return < -threshold,
        ]
        choices = [1, -1]
        df["label"] = np.select(conditions, choices, default=0)

        df["future_return"] = future_return
        df = df.dropna(subset=["label"])

        return df

    def split_chronological(
        self, df: pd.DataFrame,
        train_pct: float = 0.70, val_pct: float = 0.15, test_pct: float = 0.15,
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        n = len(df)
        train_end = int(n * train_pct)
        val_end = int(n * (train_pct + val_pct))

        train = df.iloc[:train_end].copy()
        val = df.iloc[train_end:val_end].copy()
        test = df.iloc[val_end:].copy()

        return train, val, test

    def create_walk_forward_splits(
        self, df: pd.DataFrame,
        train_window: int = 5000,
        test_window: int = 1000,
        step: int = 1000,
        embargo: int = 100,
    ) -> list[tuple[pd.DataFrame, pd.DataFrame]]:
        splits = []
        start = 0

        while start + train_window + embargo + test_window <= len(df):
            train = df.iloc[start:start + train_window].copy()
            test_start = start + train_window + embargo
            test = df.iloc[test_start:test_start + test_window].copy()
            splits.append((train, test))
            start += step

        return splits
