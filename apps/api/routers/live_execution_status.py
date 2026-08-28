"""Read-only observability for the live-execution safety architecture
(Live Futures Auto-Trading V1, Phase 27-28). GET only -- no way to
enable automatic trading, arm live execution, clear the emergency stop,
or change margin/leverage/daily-max through this router. Every field is
real, live-computed state; never a credential."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema import get_db
from apps.api.config import get_settings
from services.live_execution.gates import ORDER_CONTRACT_VERIFIED, count_open_live_positions
from services.live_execution.kill_switch import is_emergency_stop_active, get_emergency_stop_detail
from services.risk_engine.fixed_margin import get_daily_trade_budget, FIXED_MARGIN_INR, FIXED_LEVERAGE

router = APIRouter()


@router.get("/status")
async def get_live_execution_status(db: AsyncSession = Depends(get_db)):
    settings = get_settings()

    if not settings.automatic_trading_enabled or not settings.live_execution_armed:
        automatic_trading = "DISABLED"
    elif not ORDER_CONTRACT_VERIFIED:
        automatic_trading = "ARMED"  # configured, but structurally incapable of a real order today
    else:
        automatic_trading = "ACTIVE"

    emergency_active = await is_emergency_stop_active(db)
    emergency_detail = await get_emergency_stop_detail(db) if emergency_active else None
    budget = await get_daily_trade_budget(db)

    open_positions = await count_open_live_positions(db)

    return {
        "automatic_trading": automatic_trading,
        "automatic_trading_enabled": settings.automatic_trading_enabled,
        "live_execution_armed": settings.live_execution_armed,
        "order_contract_verified": ORDER_CONTRACT_VERIFIED,
        "emergency_stop": "ACTIVE" if emergency_active else "CLEAR",
        "emergency_stop_reason": emergency_detail.get("reason") if emergency_detail else None,
        "daily_entries": {"count": budget.trades_today, "target": budget.target, "max": budget.max_allowed},
        "margin_inr": FIXED_MARGIN_INR,
        "leverage": FIXED_LEVERAGE,
        "max_open_positions_live": settings.max_open_positions_live,
        "open_positions_live": open_positions,
    }
