"""Phase 3's conclusion was that no ML model demonstrated a robust,
cost-surviving out-of-sample edge (see docs/ml_methodology.md and the
Phase 3 report) -- so there is no production model deployed. This endpoint
reports that honestly rather than showing a fabricated "v1 XGBoost" status
for a model that was never actually put into service."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema import get_db
from database.schema.models import BotState

router = APIRouter()
_DEPLOYED_MODEL_KEY = "deployed_model_metadata"


@router.get("/")
async def get_model_info(db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(BotState).where(BotState.key == _DEPLOYED_MODEL_KEY))).scalar_one_or_none()
    if row is None:
        return {
            "status": "NO_MODEL_DEPLOYED",
            "note": (
                "Phase 3 research found no ML model with a robust, cost-surviving "
                "out-of-sample edge -- see the Phase 3 report and docs/known_limitations.md. "
                "The live signal source is the rule-based Donchian+ADX baseline "
                "(services.signal_engine.strategy.BaselineStrategy), also unverified as a "
                "guaranteed edge."
            ),
            "model_version": None, "training_period": None, "feature_importance": [],
        }
    return row.value


@router.get("/feature-importance")
async def get_feature_importance(db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(BotState).where(BotState.key == _DEPLOYED_MODEL_KEY))).scalar_one_or_none()
    if row is None:
        return {"features": [], "note": "No model deployed -- nothing to report feature importance for."}
    return {"features": row.value.get("feature_importance", [])}


@router.get("/ai-status")
async def get_ai_status(db: AsyncSession = Depends(get_db)):
    """AI Trading V1: the AI orchestrator + paper-trading layer's current
    status -- separate from the plain model-deployment info above so this
    endpoint keeps working (and stays cheap) even while no model is
    deployed, which is the current, honest default (see
    reports/AI_TRADING_RESEARCH_V1.txt)."""
    from services.model_monitor.monitor import evaluate_model_health
    from services.paper_trader.live_state import paper_trader

    model_row = (await db.execute(select(BotState).where(BotState.key == _DEPLOYED_MODEL_KEY))).scalar_one_or_none()
    health = await evaluate_model_health(db)

    return {
        "model_status": "NO_MODEL_DEPLOYED" if model_row is None else "MODEL_DEPLOYED",
        "model_health": {
            "status": health.status,
            "reasons": health.reasons,
            "lookback_trades": health.lookback_trades,
            "recent_win_rate": health.recent_win_rate,
            "recent_profit_factor": health.recent_profit_factor,
            "recent_max_drawdown_pct": health.recent_max_drawdown_pct,
            "prediction_class_distribution": health.prediction_class_distribution,
        },
        "paper_trading": paper_trader.get_status(),
        "automatic_trading": "DISABLED",
    }
