"""End-to-end integration test: ingest (into a real SQLite DB) -> validate
-> compute features -> run a baseline -> get a report. Exercises the
seams between modules that unit tests (which mock/construct data directly)
don't cover.
"""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema import models  # noqa: F401
from services.market_data import ExchangeBase, OHLCV
from services.market_data.ingestion import DataIngestionService
from services.data_quality.validator import validate_candles
from services.feature_engine.engine import FeatureEngine
from services.backtester.engine import BacktestConfig
from services.backtester.report import RunMetadata, to_text
from ml.evaluation.baselines import run_baseline


class SyntheticExchange(ExchangeBase):
    """Deterministic synthetic OHLCV generator standing in for a real
    exchange, so this test needs no network access."""

    def __init__(self, n_candles: int = 400):
        self.n_candles = n_candles

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1000):
        rng = np.random.default_rng(11)
        base = since or datetime(2024, 1, 1)
        n = min(limit, self.n_candles)
        closes = 40000 + np.cumsum(rng.standard_normal(n) * 50)
        out = []
        for i in range(n):
            c = float(closes[i])
            out.append(OHLCV(
                timestamp=base + timedelta(hours=i), open=c, high=c + 20, low=c - 20,
                close=c, volume=float(rng.uniform(10, 100)), timeframe=timeframe, symbol=symbol,
            ))
        return out if since is None or since == base else []

    async def fetch_funding_rate(self, symbol):
        raise NotImplementedError

    async def fetch_funding_rate_history(self, symbol, since=None, limit=1000):
        return []

    async def fetch_open_interest(self, symbol):
        raise NotImplementedError

    async def fetch_open_interest_history(self, symbol, timeframe="1h", since=None, limit=500):
        return []

    async def fetch_liquidations(self, symbol, limit=100):
        return []

    async def fetch_order_book(self, symbol, limit=20):
        raise NotImplementedError

    async def fetch_ticker(self, symbol):
        raise NotImplementedError

    async def close(self):
        pass


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_full_pipeline_ingest_validate_features_backtest_report(db_session):
    exchange = SyntheticExchange(n_candles=400)
    svc = DataIngestionService(exchange, db_session)

    start = datetime(2024, 1, 1)
    stored = await svc.backfill("BTC/USDT", "1h", start, start + timedelta(hours=400))
    assert stored == 400

    candles = await svc.get_stored_candles("BTC/USDT", "1h")
    df = pd.DataFrame([{
        "timestamp": c.timestamp, "open": c.open, "high": c.high, "low": c.low,
        "close": c.close, "volume": c.volume,
    } for c in candles])
    assert len(df) == 400

    report = validate_candles(df, "BTC/USDT", "1h", as_of=df["timestamp"].max())
    assert report.duplicate_count == 0
    assert report.invalid_count == 0
    assert report.coverage_pct == 100.0

    engine = FeatureEngine()
    features_df = engine.compute_features(df)
    assert len(engine.feature_names) > 20
    assert len(features_df) == len(df)

    config = BacktestConfig(initial_capital=10000)
    display_name, result = run_baseline("ema_crossover", df, config)
    assert result is not None
    assert result.initial_capital == 10000

    meta = RunMetadata(
        strategy_name=display_name, symbol="BTC/USDT", timeframe="1h",
        period_start=df["timestamp"].iloc[0], period_end=df["timestamp"].iloc[-1],
    )
    text = to_text(meta, result)
    assert "BACKTEST REPORT" in text
    assert "Net Return:" in text
    # the report must state the outcome plainly, whichever way it went
    assert "RESULT:" in text
