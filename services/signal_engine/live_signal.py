"""Generates one signal from the most recently ingested real candles and
persists it. Invoked both on demand (POST /api/v1/signals/generate) and by
the scheduler's periodic signal_generation_job. Returns None rather than
fabricating anything when there isn't enough real data.

Dedup guard: before persisting a new LONG/SHORT signal, checks whether one
already exists for this EXACT (symbol, candle timestamp). Without this, a
15-minute scheduler re-run against a candle that hasn't closed yet (the
Donchian breakout condition is still true, nothing new happened) would
insert a fresh Signal row -- and since Telegram dedup
(services/signal_engine/notify.py) keys on signal_id, which is always a
new UUID, that would trigger a NEW Telegram alert every 15 minutes for the
same still-standing breakout. This guard makes "one signal per candle" a
real, restart-safe (DB-backed, not in-memory) invariant shared by both
this scheduled path and services/signal_engine/live_breakout.py's
intrabar path.
"""
from datetime import datetime
from typing import Optional
import uuid

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import Candle, Signal, SignalOutcome, SignalOutcomeType, BotState
from services.signal_engine.regime import MarketRegimeDetector
from services.signal_engine.strategy import SignalStrategy, BaselineStrategy, StrategySignal

DEFAULT_TIMEFRAME = "4h"
DEFAULT_LOOKBACK_BARS = 250
MIN_BARS_REQUIRED = 60
PAUSE_STATE_KEY = "signal_generation_paused"


async def is_signal_generation_paused(session: AsyncSession) -> bool:
    row = (await session.execute(select(BotState).where(BotState.key == PAUSE_STATE_KEY))).scalar_one_or_none()
    return bool(row.value.get("paused")) if row else False


async def set_signal_generation_paused(session: AsyncSession, paused: bool) -> None:
    row = (await session.execute(select(BotState).where(BotState.key == PAUSE_STATE_KEY))).scalar_one_or_none()
    if row is None:
        session.add(BotState(key=PAUSE_STATE_KEY, value={"paused": paused}))
    else:
        row.value = {"paused": paused}
    await session.commit()


async def _load_recent_candles(session: AsyncSession, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    result = await session.execute(
        select(Candle)
        .where(Candle.symbol == symbol, Candle.timeframe == timeframe, Candle.quality_status == "valid")
        .order_by(Candle.timestamp.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    rows.reverse()
    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    return pd.DataFrame(
        [
            {
                "timestamp": r.timestamp, "open": r.open, "high": r.high,
                "low": r.low, "close": r.close, "volume": r.volume,
            }
            for r in rows
        ]
    )


async def signal_already_exists_for_candle(session: AsyncSession, symbol: str, timestamp: datetime) -> bool:
    """The shared dedup guard: is there already a non-NO_TRADE Signal row
    for this exact (symbol, candle timestamp)? A real DB query, not
    in-memory state, so this is safe across process restarts and shared
    correctly between the scheduled (closed-candle) and live (intrabar)
    paths -- whichever one detects a breakout first "wins" and the other
    naturally no-ops rather than sending a duplicate alert."""
    result = await session.execute(
        select(Signal).where(
            Signal.symbol == symbol, Signal.timestamp == timestamp, Signal.signal_type != "NO_TRADE",
        )
    )
    return result.scalar_one_or_none() is not None


async def _persist_new_signal(
    session: AsyncSession,
    result: StrategySignal,
    symbol: str,
    timestamp: datetime,
    regime: str,
    reasoning: str,
) -> Signal:
    """Shared persistence, used by both the closed-candle
    (generate_and_persist_signal, below) and live/intrabar
    (services/signal_engine/live_breakout.py) paths -- one place that
    defines what a Signal row/its paired SignalOutcome look like."""
    risk = None
    rr = None
    if result.entry_price is not None and result.stop_loss is not None and result.take_profit_1 is not None:
        risk = abs(result.entry_price - result.stop_loss)
        rr = round(abs(result.take_profit_1 - result.entry_price) / risk, 2) if risk else None

    signal = Signal(
        signal_id=f"SIG-{uuid.uuid4().hex[:12].upper()}",
        timestamp=timestamp,
        symbol=symbol,
        signal_type=result.signal_type,
        confidence=0.0,  # deliberately unused for a rule-based strategy; see `quality`
        entry_price=result.entry_price,
        stop_loss=result.stop_loss,
        take_profit_1=result.take_profit_1,
        take_profit_2=result.take_profit_2,
        take_profit_3=result.take_profit_3,
        risk_reward=rr,
        market_regime=regime,
        reasoning=reasoning,
        is_active=result.signal_type != "NO_TRADE",
        quality=result.quality,
        strategy_name=result.strategy_name,
        model_version=result.model_version,
    )
    session.add(signal)
    await session.flush()

    session.add(
        SignalOutcome(
            signal_id=signal.signal_id,
            outcome=SignalOutcomeType.NO_TRADE.value if result.signal_type == "NO_TRADE" else SignalOutcomeType.PENDING.value,
            hypothetical_entry_price=result.entry_price,
        )
    )
    await session.commit()
    await session.refresh(signal)
    return signal


async def generate_and_persist_signal(
    session: AsyncSession,
    strategy: Optional[SignalStrategy] = None,
    symbol: str = "BTC/USDT",
    timeframe: str = DEFAULT_TIMEFRAME,
) -> Optional[Signal]:
    if await is_signal_generation_paused(session):
        return None

    strategy = strategy or BaselineStrategy()
    df = await _load_recent_candles(session, symbol, timeframe, DEFAULT_LOOKBACK_BARS)
    if len(df) < MIN_BARS_REQUIRED:
        return None  # not enough real data yet -- never fabricate a signal

    result = strategy.generate(df)
    candle_timestamp = df.iloc[-1]["timestamp"]

    if result.signal_type != "NO_TRADE" and await signal_already_exists_for_candle(session, symbol, candle_timestamp):
        return None  # this breakout was already alerted (scheduled or live) -- do not duplicate

    regime = MarketRegimeDetector().detect(df)
    return await _persist_new_signal(session, result, symbol, candle_timestamp, regime, result.reasoning)
