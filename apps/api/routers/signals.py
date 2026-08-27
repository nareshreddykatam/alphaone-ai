from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema import get_db
from database.schema.models import Signal, SignalOutcome
from services.exchange.fx import get_usdt_inr_rate, convert_usdt_to_inr, conversion_meta
from services.signal_engine.live_signal import generate_and_persist_signal
from services.signal_engine.outcome_evaluator import evaluate_pending_signal_outcomes
from services.signal_engine.notify import notify_new_signal

router = APIRouter()

# Signal price levels are computed by the Binance-sourced signal engine
# (Phases 1-3) and stored in USDT -- converted here to INR for display.
_USDT_PRICE_FIELDS = ("entry_price", "stop_loss", "take_profit_1", "take_profit_2", "take_profit_3")


def _serialize(signal: Signal, rate=None) -> dict:
    return {
        "signal_id": signal.signal_id, "timestamp": signal.timestamp, "symbol": signal.symbol,
        "signal_type": signal.signal_type, "entry_price": signal.entry_price, "stop_loss": signal.stop_loss,
        "take_profit_1": signal.take_profit_1, "take_profit_2": signal.take_profit_2,
        "take_profit_3": signal.take_profit_3, "risk_reward": signal.risk_reward,
        "market_regime": signal.market_regime, "reasoning": signal.reasoning,
        "quality": signal.quality, "strategy_name": signal.strategy_name,
        "model_version": signal.model_version, "is_active": signal.is_active,
        **{f"{field}_inr": convert_usdt_to_inr(getattr(signal, field), rate) for field in _USDT_PRICE_FIELDS},
        **conversion_meta(rate),
    }


@router.get("/")
async def get_signals(limit: int = 50, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Signal).order_by(Signal.timestamp.desc()).limit(limit))
    rows = result.scalars().all()
    rate = await get_usdt_inr_rate() if rows else None
    return {"signals": [_serialize(s, rate) for s in rows], "count": len(rows)}


@router.get("/latest")
async def get_latest_signal(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Signal).order_by(Signal.timestamp.desc()).limit(1))
    signal = result.scalar_one_or_none()
    rate = await get_usdt_inr_rate() if signal else None
    return {"signal": _serialize(signal, rate) if signal else None}


@router.get("/{signal_id}")
async def get_signal(signal_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Signal).where(Signal.signal_id == signal_id))
    signal = result.scalar_one_or_none()
    if signal is None:
        raise HTTPException(status_code=404, detail="signal not found")
    outcome = (await db.execute(select(SignalOutcome).where(SignalOutcome.signal_id == signal_id))).scalar_one_or_none()
    rate = await get_usdt_inr_rate()
    return {
        "signal": _serialize(signal, rate),
        "outcome": {
            "outcome": outcome.outcome, "hypothetical_pnl_pct": outcome.hypothetical_pnl_pct,
            "was_taken_by_user": outcome.was_taken_by_user,
        } if outcome else None,
    }


@router.post("/generate")
async def generate_signal(symbol: str = "BTC/USDT", timeframe: str = "4h", db: AsyncSession = Depends(get_db)):
    """On-demand research signal from the latest real candles (Phase 4 has
    no live scheduler yet). Never fabricates -- returns null if there isn't
    enough real data ingested for this symbol/timeframe."""
    signal = await generate_and_persist_signal(db, symbol=symbol, timeframe=timeframe)
    if signal is not None:
        await notify_new_signal(db, signal)
    rate = await get_usdt_inr_rate() if signal else None
    return {"signal": _serialize(signal, rate) if signal else None}


@router.post("/evaluate-outcomes")
async def evaluate_outcomes(symbol: str = "BTC/USDT", timeframe: str = "4h", db: AsyncSession = Depends(get_db)):
    """Resolves PENDING signal outcomes against real subsequent candles --
    this is what makes AlphaOne Signal Performance eventually show real
    win/loss numbers instead of staying at zero. Safe to call repeatedly;
    only newly-resolvable outcomes are updated."""
    updated = await evaluate_pending_signal_outcomes(db, symbol=symbol, timeframe=timeframe)
    return {"updated": updated}
