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
