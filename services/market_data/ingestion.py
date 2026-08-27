import asyncio
from datetime import datetime, timedelta
from typing import Optional
import structlog
from sqlalchemy import select, func
from sqlalchemy.dialects import sqlite as sqlite_dialect, postgresql as postgresql_dialect
from sqlalchemy.ext.asyncio import AsyncSession

from services.market_data import (
    ExchangeBase, OHLCV, FundingRate, OpenInterest, Liquidation, ExchangeCapabilityUnsupported,
)
from database.schema.models import Candle, FundingRate as FundingRateRow, OpenInterestRecord, LiquidationEvent

logger = structlog.get_logger()

TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]
SYMBOL = "BTC/USDT"

TIMEFRAME_TO_TIMEDELTA: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "1w": timedelta(weeks=1),
}


def _insert_ignoring_conflicts(dialect_name: str, table, rows: list[dict]):
    """Build a bulk INSERT ... ON CONFLICT DO NOTHING statement for the given rows.

    Relies on the unique index already defined on the table (symbol/timeframe/timestamp
    or symbol/timestamp) to make repeated backfills idempotent without a per-row SELECT.
    """
    if dialect_name == "postgresql":
        stmt = postgresql_dialect.insert(table).values(rows)
        return stmt.on_conflict_do_nothing()
    stmt = sqlite_dialect.insert(table).values(rows)
    return stmt.on_conflict_do_nothing()


