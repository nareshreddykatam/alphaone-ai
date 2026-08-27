from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.config import get_settings
from database.schema import get_db
from services.exchange.coindcx import CoinDCXReadOnlyAccountProvider

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
