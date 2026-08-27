"""Reuses the Phase 2.6 RiskEngine/RiskStatus (ACTIVE/DAILY_LIMIT/COOLDOWN/
HARD_KILL) as-is -- this router never builds a second risk model. It is
informational only in Phase 4: AlphaOne cannot place trades, so nothing
here ever blocks an order. reset-hard-kill is the one write action, and it
mirrors RiskEngine.reset_hard_kill()'s own explicit-only semantics (Phase
2.6) -- never auto-triggered."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema import get_db
from services.risk_engine.state_store import load_risk_engine, save_risk_engine

router = APIRouter()


@router.get("/")
async def get_risk_status(db: AsyncSession = Depends(get_db)):
    engine = await load_risk_engine(db)
    return engine.get_status()


@router.post("/reset-hard-kill")
async def reset_hard_kill(db: AsyncSession = Depends(get_db)):
    engine = await load_risk_engine(db)
    engine.reset_hard_kill()
    await save_risk_engine(db, engine)
    return engine.get_status()
