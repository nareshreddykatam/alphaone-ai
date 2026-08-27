"""Integration tests for the ingestion/backfill path against a real (in-memory)
SQLite database -- covers resumability and dedup end to end, not just the
pure functions in isolation. Retry-on-transient-failure behavior is specific
to BinanceExchange (services/market_data/binance.py) and is tested
separately in tests/unit/test_binance_exchange.py against a mocked ccxt
client, since the generic ExchangeBase/DataIngestionService layer does not
itself implement retries -- that is left to each concrete exchange.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema import models  # noqa: F401 -- ensure models are registered
from services.market_data import ExchangeBase, OHLCV
from services.market_data.ingestion import DataIngestionService, TIMEFRAME_TO_TIMEDELTA


class FlakyExchange(ExchangeBase):
    """Fails the first N calls, then succeeds -- exercises a caller that
    does its own error handling on top of DataIngestionService."""

    def __init__(self, fail_times: int = 0, pages: int = 1):
        self.fail_times = fail_times
        self.calls = 0
        self.pages = pages

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1000):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("simulated transient network failure")
        page_index = self.calls - self.fail_times - 1
        if page_index >= self.pages:
            return []
        interval = TIMEFRAME_TO_TIMEDELTA.get(timeframe, timedelta(minutes=1))
        base = since or datetime(2024, 1, 1)
        return [
            OHLCV(timestamp=base + interval * i, open=1, high=2, low=0.5, close=1.5,
                  volume=10, timeframe=timeframe, symbol=symbol)
            for i in range(50)
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


@pytest.mark.asyncio
async def test_resumable_backfill_produces_no_duplicates_or_gaps(db_session):
    start = datetime(2024, 1, 1)
    end = start + timedelta(minutes=150)

    exchange1 = FlakyExchange(pages=3)
    svc1 = DataIngestionService(exchange1, db_session)
    first_pass = await svc1.backfill("BTC/USDT", "1m", start, end, page_limit=50)
    assert first_pass > 0

    # simulate a fresh process resuming the same backfill
    exchange2 = FlakyExchange(pages=3)
    svc2 = DataIngestionService(exchange2, db_session)
    second_pass = await svc2.backfill("BTC/USDT", "1m", start, end, page_limit=50)
    assert second_pass == 0, "resumed backfill should not re-store any candles"

    candles = await svc2.get_stored_candles("BTC/USDT", "1m")
    timestamps = [c.timestamp for c in candles]
    assert len(timestamps) == len(set(timestamps)), "no duplicate timestamps should be stored"
    assert timestamps == sorted(timestamps)


@pytest.mark.asyncio
async def test_backfill_fills_an_earlier_historical_gap_not_just_forward(db_session):
    """If a small recent window was already ingested (e.g. an earlier test
    download) before a full historical backfill is requested, resuming
    naively from the existing max timestamp would silently skip the entire
    historical range before that window. backfill() must detect and fill
    that earlier gap too."""
    exchange = FlakyExchange(pages=100)
    svc = DataIngestionService(exchange, db_session)

    island_start = datetime(2024, 1, 20)
    await svc.backfill("BTC/USDT", "1h", island_start, island_start + timedelta(hours=5), page_limit=1000)

    full_start = datetime(2024, 1, 1)
    full_end = datetime(2024, 1, 25)
    await svc.backfill("BTC/USDT", "1h", full_start, full_end, page_limit=1000)

    candles = await svc.get_stored_candles("BTC/USDT", "1h")
    timestamps = [c.timestamp for c in candles]
    expected_count = int((full_end - full_start).total_seconds() / 3600) + 1

    assert len(timestamps) == len(set(timestamps))
    assert len(timestamps) == expected_count, "historical gap before the pre-existing window was not backfilled"
    assert timestamps[0] == full_start
    assert timestamps[-1] == full_end


@pytest.mark.asyncio
async def test_backfill_funding_rates_fills_earlier_historical_gap_too(db_session):
    """Same class of bug as the candle backfill gap, in the funding-rate
    path: a small recent funding window ingested before a full historical
    request must not cause the historical range to be silently skipped."""

    class FundingExchange(ExchangeBase):
        def __init__(self):
            self.calls = 0

        async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1000):
            return []

        async def fetch_funding_rate(self, symbol):
            raise NotImplementedError

        async def fetch_funding_rate_history(self, symbol, since=None, limit=1000):
            self.calls += 1
            from services.market_data import FundingRate
            base = since or datetime(2024, 1, 1)
            out = []
            for i in range(min(limit, 50)):
                out.append(FundingRate(timestamp=base + timedelta(hours=8 * i), rate=0.0001, symbol=symbol))
            return out

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

    exchange = FundingExchange()
    svc = DataIngestionService(exchange, db_session)

    island_start = datetime(2024, 3, 1)
    await svc.backfill_funding_rates("BTC/USDT", island_start, island_start + timedelta(hours=8 * 3))

    full_start = datetime(2024, 1, 1)
    full_end = datetime(2024, 3, 10)
    await svc.backfill_funding_rates("BTC/USDT", full_start, full_end)

    from sqlalchemy import select as sa_select
    from database.schema.models import FundingRate as FundingRateRow
    rows = (await db_session.execute(
        sa_select(FundingRateRow).where(FundingRateRow.symbol == "BTC/USDT").order_by(FundingRateRow.timestamp)
    )).scalars().all()
    timestamps = [r.timestamp for r in rows]

    assert timestamps[0] <= full_start + timedelta(hours=8)
    assert len(timestamps) == len(set(timestamps))
    # confirm the historical range before the island is actually present
    assert any(ts < island_start for ts in timestamps), "historical funding gap before the pre-existing island was not backfilled"


@pytest.mark.asyncio
async def test_a_failing_exchange_call_propagates_rather_than_looking_like_empty_data(db_session):
    """DataIngestionService must not swallow an exchange error into an
    empty-looking result -- a caller needs to be able to distinguish
    "genuinely no data" from "the call failed"."""
    exchange = FlakyExchange(fail_times=10, pages=1)
    svc = DataIngestionService(exchange, db_session)
    with pytest.raises(ConnectionError):
        await svc.fetch_and_store_candles("BTC/USDT", "1m", since=datetime(2024, 1, 1))
