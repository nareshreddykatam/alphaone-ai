"""Kept for backward compatibility with the existing frontend performance
page; this now just re-exposes the "user_actual" view from
services.portfolio.service under the shape the old stub used. New
consumers should prefer /api/v1/portfolio/performance, which returns the
three views (backtest / alphaone_signals / user_actual) explicitly
separated -- never combine them."""
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema import get_db
from services.portfolio.account import get_or_create_default_account
from services.portfolio.service import get_user_actual_performance, get_equity_curve, get_pnl_breakdown

router = APIRouter()


@router.get("/")
async def get_performance(db: AsyncSession = Depends(get_db)):
    account = await get_or_create_default_account(db)
    stats = await get_user_actual_performance(db, account_id=account.id)
    curve = await get_equity_curve(db, account.id)
    return {
        "total_pnl": stats["total_pnl"],
        "total_trades": stats["total_trades"],
        "win_rate": stats["win_rate"],
        "profit_factor": stats["profit_factor"],
        "average_r": stats["average_r"],
        "equity_curve": [{"timestamp": c["timestamp"], "equity": c["equity"]} for c in curve],
    }


@router.get("/daily")
async def get_daily_performance(db: AsyncSession = Depends(get_db)):
    account = await get_or_create_default_account(db)
    breakdown = await get_pnl_breakdown(db, account.id, period="daily")
    return {"daily_metrics": breakdown}
