"""Generates one signal from the most recently ingested real candles and
persists it (Phase 4 has no live scheduler yet -- this is invoked on demand,
e.g. POST /api/v1/signals/generate, or by a future scheduled job). Returns
None rather than fabricating anything when there isn't enough real data.
"""
from typing import Optional
import uuid

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import Candle, Signal, SignalOutcome, SignalOutcomeType, BotState
from services.signal_engine.regime import MarketRegimeDetector
from services.signal_engine.strategy import SignalStrategy, BaselineStrategy

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
    regime = MarketRegimeDetector().detect(df)

    risk = None
    rr = None
    if result.entry_price is not None and result.stop_loss is not None and result.take_profit_1 is not None:
        risk = abs(result.entry_price - result.stop_loss)
        rr = round(abs(result.take_profit_1 - result.entry_price) / risk, 2) if risk else None

    signal = Signal(
        signal_id=f"SIG-{uuid.uuid4().hex[:12].upper()}",
        timestamp=df.iloc[-1]["timestamp"],
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
        reasoning=result.reasoning,
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
