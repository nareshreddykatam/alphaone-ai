"""Section 6/20/26/33/34: the three performance views are returned as
separate top-level keys and MUST NEVER be combined into one number by any
consumer of this endpoint."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema import get_db
from services.portfolio.account import get_or_create_default_account
from services.portfolio.service import (
    get_user_actual_performance,
    get_alphaone_signal_performance,
    get_backtest_performance,
    get_missed_signals,
    get_equity_curve,
    get_pnl_breakdown,
)

router = APIRouter()


@router.get("/performance")
async def get_performance(db: AsyncSession = Depends(get_db)):
    account = await get_or_create_default_account(db)
    return {
        "backtest": await get_backtest_performance(db),
        "alphaone_signals": await get_alphaone_signal_performance(db),
        "user_actual": await get_user_actual_performance(db, account_id=account.id),
    }


@router.get("/missed-signals")
async def missed_signals(db: AsyncSession = Depends(get_db)):
    return await get_missed_signals(db)


@router.get("/equity-curve")
async def equity_curve(initial_equity: float = 0.0, db: AsyncSession = Depends(get_db)):
    account = await get_or_create_default_account(db)
    curve = await get_equity_curve(db, account.id, initial_equity=initial_equity)
    return {"equity_curve": curve}


@router.get("/pnl-breakdown")
async def pnl_breakdown(period: str = "daily", db: AsyncSession = Depends(get_db)):
    account = await get_or_create_default_account(db)
    breakdown = await get_pnl_breakdown(db, account.id, period=period)
    return {"period": period, "breakdown": breakdown}
