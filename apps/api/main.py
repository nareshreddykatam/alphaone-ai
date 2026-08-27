from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from apps.api.config import get_settings
from apps.api.routers import api_router
from apps.api.routers.health import compute_readiness
from database.schema import engine, Base, get_db
from services.market_data.live_state import start_market_data_ws, stop_market_data_ws
from services.scheduler.runner import SchedulerRunner

logger = structlog.get_logger()
settings = get_settings()
scheduler = SchedulerRunner(settings.coindcx_api_key, settings.coindcx_api_secret)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("AlphaOne BTC AI starting up", mode=settings.trading_mode)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    if settings.scheduler_enabled:
        # Must run on a persistent process, never inside a serverless
        # function -- see docs/deployment.md.
        scheduler.start()
        logger.info("Background scheduler enabled")
    else:
        logger.info("Background scheduler disabled (SCHEDULER_ENABLED=false)")

    if settings.market_data_ws_enabled:
        # Fire-and-forget: connect() awaits the initial handshake, so run it
        # as a background task rather than blocking app startup on it; a
        # failed initial connection is caught and logged, never crashes
        # startup (see services/market_data/live_state.py).
        import asyncio
        asyncio.create_task(start_market_data_ws())
        logger.info("CoinDCX live market-data WebSocket enabled")
    else:
        logger.info("CoinDCX live market-data WebSocket disabled (MARKET_DATA_WS_ENABLED=false)")

    yield
    if settings.scheduler_enabled:
        await scheduler.stop()
    if settings.market_data_ws_enabled:
        await stop_market_data_ws()
    logger.info("AlphaOne BTC AI shutting down")
    await engine.dispose()


app = FastAPI(
    title="AlphaOne BTC AI",
    description="AI-powered BTC/USDT perpetual futures trading intelligence platform",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/health")
async def health_check():
    """Liveness only: is the process up and responding at all. No
    dependency checks (DB/CoinDCX) -- cheap and fast, safe to call very
    frequently. Use this for an orchestrator's restart-on-failure probe."""
    return {"status": "ok", "service": "alphaone", "version": "0.1.0"}


@app.get("/ready")
async def readiness_check(db: AsyncSession = Depends(get_db)):
    """Readiness: is the process ready to serve real traffic -- checks
    real DB connectivity and CoinDCX reachability (never returns a
    credential, connection string, or account balance; see
    apps/api/routers/health.py's compute_readiness). Identical logic to
    GET /api/v1/health/, exposed at the conventional top-level /ready
    path some hosting platforms expect. Use this for a load balancer's
    traffic-routing probe -- a "degraded" response should stop new
    traffic from being routed here without necessarily restarting the
    process."""
    return await compute_readiness(db)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("apps.api.main:app", host=settings.api_host, port=settings.api_port, reload=settings.app_debug)
