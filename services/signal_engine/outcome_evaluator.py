"""Resolves PENDING SignalOutcome rows against real subsequent candles
(Phase 4J) -- this is what makes "AlphaOne Signal Performance" (services/
portfolio/service.py) eventually show real numbers instead of staying at
zero forever. A signal is WIN if its take-profit is touched before its
stop-loss, LOSS the other way round, and EXPIRED if neither happens within
`max_horizon_bars`. Same-bar-touches-both convention as the backtester
(docs/execution_semantics.md): stop-loss wins ties, since OHLC data can't
actually disambiguate intrabar order -- this is the conservative choice,
consistently applied everywhere in this codebase.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import Candle, Signal, SignalOutcome, SignalOutcomeType

DEFAULT_MAX_HORIZON_BARS = 30


async def _candles_after(session: AsyncSession, symbol: str, timeframe: str, after: datetime, limit: int) -> list[Candle]:
    result = await session.execute(
        select(Candle)
        .where(
            Candle.symbol == symbol, Candle.timeframe == timeframe,
            Candle.timestamp > after, Candle.quality_status == "valid",
        )
        .order_by(Candle.timestamp)
        .limit(limit)
    )
    return list(result.scalars().all())


def _resolve(signal: Signal, candles: list[Candle], max_horizon_bars: int) -> Optional[dict]:
    """Returns a resolution dict, or None if not enough time/data has
    passed yet to decide (stays PENDING)."""
    if signal.entry_price is None or signal.stop_loss is None or signal.take_profit_1 is None:
        return None

    for candle in candles[:max_horizon_bars]:
        if signal.signal_type == "LONG":
            hit_stop = candle.low <= signal.stop_loss
            hit_target = candle.high >= signal.take_profit_1
        else:  # SHORT
            hit_stop = candle.high >= signal.stop_loss
            hit_target = candle.low <= signal.take_profit_1

        if hit_stop:  # stop wins ties -- conservative, see module docstring
            pnl_pct = (
                (signal.stop_loss - signal.entry_price) / signal.entry_price * 100
                if signal.signal_type == "LONG"
                else (signal.entry_price - signal.stop_loss) / signal.entry_price * 100
            )
            return {"outcome": SignalOutcomeType.LOSS.value, "exit_price": signal.stop_loss, "pnl_pct": pnl_pct, "evaluated_at": candle.timestamp}
        if hit_target:
            pnl_pct = (
                (signal.take_profit_1 - signal.entry_price) / signal.entry_price * 100
                if signal.signal_type == "LONG"
                else (signal.entry_price - signal.take_profit_1) / signal.entry_price * 100
            )
            return {"outcome": SignalOutcomeType.WIN.value, "exit_price": signal.take_profit_1, "pnl_pct": pnl_pct, "evaluated_at": candle.timestamp}

    if len(candles) >= max_horizon_bars:
        return {"outcome": SignalOutcomeType.EXPIRED.value, "exit_price": None, "pnl_pct": None, "evaluated_at": candles[max_horizon_bars - 1].timestamp}

    return None  # not enough bars yet to decide either way


async def evaluate_pending_signal_outcomes(
    session: AsyncSession, symbol: str = "BTC/USDT", timeframe: str = "4h",
    max_horizon_bars: int = DEFAULT_MAX_HORIZON_BARS,
) -> int:
    """Evaluates every resolvable PENDING outcome. Returns the count
    updated. Signals with signal_type NO_TRADE never reach PENDING (see
    services/signal_engine/live_signal.py), so this only ever touches real
    LONG/SHORT signals."""
    result = await session.execute(
        select(SignalOutcome, Signal)
        .join(Signal, Signal.signal_id == SignalOutcome.signal_id)
        .where(SignalOutcome.outcome == SignalOutcomeType.PENDING.value, Signal.symbol == symbol)
    )
    pending = result.all()

    updated = 0
    for outcome, signal in pending:
        candles = await _candles_after(session, symbol, timeframe, signal.timestamp, max_horizon_bars)
        resolution = _resolve(signal, candles, max_horizon_bars)
        if resolution is None:
            continue

        outcome.outcome = resolution["outcome"]
        outcome.hypothetical_entry_price = signal.entry_price
        outcome.hypothetical_exit_price = resolution["exit_price"]
        outcome.hypothetical_pnl_pct = resolution["pnl_pct"]
        outcome.evaluated_at = resolution["evaluated_at"]
        updated += 1

    if updated:
        await session.commit()
    return updated
