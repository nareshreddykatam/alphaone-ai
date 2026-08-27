from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.config import get_settings
from database.schema import get_db
from services.exchange.coindcx import CoinDCXReadOnlyAccountProvider
from services.scheduler.runner import scheduler

router = APIRouter()
settings = get_settings()


async def compute_readiness(db: AsyncSession) -> dict:
    """Real dependency checks (DB connectivity, CoinDCX reachability) --
    used by both GET /api/v1/health/ and the top-level GET /ready (see
    apps/api/main.py). Deliberately returns only coarse status strings,
    never a credential, token, connection string, or account balance --
    safe to expose on an unauthenticated endpoint that a hosting
    platform's health checker will call repeatedly."""
    try:
        await db.execute(text("SELECT 1"))
        database_status = "ok"
    except Exception:
        database_status = "error"

    provider = CoinDCXReadOnlyAccountProvider(settings.coindcx_api_key, settings.coindcx_api_secret)
    try:
        coindcx_status = (await provider.get_connection_status())["status"]
    finally:
        await provider.close()

    services = {"database": database_status, "coindcx_account": coindcx_status}
    healthy_values = ("ok", "OK", "NOT_CONFIGURED")
    overall = "ok" if all(v in healthy_values for v in services.values()) else "degraded"
    return {"status": overall, "services": services}


@router.get("/")
async def health(db: AsyncSession = Depends(get_db)):
    return await compute_readiness(db)


@router.get("/scheduler")
async def scheduler_health():
    """Read-only scheduler-loop liveness, safe to expose unauthenticated --
    never a credential, token, or account balance. Added after a real
    production incident where every DB-writing scheduler job went silent
    for 12+ minutes with no way to tell, from the outside, whether the
    job LOOP had stopped iterating versus every attempt merely failing
    before it could write anything -- see services/scheduler/runner.py's
    module docstring. `last_tick_at`/`seconds_since_last_tick` answer that
    directly; `circuit_state`/`consecutive_failures` show the existing
    per-job CircuitBreaker state."""
    return scheduler.get_heartbeat()
