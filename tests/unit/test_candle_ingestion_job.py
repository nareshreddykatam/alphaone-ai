"""Tests for services/scheduler/jobs.py: candle_ingestion_job -- the 5th
scheduler job that keeps the real-data Candle table topped up from
Binance. Against an in-memory SQLite DB and a fake exchange (never a real
Binance call), covering idempotency (requirement 2/3) and clean failure
propagation (so the runner's circuit breaker, tested separately in
test_scheduler_runner.py, can catch it).
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema import models  # noqa: F401 -- ensure models are registered
from database.schema.models import Candle
from services.market_data import ExchangeBase, OHLCV
from services.scheduler.jobs import candle_ingestion_job


class FakeBinance(ExchangeBase):
    """Simulates a real exchange with a FIXED, bounded set of historical
    candles (generated once at construction, ending "now") -- not an
    endless generator -- so DataIngestionService's pagination loop
    terminates correctly (two consecutive empty pages) exactly like it
    would against the real Binance API once it catches up to the present.
    Never a real network call."""

    def __init__(self, num_candles: int = 5, fail: bool = False, timeframe: str = "4h", anchor_end: datetime = None):
        self.fail = fail
        self.calls = 0
        interval = timedelta(hours=4) if timeframe == "4h" else timedelta(hours=1)
        end = anchor_end or datetime.utcnow()
        start = end - interval * num_candles
        self._all_candles = [
            OHLCV(
                timestamp=start + interval * i, open=100.0, high=101.0, low=99.0, close=100.5,
                volume=10.0, timeframe=timeframe, symbol="BTC/USDT",
            )
            for i in range(num_candles)
        ]

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1000):
        self.calls += 1
        if self.fail:
            raise ConnectionError("simulated Binance network failure")
        candles = [c for c in self._all_candles if since is None or c.timestamp >= since]
        return [
            OHLCV(timestamp=c.timestamp, open=c.open, high=c.high, low=c.low, close=c.close,
                  volume=c.volume, timeframe=timeframe, symbol=symbol)
            for c in candles[:limit]
        ]

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


async def _candle_count(session, symbol="BTC/USDT", timeframe="4h") -> int:
    result = await session.execute(
        select(func.count()).select_from(Candle).where(Candle.symbol == symbol, Candle.timeframe == timeframe)
    )
    return result.scalar_one()


# ---- 1. Ingests BTC/USDT 4h by default ----

@pytest.mark.asyncio
async def test_default_symbol_and_timeframe_are_btc_usdt_4h(db_session):
    exchange = FakeBinance(num_candles=5)
    stored = await candle_ingestion_job(db_session, exchange)
    assert stored == 5
    assert await _candle_count(db_session, "BTC/USDT", "4h") == 5


# ---- 2/3. Idempotent: fetches only missing/recent data, no duplicates ----

@pytest.mark.asyncio
async def test_second_run_with_same_data_stores_nothing_new(db_session):
    anchor = datetime.utcnow()  # fixed reference point shared by both exchange instances below
    exchange1 = FakeBinance(num_candles=5, anchor_end=anchor)
    first = await candle_ingestion_job(db_session, exchange1)
    assert first == 5

    # A second tick, using a fresh exchange instance (as the real runner
    # does every tick) that returns the exact SAME candles (same anchor) --
    # backfill()'s resume-from-MAX(timestamp) logic must make this tick's
    # real Binance page request start after what's already stored, and the
    # ON CONFLICT DO NOTHING insert must not duplicate anything even if it
    # somehow re-fetched an overlapping bar.
    exchange2 = FakeBinance(num_candles=5, anchor_end=anchor)
    second = await candle_ingestion_job(db_session, exchange2)

    assert second == 0  # every candle was already stored -- nothing new
    assert await _candle_count(db_session, "BTC/USDT", "4h") == 5  # no duplicates


@pytest.mark.asyncio
async def test_running_three_times_in_a_row_never_duplicates(db_session):
    anchor = datetime.utcnow()
    for _ in range(3):
        exchange = FakeBinance(num_candles=5, anchor_end=anchor)
        await candle_ingestion_job(db_session, exchange)
    assert await _candle_count(db_session, "BTC/USDT", "4h") == 5


# ---- 5. Failure handling: propagates cleanly, never silently swallowed ----

@pytest.mark.asyncio
async def test_exchange_failure_propagates_rather_than_being_swallowed(db_session):
    exchange = FakeBinance(fail=True)
    with pytest.raises(ConnectionError):
        await candle_ingestion_job(db_session, exchange)
    assert await _candle_count(db_session, "BTC/USDT", "4h") == 0  # nothing partially/incorrectly stored


@pytest.mark.asyncio
async def test_a_failed_run_does_not_block_a_later_successful_run(db_session):
    failing_exchange = FakeBinance(fail=True)
    with pytest.raises(ConnectionError):
        await candle_ingestion_job(db_session, failing_exchange)

    working_exchange = FakeBinance(num_candles=5, anchor_end=datetime.utcnow())
    stored = await candle_ingestion_job(db_session, working_exchange)
    assert stored == 5
    assert await _candle_count(db_session, "BTC/USDT", "4h") == 5


# ---- Custom symbol/timeframe/lookback are respected ----

@pytest.mark.asyncio
async def test_respects_explicit_symbol_and_timeframe(db_session):
    exchange = FakeBinance(num_candles=3, timeframe="1h")
    await candle_ingestion_job(db_session, exchange, symbol="ETH/USDT", timeframe="1h")
    assert await _candle_count(db_session, "ETH/USDT", "1h") == 3
    assert await _candle_count(db_session, "BTC/USDT", "4h") == 0
