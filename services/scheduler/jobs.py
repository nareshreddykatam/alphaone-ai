"""Individual scheduler job functions (Phase 5, sections 30-31). Each is a
plain async function taking a DB session + a CoinDCX account provider, so
they're independently testable with a fake provider and don't depend on
the runner/circuit-breaker machinery around them.

Job frequencies (documented, not arbitrary -- CoinDCX's futures API has no
published rate-limit table, see docs/coindcx_api_findings.md, so these
default to conservative values rather than guessing an aggressive one):
- account_sync_job: every 30s (balance + positions)
- exit_alert_job: every 30s (only meaningful once a live price exists)
- signal_generation_job: every 15 min (the primary strategy runs on 4h
  bars; checking more often than that just re-evaluates the same bar)
- outcome_evaluation_job: every 15 min
- candle_ingestion_job: every 15 min (a 4h candle only closes every 4
  hours, so this is ~16 checks per real candle -- not aggressive; each
  tick is one lightweight Binance OHLCV call that writes nothing once the
  latest candle is already stored, and bounds how stale that candle can
  ever get to at most 15 minutes for /generate, the chart, and
  signal_generation_job's own same-cadence reads)
"""
from datetime import datetime, timedelta
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import Candle
from sqlalchemy import select

from services.exchange.coindcx import CoinDCXReadOnlyAccountProvider
from services.exchange.coindcx_sync import sync_balance, sync_positions, sync_trade_fills
from services.market_data.binance import BinanceExchange
from services.market_data.ingestion import DataIngestionService
from services.position_monitor.monitor import get_new_exit_alerts
from services.signal_engine.live_signal import generate_and_persist_signal
from services.signal_engine.outcome_evaluator import evaluate_pending_signal_outcomes

logger = structlog.get_logger()

CANDLE_INGESTION_SYMBOL = "BTC/USDT"
CANDLE_INGESTION_TIMEFRAME = "4h"
CANDLE_INGESTION_LOOKBACK_HOURS = 24


async def account_sync_job(session: AsyncSession, provider: CoinDCXReadOnlyAccountProvider) -> dict:
    balance = await sync_balance(session, provider)
    if balance["status"] != "OK":
        return {"balance": balance, "positions": None}
    positions = await sync_positions(session, provider)
    await sync_trade_fills(session, provider)
    return {"balance": balance, "positions": positions}


async def _latest_price(session: AsyncSession, symbol: str = "BTC/USDT") -> Optional[float]:
    result = await session.execute(
        select(Candle).where(Candle.symbol == symbol).order_by(Candle.timestamp.desc()).limit(1)
    )
    candle = result.scalar_one_or_none()
    return candle.close if candle else None


async def exit_alert_job(session: AsyncSession, symbol: str = "BTC/USDT", current_price: Optional[float] = None) -> list:
    """Never generates an alert against a price it can't source -- if no
    live/recent price is available, this is a no-op rather than a guess
    (Phase 5 section 32: don't act on stale/missing critical data)."""
    price = current_price if current_price is not None else await _latest_price(session, symbol)
    if price is None:
        return []
    return await get_new_exit_alerts(session, current_price=price, symbol=symbol)


async def signal_generation_job(session: AsyncSession, symbol: str = "BTC/USDT", timeframe: str = "4h"):
    return await generate_and_persist_signal(session, symbol=symbol, timeframe=timeframe)


async def outcome_evaluation_job(session: AsyncSession, symbol: str = "BTC/USDT", timeframe: str = "4h") -> int:
    return await evaluate_pending_signal_outcomes(session, symbol=symbol, timeframe=timeframe)


async def candle_ingestion_job(
    session: AsyncSession,
    exchange: BinanceExchange,
    symbol: str = CANDLE_INGESTION_SYMBOL,
    timeframe: str = CANDLE_INGESTION_TIMEFRAME,
    lookback_hours: int = CANDLE_INGESTION_LOOKBACK_HOURS,
) -> int:
    """Keeps the real-data Candle table topped up for the live /generate,
    chart, and signal_generation_job paths. Reuses
    DataIngestionService.backfill() unmodified -- it already resumes from
    MAX(Candle.timestamp) and inserts via ON CONFLICT DO NOTHING, so
    repeated ticks never duplicate rows or re-download history (see
    tests/integration/test_ingestion.py::
    test_resumable_backfill_produces_no_duplicates_or_gaps for that
    guarantee). `lookback_hours` is only a safety floor for `start=` (e.g.
    to fill a short gap after downtime) -- not a full re-backfill window;
    on every normal tick this is a no-op write once the newest real candle
    is already stored.
    """
    svc = DataIngestionService(exchange, session)
    end = datetime.utcnow()
    start = end - timedelta(hours=lookback_hours)
    return await svc.backfill(symbol, timeframe, start, end)
