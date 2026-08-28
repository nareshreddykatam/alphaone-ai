"""Individual scheduler job functions (Phase 5, sections 30-31). Each is a
plain async function taking a DB session + a CoinDCX account provider, so
they're independently testable with a fake provider and don't depend on
the runner/circuit-breaker machinery around them.

Job frequencies (documented, not arbitrary -- CoinDCX's futures API has no
published rate-limit table, see docs/coindcx_api_findings.md, so these
default to conservative values rather than guessing an aggressive one):
- account_sync_job: every 30s (balance + positions)
- exit_alert_job: every 30s (only meaningful once a live price exists)
- signal_generation_job: every 15 min for "4h" (the primary strategy runs
  on 4h bars; checking more often than that just re-evaluates the same
  bar) and, separately, every 15 min for "15m" (a 15m candle closes every
  15 minutes, so this is exactly one check per new bar -- see
  services/signal_engine/multi_strategy.py: currently every 15m strategy
  is RESEARCH_ONLY, so this tick is presently a no-op in production, but
  the plumbing exists so a future validated 15m strategy needs no new
  scheduler wiring, only a production_status flip)
- outcome_evaluation_job: every 15 min
- candle_ingestion_job: every 15 min (a 4h candle only closes every 4
  hours, so this is ~16 checks per real candle -- not aggressive; each
  tick is one lightweight Binance OHLCV call that writes nothing once the
  latest candle is already stored, and bounds how stale that candle can
  ever get to at most 15 minutes for /generate, the chart, and
  signal_generation_job's own same-cadence reads)
- live_breakout_job: every 30s (same cadence as account_sync/exit_alert --
  detects the SAME validated 4h Donchian+ADX breakout intrabar, via the
  existing live CoinDCX price feed, instead of waiting up to 15 minutes
  for signal_generation_job's next tick after the candle actually closes;
  see services/signal_engine/live_breakout.py for why this does not
  change the strategy itself)
"""
from datetime import datetime, timedelta
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import Candle, Signal
from sqlalchemy import select

from services.exchange.coindcx import CoinDCXReadOnlyAccountProvider
from services.exchange.coindcx_sync import sync_balance, sync_positions, sync_trade_fills
from services.market_data.binance import BinanceExchange
from services.market_data.coindcx_ws import CoinDCXMarketDataWebSocket
from services.market_data.ingestion import DataIngestionService
from services.position_monitor.monitor import get_new_exit_alerts
from services.signal_engine.live_breakout import LiveCandleAggregator, evaluate_live_breakout
from services.signal_engine.multi_strategy_engine import evaluate_all_strategies_for_timeframe
from services.signal_engine.notify import notify_new_signal
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


async def signal_generation_job(session: AsyncSession, symbol: str = "BTC/USDT", timeframe: str = "4h") -> list:
    """Evaluates every PRODUCTION_ELIGIBLE strategy registered for
    `timeframe` (services/signal_engine/multi_strategy.py) independently
    against real closed candles -- not just the single existing S05
    baseline anymore. Notifies Telegram for each newly-persisted signal:
    this is the ONLY path a CLOSED_CANDLE-only strategy (e.g. S06, which
    has no live_breakout-style intrabar detector) can ever reach Telegram
    through, so this call is not optional -- without it, such a strategy
    could persist a real signal and never alert anyone."""
    signals = await evaluate_all_strategies_for_timeframe(session, symbol, timeframe)
    for signal in signals:
        await notify_new_signal(session, signal)
    return signals


async def outcome_evaluation_job(session: AsyncSession, symbol: str = "BTC/USDT", timeframe: str = "4h") -> int:
    return await evaluate_pending_signal_outcomes(session, symbol=symbol, timeframe=timeframe)


async def ai_paper_trading_job(session: AsyncSession, symbol: str = "BTC/USDT", timeframe: str = "4h") -> list:
    """AI Trading V1, Phases 8/11/13: enriches every signal
    signal_generation_job just persisted this tick with AI evidence
    (services/signal_engine/ai_orchestrator.py -- regime, expected
    volatility, and a calibrated model probability ONLY if a validated
    model has actually been deployed), then feeds each qualifying
    decision to the process-wide paper trader
    (services/paper_trader/live_state.py). Returns the AIDecision dicts
    for any position NEWLY opened this tick, for Telegram.

    Deliberately called AFTER signal_generation_job in the same tick
    (see services/scheduler/runner.py) rather than as an independently-
    scheduled loop, specifically to avoid a read-after-write race: this
    function reads back the Signal rows signal_generation_job just
    committed, so it must run strictly after that commit, not on its own
    clock. Position management (SL/TP1/TP2/TP3 checks on already-open
    paper positions) runs on EVERY call regardless of whether a new
    signal fired or the model-health gate below allows a NEW position --
    a degraded model is never a reason to stop managing risk on an
    already-open paper trade.
    """
    from services.model_monitor.monitor import evaluate_model_health, ModelHealthStatus
    from services.paper_trader.live_state import paper_trader
    from services.paper_trader.persistence import persist_paper_open, persist_paper_event
    from services.signal_engine.ai_orchestrator import enrich_signal_with_ai_evidence
    from services.signal_engine.live_signal import _load_recent_candles, DEFAULT_LOOKBACK_BARS, MIN_BARS_REQUIRED

    df = await _load_recent_candles(session, symbol, timeframe, DEFAULT_LOOKBACK_BARS)
    if len(df) < MIN_BARS_REQUIRED:
        return []  # not enough real data yet -- never fabricate a decision

    last_candle = df.iloc[-1]
    for event in paper_trader.process_candle(last_candle):
        await persist_paper_event(session, event)

    health = await evaluate_model_health(session)
    if health.status == ModelHealthStatus.DISABLED:
        logger.warning("AI paper trading: new positions refused this tick", reasons=health.reasons)
        return []

    result = await session.execute(
        select(Signal).where(
            Signal.symbol == symbol, Signal.timeframe == timeframe,
            Signal.timestamp == last_candle["timestamp"], Signal.signal_type.in_(["LONG", "SHORT"]),
        )
    )
    signals = list(result.scalars().all())

    opened = []
    for signal in signals:
        decision = await enrich_signal_with_ai_evidence(session, df, signal, symbol=symbol)
        position = paper_trader.open_position(signal, current_price=float(last_candle["close"]))
        if position is not None:
            await persist_paper_open(session, position, symbol=symbol)
            opened.append(decision)
    return opened


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


async def live_breakout_job(
    session: AsyncSession,
    market_ws: CoinDCXMarketDataWebSocket,
    aggregator: LiveCandleAggregator,
    symbol: str = "BTC/USDT",
    timeframe: str = "4h",
) -> Optional[Signal]:
    """Evaluates the unmodified 4h Donchian+ADX strategy intrabar (see
    services/signal_engine/live_breakout.py) and, only for a genuinely new
    breakout, notifies exactly like the closed-candle path does
    (notify_new_signal -- same Telegram template, same NotificationLog
    dedup, same INR conversion)."""
    signal = await evaluate_live_breakout(session, market_ws, aggregator, symbol=symbol, timeframe=timeframe)
    if signal is not None:
        await notify_new_signal(session, signal)
    return signal
