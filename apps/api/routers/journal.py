"""Manual trade tracking endpoints (Phase 4, sections 6 & 18). AlphaOne
never places, cancels, or modifies an order here -- every price/quantity is
exactly what the user reports after executing the trade themselves on
CoinDCX."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dataclasses import asdict

from database.schema import get_db
from database.schema.models import Trade, Candle
from services.portfolio.account import get_or_create_default_account
from services.position_monitor.monitor import get_new_exit_alerts
from services.risk_engine.state_store import load_risk_engine, save_risk_engine
from services.signal_matching.matcher import find_candidate_signals, pick_confident_match
from services.trade_journal.journal import (
    TradeJournalError,
    open_trade,
    record_exit,
    cancel_trade,
    set_signal_match,
)

router = APIRouter()


class OpenTradeIn(BaseModel):
    symbol: str = "BTC/USDT"
    side: str
    entry_price: float
    quantity: float
    entry_time: Optional[datetime] = None
    stop_loss: Optional[float] = None
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    take_profit_3: Optional[float] = None
    leverage: int = 1
    signal_id: Optional[str] = None
    matched_signal_confidence: Optional[float] = None


class ExitTradeIn(BaseModel):
    exit_price: float
    quantity: float
    timestamp: Optional[datetime] = None
    reason: Optional[str] = None
    note: Optional[str] = None


class CancelTradeIn(BaseModel):
    reason: Optional[str] = None


class ConfirmMatchIn(BaseModel):
    signal_id: str
    confidence: Optional[float] = None


def _serialize(trade: Trade) -> dict:
    return {
        "trade_id": trade.trade_id, "signal_id": trade.signal_id, "symbol": trade.symbol,
        "side": trade.side, "status": trade.status, "entry_price": trade.entry_price,
        "exit_price": trade.exit_price, "stop_loss": trade.stop_loss,
        "take_profit_1": trade.take_profit_1, "quantity": trade.quantity, "leverage": trade.leverage,
        "pnl": trade.pnl, "pnl_pct": trade.pnl_pct, "fees": trade.fees, "r_multiple": trade.r_multiple,
        "entry_time": trade.entry_time, "exit_time": trade.exit_time, "exit_reason": trade.exit_reason,
        "is_manual_entry": trade.is_manual_entry, "source": trade.source,
        "match_status": trade.match_status, "data_source": trade.data_source,
        "mark_price": trade.mark_price, "unrealized_pnl": trade.unrealized_pnl,
        "liquidation_price": trade.liquidation_price, "margin": trade.margin,
        "last_synced_at": trade.last_synced_at,
    }


@router.post("/open")
async def open_manual_trade(payload: OpenTradeIn, db: AsyncSession = Depends(get_db)):
    account = await get_or_create_default_account(db)
    entry_time = payload.entry_time or datetime.utcnow()

    signal_id = payload.signal_id
    confidence = payload.matched_signal_confidence
    match_candidates: list[dict] = []

    if signal_id is None:
        # Attempt auto-matching (Phase 4H) -- only ever auto-links when
        # confident; ambiguous candidates are returned for manual
        # confirmation via POST /{trade_id}/confirm-match, never guessed.
        candidates = await find_candidate_signals(
            db, symbol=payload.symbol, side=payload.side,
            entry_price=payload.entry_price, timestamp=entry_time,
        )
        match_candidates = [c.__dict__ for c in candidates]
        confident = pick_confident_match(candidates)
        if confident is not None:
            signal_id, confidence = confident.signal_id, confident.confidence

    try:
        trade = await open_trade(
            db, symbol=payload.symbol, side=payload.side, entry_price=payload.entry_price,
            quantity=payload.quantity, entry_time=entry_time,
            stop_loss=payload.stop_loss, take_profit_1=payload.take_profit_1,
            take_profit_2=payload.take_profit_2, take_profit_3=payload.take_profit_3,
            leverage=payload.leverage, signal_id=signal_id, account_id=account.id,
            matched_signal_confidence=confidence,
        )
    except TradeJournalError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = _serialize(trade)
    result["match_candidates"] = match_candidates if signal_id is None else []
    return result


@router.get("/{trade_id}/match-candidates")
async def get_match_candidates(trade_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Trade).where(Trade.trade_id == trade_id))
    trade = result.scalar_one_or_none()
    if trade is None:
        raise HTTPException(status_code=404, detail="trade not found")
    candidates = await find_candidate_signals(
        db, symbol=trade.symbol, side=trade.side, entry_price=trade.entry_price, timestamp=trade.entry_time,
    )
    return {"candidates": [c.__dict__ for c in candidates]}


@router.post("/{trade_id}/confirm-match")
async def confirm_match(trade_id: str, payload: ConfirmMatchIn, db: AsyncSession = Depends(get_db)):
    try:
        trade = await set_signal_match(db, trade_id=trade_id, signal_id=payload.signal_id, confidence=payload.confidence)
    except TradeJournalError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize(trade)


@router.post("/{trade_id}/exit")
async def exit_manual_trade(trade_id: str, payload: ExitTradeIn, db: AsyncSession = Depends(get_db)):
    try:
        trade = await record_exit(
            db, trade_id=trade_id, exit_price=payload.exit_price, quantity=payload.quantity,
            timestamp=payload.timestamp or datetime.utcnow(), reason=payload.reason, note=payload.note,
        )
    except TradeJournalError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Feed the risk dashboard (Phase 2.6 RiskEngine, informational only in
    # Phase 4 -- it never gates a manual trade) once a trade is fully closed.
    if trade.status == "CLOSED":
        risk_engine = await load_risk_engine(db)
        equity_relative_pnl_pct = (
            (trade.pnl or 0) / risk_engine.state.current_equity * 100
            if risk_engine.state.current_equity else 0.0
        )
        risk_engine.record_trade_result(equity_relative_pnl_pct, now=trade.exit_time)
        await save_risk_engine(db, risk_engine)

    return _serialize(trade)


@router.get("/exit-alerts")
async def exit_alerts(current_price: Optional[float] = None, symbol: str = "BTC/USDT", db: AsyncSession = Depends(get_db)):
    """Recommends exits for open manual positions whose stop-loss/take-
    profit would have been hit -- never closes anything itself. Falls back
    to the latest real ingested candle close when no price is supplied;
    never fabricates a price."""
    if current_price is None:
        result = await db.execute(select(Candle).where(Candle.symbol == symbol).order_by(Candle.timestamp.desc()).limit(1))
        candle = result.scalar_one_or_none()
        if candle is None:
            return {"alerts": [], "current_price": None, "note": "no price data available yet"}
        current_price = candle.close

    alerts = await get_new_exit_alerts(db, current_price=current_price, symbol=symbol)

    if alerts:
        from services.telegram.bot import TelegramBot
        bot = TelegramBot()
        for alert in alerts:
            await bot.send_exit_alert(asdict(alert))

    return {"alerts": [asdict(a) for a in alerts], "current_price": current_price}


@router.post("/{trade_id}/cancel")
async def cancel_manual_trade(trade_id: str, payload: CancelTradeIn, db: AsyncSession = Depends(get_db)):
    try:
        trade = await cancel_trade(db, trade_id=trade_id, reason=payload.reason)
    except TradeJournalError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize(trade)
