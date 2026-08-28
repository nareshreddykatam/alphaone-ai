"""Durable emergency-stop mechanism for real-money execution (Live
Futures Auto-Trading V1, Phase 14). Backed by the existing BotState
key-value table (same table apps/api/routers/model.py already uses for
"deployed_model_metadata") -- survives process restarts, Railway
restarts, and scheduler restarts, unlike an in-memory flag.

Deliberately NOT exposed through any HTTP endpoint, authenticated or
not -- Phase 14 explicitly forbids "an easily-triggered public endpoint
that anyone can use to manipulate trading". Activating or clearing the
emergency stop requires calling these functions directly (from a trusted
operator script/shell, never from a request handler). The READ-ONLY
status endpoint (apps/api/routers/live_execution_status.py) can report
whether it is active, exactly like every other status field, but has no
corresponding write endpoint.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import BotState

EMERGENCY_STOP_KEY = "emergency_stop_active"


async def is_emergency_stop_active(session: AsyncSession) -> bool:
    row = (await session.execute(select(BotState).where(BotState.key == EMERGENCY_STOP_KEY))).scalar_one_or_none()
    if row is None:
        return False
    return bool(row.value.get("active", False))


async def get_emergency_stop_detail(session: AsyncSession) -> Optional[dict]:
    row = (await session.execute(select(BotState).where(BotState.key == EMERGENCY_STOP_KEY))).scalar_one_or_none()
    return row.value if row is not None else None


async def activate_emergency_stop(session: AsyncSession, reason: str) -> None:
    """Existing OPEN positions may still be monitored/managed (Phase 14:
    'existing position monitoring may continue if safe') -- this only
    blocks NEW entries, enforced by the gate check
    (services/live_execution/gates.py) reading this same flag."""
    row = (await session.execute(select(BotState).where(BotState.key == EMERGENCY_STOP_KEY))).scalar_one_or_none()
    value = {"active": True, "reason": reason, "activated_at": datetime.utcnow().isoformat()}
    if row is None:
        session.add(BotState(key=EMERGENCY_STOP_KEY, value=value))
    else:
        row.value = value
    await session.commit()


async def clear_emergency_stop(session: AsyncSession) -> None:
    row = (await session.execute(select(BotState).where(BotState.key == EMERGENCY_STOP_KEY))).scalar_one_or_none()
    if row is None:
        return
    row.value = {"active": False, "cleared_at": datetime.utcnow().isoformat()}
    await session.commit()
