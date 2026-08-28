from fastapi import APIRouter
from apps.api.routers import (
    dashboard, signals, trades, performance, model, risk, settings, health,
    accounts, journal, portfolio, market, telegram_status,
)

api_router = APIRouter()

api_router.include_router(dashboard.router, prefix="/dashboard", tags=["Dashboard"])
api_router.include_router(signals.router, prefix="/signals", tags=["Signals"])
api_router.include_router(trades.router, prefix="/trades", tags=["Trades"])
api_router.include_router(performance.router, prefix="/performance", tags=["Performance"])
api_router.include_router(model.router, prefix="/model", tags=["Model"])
api_router.include_router(risk.router, prefix="/risk", tags=["Risk"])
api_router.include_router(settings.router, prefix="/settings", tags=["Settings"])
api_router.include_router(health.router, prefix="/health", tags=["Health"])
api_router.include_router(accounts.router, prefix="/accounts", tags=["Accounts"])
api_router.include_router(journal.router, prefix="/journal", tags=["Trade Journal"])
api_router.include_router(portfolio.router, prefix="/portfolio", tags=["Portfolio"])
api_router.include_router(market.router, prefix="/market", tags=["Market Data"])
api_router.include_router(telegram_status.router, prefix="/telegram", tags=["Telegram"])