class DataIngestionService:
    def __init__(self, exchange: ExchangeBase, db: AsyncSession):
        self.exchange = exchange
        self.db = db

    async def _dialect_name(self) -> str:
        bind = self.db.get_bind()
        return bind.dialect.name

    async def _get_max_candle_timestamp(self, symbol: str, timeframe: str) -> Optional[datetime]:
        stmt = select(func.max(Candle.timestamp)).where(
            Candle.symbol == symbol, Candle.timeframe == timeframe
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_min_candle_timestamp(self, symbol: str, timeframe: str) -> Optional[datetime]:
        stmt = select(func.min(Candle.timestamp)).where(
            Candle.symbol == symbol, Candle.timeframe == timeframe
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def fetch_and_store_candles(
        self, symbol: str, timeframe: str, since: Optional[datetime] = None, limit: int = 1000
    ) -> int:
        """Fetch a single page of candles and store any not already present."""
        candles = await self.exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        return await self._bulk_insert_candles(symbol, timeframe, candles)

    async def _bulk_insert_candles(self, symbol: str, timeframe: str, candles: list[OHLCV]) -> int:
        if not candles:
            return 0
        dialect_name = await self._dialect_name()
        now = datetime.utcnow()
        rows = [
            {
                "timestamp": c.timestamp,
                "timeframe": c.timeframe,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
                "symbol": c.symbol,
                "source": "binance",
                "ingested_at": now,
                "quality_status": "valid",
            }
            for c in candles
        ]
        stmt = _insert_ignoring_conflicts(dialect_name, Candle.__table__, rows)
        result = await self.db.execute(stmt)
        await self.db.commit()
        stored = result.rowcount if result.rowcount is not None and result.rowcount >= 0 else len(rows)
        logger.info("Candles stored", symbol=symbol, timeframe=timeframe, count=stored, fetched=len(candles))
        return stored

    async def _backfill_range(
        self, symbol: str, timeframe: str, range_start: datetime, range_end: datetime, page_limit: int,
    ) -> int:
        """Paginate fetch_ohlcv forward across exactly [range_start, range_end].

        Never persists a still-forming (not yet closed) candle as though it
        were completed historical data: ccxt/Binance commonly return the
        currently-in-progress candle as the last page entry once the
        requested window reaches "now" (observed live during the live-price
        audit -- the most recently stored 4h candle disagreed with the
        independently-computed live forming candle for the same bucket).
        Generic across every timeframe via TIMEFRAME_TO_TIMEDELTA -- no
        `if timeframe == "4h"` special-casing. For a fully historical range
        (range_end already in the past) this filter is always a no-op,
        since every returned candle's bucket has already closed.
        """
        interval = TIMEFRAME_TO_TIMEDELTA.get(timeframe, timedelta(minutes=1))
        since = range_start
        total_stored = 0
        consecutive_empty_pages = 0

        while since < range_end:
            candles = await self.exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=page_limit)
            candles = [c for c in candles if range_start <= c.timestamp <= range_end]

            if not candles:
                consecutive_empty_pages += 1
                if consecutive_empty_pages >= 2:
                    logger.info(
                        "Backfill stopping: exchange returned no further candles",
                        symbol=symbol, timeframe=timeframe, since=since,
                    )
                    break
                since += interval * page_limit
                continue

            now = datetime.utcnow()
            complete = [c for c in candles if c.timestamp + interval <= now]

            if not complete:
                # Only a still-forming candle was returned -- nothing safe to
                # store yet. Stop WITHOUT advancing `since` past it, so the
                # next backfill() call (e.g. the next candle_ingestion_job
                # tick) re-fetches this exact bucket once it has actually
                # closed, rather than skipping it forever.
                logger.info(
                    "Backfill reached the live edge -- newest candle not yet closed, stopping",
                    symbol=symbol, timeframe=timeframe, since=since,
                )
                break

            consecutive_empty_pages = 0
            stored = await self._bulk_insert_candles(symbol, timeframe, complete)
            total_stored += stored

            last_ts = complete[-1].timestamp
            if last_ts < since:
                logger.warning("Backfill made no forward progress, stopping", symbol=symbol, timeframe=timeframe)
                break
            since = last_ts + interval

            await asyncio.sleep(0)

        return total_stored

    async def backfill(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: Optional[datetime] = None,
        page_limit: int = 1000,
    ) -> int:
        """Backfill [start, end], resuming from whatever is already stored so
        repeated/interrupted runs never re-fetch or duplicate data.

        Handles two distinct gaps around any existing data, not just "pick
        up where we left off": if `start` is EARLIER than the earliest
        candle already stored (e.g. a small recent window was ingested
        before a full historical backfill was requested), that earlier
        historical gap is filled first; then any gap between the existing
        latest candle and `end` is filled forward as before. A naive
        "resume from max timestamp" alone would silently skip the entire
        historical range in that first scenario.
        """
        end = end or datetime.utcnow()
        interval = TIMEFRAME_TO_TIMEDELTA.get(timeframe, timedelta(minutes=1))
        existing_min = await self._get_min_candle_timestamp(symbol, timeframe)
        existing_max = await self._get_max_candle_timestamp(symbol, timeframe)

        total_stored = 0

        if existing_min is not None and start < existing_min:
            total_stored += await self._backfill_range(
                symbol, timeframe, start, existing_min - interval, page_limit,
            )

        forward_start = start
        if existing_max is not None and existing_max + interval > forward_start:
            forward_start = existing_max + interval

        if forward_start < end:
            total_stored += await self._backfill_range(symbol, timeframe, forward_start, end, page_limit)

        logger.info("Backfill complete", symbol=symbol, timeframe=timeframe, total_stored=total_stored)
        return total_stored

    async def fetch_all_timeframes(
        self, symbol: str = SYMBOL, since: Optional[datetime] = None, limit: int = 1000
    ) -> dict[str, int]:
        results = {}
        for tf in TIMEFRAMES:
            count = await self.fetch_and_store_candles(symbol, tf, since=since, limit=limit)
            results[tf] = count
        return results

    async def backfill_all_timeframes(
        self, symbol: str, start: datetime, end: Optional[datetime] = None,
        timeframes: Optional[list[str]] = None,
    ) -> dict[str, int]:
        results = {}
        for tf in timeframes or TIMEFRAMES:
            results[tf] = await self.backfill(symbol, tf, start, end)
        return results

    async def _backfill_funding_range(
        self, symbol: str, range_start: datetime, range_end: datetime, page_limit: int,
    ) -> int:
        dialect_name = await self._dialect_name()
        since = range_start
        total_stored = 0
        consecutive_empty_pages = 0

        while since < range_end:
            history = await self.exchange.fetch_funding_rate_history(symbol, since=since, limit=page_limit)
            history = [h for h in history if range_start <= h.timestamp <= range_end]

            if not history:
                consecutive_empty_pages += 1
                if consecutive_empty_pages >= 2:
                    break
                since += timedelta(hours=8) * page_limit
                continue

            consecutive_empty_pages = 0
            rows = [
                {"symbol": h.symbol, "timestamp": h.timestamp, "rate": h.rate, "source": "binance"}
                for h in history
            ]
            stmt_insert = _insert_ignoring_conflicts(dialect_name, FundingRateRow.__table__, rows)
            res = await self.db.execute(stmt_insert)
            await self.db.commit()
            total_stored += res.rowcount if res.rowcount and res.rowcount >= 0 else len(rows)

            last_ts = history[-1].timestamp
            if last_ts < since:
                break
            # Step by a full funding interval, not a small epsilon: near the
            # end of a requested range, the range filter above can leave only
            # ONE surviving record per page (whichever record landed exactly
            # at `since`), so stepping by e.g. 1 second instead of a real
            # interval would crawl forward one second at a time -- in the
            # worst case tens of thousands of redundant calls before it ever
            # reaches `range_end`.
            since = last_ts + timedelta(hours=8)

        return total_stored

    async def backfill_funding_rates(
        self, symbol: str, start: datetime, end: Optional[datetime] = None, page_limit: int = 1000,
    ) -> int:
        """Same historical-gap-aware resume logic as backfill() (candles):
        fills any gap before the earliest stored funding record first, then
        continues forward from the latest stored record -- a plain
        resume-from-max would silently skip history older than whatever
        happened to be ingested first."""
        end = end or datetime.utcnow()

        min_stmt = select(func.min(FundingRateRow.timestamp)).where(FundingRateRow.symbol == symbol)
        existing_min = (await self.db.execute(min_stmt)).scalar_one_or_none()
        max_stmt = select(func.max(FundingRateRow.timestamp)).where(FundingRateRow.symbol == symbol)
        existing_max = (await self.db.execute(max_stmt)).scalar_one_or_none()

        total_stored = 0

        if existing_min is not None and start < existing_min:
            total_stored += await self._backfill_funding_range(
                symbol, start, existing_min - timedelta(seconds=1), page_limit,
            )

        forward_start = start
        if existing_max is not None and existing_max + timedelta(seconds=1) > forward_start:
            forward_start = existing_max + timedelta(seconds=1)

        if forward_start < end:
            total_stored += await self._backfill_funding_range(symbol, forward_start, end, page_limit)

        logger.info("Funding rate backfill complete", symbol=symbol, total_stored=total_stored)
        return total_stored

    async def backfill_open_interest(
        self, symbol: str, start: datetime, end: Optional[datetime] = None,
        timeframe: str = "1h", page_limit: int = 500,
    ) -> int:
        """Binance only serves ~30 days of open-interest history via the public
        API regardless of `start` -- this is a documented exchange limitation,
        not a bug in this ingestion path. See docs/known_limitations.md.
        """
        end = end or datetime.utcnow()
        dialect_name = await self._dialect_name()
        history = await self.exchange.fetch_open_interest_history(symbol, timeframe=timeframe, since=start, limit=page_limit)
        history = [h for h in history if start <= h.timestamp <= end]
        if not history:
            return 0
        rows = [
            {"symbol": h.symbol, "timestamp": h.timestamp, "value": h.value, "source": "binance"}
            for h in history
        ]
        stmt = _insert_ignoring_conflicts(dialect_name, OpenInterestRecord.__table__, rows)
        result = await self.db.execute(stmt)
        await self.db.commit()
        stored = result.rowcount if result.rowcount and result.rowcount >= 0 else len(rows)
        logger.info("Open interest backfill complete", symbol=symbol, total_stored=stored, note="binance limits OI history to ~30 days")
        return stored

    async def fetch_recent_liquidations(self, symbol: str, limit: int = 100) -> int:
        """Best-effort recent-liquidation snapshot -- Binance has no public
        historical-liquidation backfill endpoint, so this can only ever cover
        a recent window, never a full historical dataset. Callers/reports must
        label liquidation coverage as recent-only, not silently treat it as
        complete history.
        """
        try:
            liquidations = await self.exchange.fetch_liquidations(symbol, limit=limit)
        except ExchangeCapabilityUnsupported as e:
            logger.warning("Liquidations unavailable from this exchange, skipping", symbol=symbol, error=str(e))
            return 0
        if not liquidations:
            return 0
        dialect_name = await self._dialect_name()
        rows = [
            {
                "symbol": l.symbol, "timestamp": l.timestamp, "side": l.side,
                "price": l.price, "quantity": l.quantity, "source": "binance",
            }
            for l in liquidations
        ]
        stmt = _insert_ignoring_conflicts(dialect_name, LiquidationEvent.__table__, rows)
        result = await self.db.execute(stmt)
        await self.db.commit()
        stored = result.rowcount if result.rowcount and result.rowcount >= 0 else len(rows)
        logger.info("Liquidations stored (recent-only, not a historical backfill)", symbol=symbol, count=stored)
        return stored

    async def get_stored_candles(
        self, symbol: str, timeframe: str, start: Optional[datetime] = None, end: Optional[datetime] = None
    ) -> list[OHLCV]:
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
        return [
            OHLCV(
                timestamp=r.timestamp, open=r.open, high=r.high, low=r.low,
                close=r.close, volume=r.volume, timeframe=r.timeframe, symbol=r.symbol,
            )
            for r in rows
        ]

    async def get_latest_candle(self, symbol: str, timeframe: str) -> Optional[OHLCV]:
        stmt = (
            select(Candle)
            .where(Candle.symbol == symbol, Candle.timeframe == timeframe)
            .order_by(Candle.timestamp.desc())
            .limit(1)
        )
        result = await self.db.execute(stmt)
        r = result.scalar_one_or_none()
        if r is None:
            return None
        return OHLCV(
            timestamp=r.timestamp, open=r.open, high=r.high, low=r.low,
            close=r.close, volume=r.volume, timeframe=r.timeframe, symbol=r.symbol,
        )
