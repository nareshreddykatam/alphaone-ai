from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema import get_db
from database.schema.models import Trade
from services.portfolio.account import get_or_create_default_account
from services.trade_journal.journal import get_open_trades, get_trade_executions

router = APIRouter()


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


@router.get("/")
async def get_trades(limit: int = 50, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Trade).order_by(Trade.entry_time.desc()).limit(limit))
    rows = result.scalars().all()
    return {"trades": [_serialize(t) for t in rows], "count": len(rows)}


@router.get("/open")
async def get_open_trades_endpoint(db: AsyncSession = Depends(get_db)):
    account = await get_or_create_default_account(db)
    rows = await get_open_trades(db, account_id=account.id)
    return {"trades": [_serialize(t) for t in rows], "count": len(rows)}


@router.get("/{trade_id}")
async def get_trade(trade_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Trade).where(Trade.trade_id == trade_id))
    trade = result.scalar_one_or_none()
    if trade is None:
        raise HTTPException(status_code=404, detail="trade not found")
    executions = await get_trade_executions(db, trade_id)
    return {
        "trade": _serialize(trade),
        "executions": [
            {"execution_type": e.execution_type, "price": e.price, "quantity": e.quantity, "timestamp": e.timestamp}
            for e in executions
        ],
    }
