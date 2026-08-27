from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.config import get_settings
from database.schema import get_db
from database.schema.models import BotState

router = APIRouter()
_env_settings = get_settings()
_SETTINGS_KEY = "user_settings"

_MUTABLE_DEFAULTS = {
    "prediction_threshold": _env_settings.prediction_threshold,
    "telegram_enabled": _env_settings.telegram_enabled,
    "min_signal_quality": "MEDIUM",
}


@router.get("/")
async def get_settings_endpoint(db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(BotState).where(BotState.key == _SETTINGS_KEY))).scalar_one_or_none()
    mutable = row.value if row is not None else _MUTABLE_DEFAULTS
    return {
        "trading_mode": _env_settings.trading_mode,
        "max_leverage": _env_settings.max_leverage,
        "risk_per_trade_pct": _env_settings.risk_per_trade_pct,
        "max_daily_loss_pct": _env_settings.max_daily_loss_pct,
        **mutable,
    }


@router.put("/")
async def update_settings(payload: dict, db: AsyncSession = Depends(get_db)):
    allowed = {k: v for k, v in payload.items() if k in _MUTABLE_DEFAULTS}
    row = (await db.execute(select(BotState).where(BotState.key == _SETTINGS_KEY))).scalar_one_or_none()
    merged = {**_MUTABLE_DEFAULTS, **(row.value if row else {}), **allowed}
    if row is None:
        db.add(BotState(key=_SETTINGS_KEY, value=merged))
    else:
        row.value = merged
    await db.commit()
    return {"status": "updated", "settings": merged}
