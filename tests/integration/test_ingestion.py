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


# ---- Incomplete (still-forming) candle rejection, generic across every
# supported timeframe -- see services/market_data/ingestion.py's
# _backfill_range docstring. A real bug found during the live-price audit:
# ccxt/Binance can return the currently-in-progress candle as the last page
# entry once the requested window reaches "now"; that candle must never be
# persisted as if it were a completed historical bar. ----

class _FixedCandlesExchange(ExchangeBase):
    """Returns exactly the candles it was constructed with (never an
    infinite generator), filtered by `since` -- lets a test control
    precisely which candles (complete and/or still-forming) the "exchange"
    hands back for a given backfill() call."""

    def __init__(self, candles: list[OHLCV]):
        self._candles = candles

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1000):
        cs = [c for c in self._candles if since is None or c.timestamp >= since]
        return cs[:limit]

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


class _FrozenClock(datetime):
    """A real datetime subclass (so type hints/arithmetic in ingestion.py
    keep working unmodified) whose .utcnow() returns a controllable,
    class-level value -- lets a test simulate time passing (a still-
    forming candle's bucket actually closing) deterministically, without a
    real sleep."""
    _now = datetime(2024, 1, 1)

    @classmethod
    def utcnow(cls):
        return cls._now


@pytest.mark.asyncio
@pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "1h", "4h", "1d"])
async def test_incomplete_current_candle_is_not_stored_as_completed(db_session, monkeypatch, timeframe):
    interval = TIMEFRAME_TO_TIMEDELTA[timeframe]
    frozen_now = datetime(2024, 6, 1, 12, 0, 0)
    monkeypatch.setattr(_FrozenClock, "_now", frozen_now)
    monkeypatch.setattr("services.market_data.ingestion.datetime", _FrozenClock)

    def _mk(ts):
        return OHLCV(timestamp=ts, open=1, high=2, low=0.5, close=1.5, volume=10, timeframe=timeframe, symbol="BTC/USDT")

    complete = [_mk(frozen_now - interval * n) for n in (3, 2, 1)]
    forming = _mk(frozen_now)  # close time == frozen_now + interval, strictly after "now" -- not yet closed

    exchange = _FixedCandlesExchange(complete + [forming])
    svc = DataIngestionService(exchange, db_session)
    stored = await svc.backfill("BTC/USDT", timeframe, frozen_now - interval * 10, frozen_now, page_limit=1000)

    assert stored == 3, "only the 3 already-closed candles should be stored"
    saved = await svc.get_stored_candles("BTC/USDT", timeframe)
    saved_timestamps = {c.timestamp for c in saved}
    assert forming.timestamp not in saved_timestamps, "the still-forming candle must never be persisted as completed"
    assert {c.timestamp for c in complete} <= saved_timestamps


@pytest.mark.asyncio
@pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "1h", "4h", "1d"])
async def test_previously_incomplete_candle_is_stored_once_its_bucket_actually_closes(db_session, monkeypatch, timeframe):
    """The candle skipped as "still forming" on one backfill() call must be
    picked up and stored on a LATER call once real time has actually
    advanced past its close boundary -- proving `since` is never advanced
    past an incomplete candle (which would silently skip it forever)."""
    interval = TIMEFRAME_TO_TIMEDELTA[timeframe]
    frozen_now = datetime(2024, 6, 1, 12, 0, 0)
    monkeypatch.setattr(_FrozenClock, "_now", frozen_now)
    monkeypatch.setattr("services.market_data.ingestion.datetime", _FrozenClock)

    def _mk(ts):
        return OHLCV(timestamp=ts, open=1, high=2, low=0.5, close=1.5, volume=10, timeframe=timeframe, symbol="BTC/USDT")

    forming_then = _mk(frozen_now)
    exchange = _FixedCandlesExchange([forming_then])
    svc = DataIngestionService(exchange, db_session)

    first = await svc.backfill("BTC/USDT", timeframe, frozen_now - interval, frozen_now, page_limit=1000)
    assert first == 0

    # Real time advances past the candle's close boundary.
    later = frozen_now + interval
    monkeypatch.setattr(_FrozenClock, "_now", later)
    exchange2 = _FixedCandlesExchange([forming_then])
    svc2 = DataIngestionService(exchange2, db_session)
    second = await svc2.backfill("BTC/USDT", timeframe, frozen_now - interval, later, page_limit=1000)

    assert second == 1, "the candle must be re-fetched and stored now that it has actually closed"
    saved = await svc2.get_stored_candles("BTC/USDT", timeframe)
    assert forming_then.timestamp in {c.timestamp for c in saved}


@pytest.mark.asyncio
async def test_fully_historical_backfill_is_unaffected_by_the_completeness_filter(db_session, monkeypatch):
    """When range_end is already fully in the past, every returned candle's
    bucket has necessarily already closed -- the completeness filter must
    be a complete no-op there, dropping nothing."""
    frozen_now = datetime(2026, 1, 1)
    monkeypatch.setattr(_FrozenClock, "_now", frozen_now)
    monkeypatch.setattr("services.market_data.ingestion.datetime", _FrozenClock)

    start = datetime(2024, 1, 1)
    candles = [
        OHLCV(timestamp=start + timedelta(minutes=i), open=1, high=2, low=0.5, close=1.5,
              volume=10, timeframe="1m", symbol="BTC/USDT")
        for i in range(10)
    ]
    exchange = _FixedCandlesExchange(candles)
    svc = DataIngestionService(exchange, db_session)
    stored = await svc.backfill("BTC/USDT", "1m", start, start + timedelta(minutes=9), page_limit=1000)
    assert stored == 10  # every historical candle stored, none dropped as "incomplete"
