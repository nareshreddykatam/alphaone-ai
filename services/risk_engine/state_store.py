"""Persists the Phase 4 RiskEngine's state to the `bot_state` key-value
table so the risk dashboard reflects real state across stateless API
requests (a fresh RiskEngine() per request would always report ACTIVE).

This engine instance is informational only in Phase 4 -- it is fed the
user's own manually-reported trade results (see apps/api/routers/journal.py)
so the Risk Dashboard (section 28) can show the same RiskStatus states
(ACTIVE/DAILY_LIMIT/COOLDOWN/HARD_KILL) already proven correct in Phase 2.6.
It never gates or blocks a manual trade -- AlphaOne cannot place orders at
all, so there is nothing for it to block; it only reports risk state.
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import BotState
from services.risk_engine.engine import RiskEngine, RiskConfig, RiskState

_STATE_KEY = "risk_engine_state"


def _serialize(engine: RiskEngine) -> dict:
    state = engine.state
    return {
        "initial_equity": engine.initial_equity,
        "trade_date": engine._trade_date.isoformat(),
        "config": {
            "risk_per_trade_pct": engine.config.risk_per_trade_pct,
            "max_daily_loss_pct": engine.config.max_daily_loss_pct,
            "max_drawdown_pct": engine.config.max_drawdown_pct,
            "max_leverage": engine.config.max_leverage,
            "max_positions": engine.config.max_positions,
            "max_daily_trades": engine.config.max_daily_trades,
            "cooldown_consecutive_losses": engine.config.cooldown_consecutive_losses,
            "cooldown_minutes": engine.config.cooldown_minutes,
            "breakeven_resets_consecutive_losses": engine.config.breakeven_resets_consecutive_losses,
        },
        "state": {
            "daily_pnl_pct": state.daily_pnl_pct,
            "daily_trades": state.daily_trades,
            "current_drawdown_pct": state.current_drawdown_pct,
            "peak_equity": state.peak_equity,
            "current_equity": state.current_equity,
            "consecutive_losses": state.consecutive_losses,
            "last_loss_time": state.last_loss_time.isoformat() if state.last_loss_time else None,
            "cooldown_until": state.cooldown_until.isoformat() if state.cooldown_until else None,
            "kill_switch": state.kill_switch,
            "positions_open": state.positions_open,
        },
    }


def _deserialize(payload: dict) -> RiskEngine:
    config = RiskConfig(**payload["config"])
    engine = RiskEngine(config=config, initial_equity=payload["initial_equity"])
    engine._trade_date = date.fromisoformat(payload["trade_date"])

    s = payload["state"]
    engine.state = RiskState(
        daily_pnl_pct=s["daily_pnl_pct"],
        daily_trades=s["daily_trades"],
        current_drawdown_pct=s["current_drawdown_pct"],
        peak_equity=s["peak_equity"],
        current_equity=s["current_equity"],
        consecutive_losses=s["consecutive_losses"],
        last_loss_time=datetime.fromisoformat(s["last_loss_time"]) if s["last_loss_time"] else None,
        cooldown_until=datetime.fromisoformat(s["cooldown_until"]) if s["cooldown_until"] else None,
        kill_switch=s["kill_switch"],
        positions_open=s["positions_open"],
    )
    return engine


async def load_risk_engine(
    session: AsyncSession, config: Optional[RiskConfig] = None, initial_equity: float = 10000.0
) -> RiskEngine:
    row = (await session.execute(select(BotState).where(BotState.key == _STATE_KEY))).scalar_one_or_none()
    if row is None:
        return RiskEngine(config=config, initial_equity=initial_equity)
    return _deserialize(row.value)


async def save_risk_engine(session: AsyncSession, engine: RiskEngine) -> None:
    row = (await session.execute(select(BotState).where(BotState.key == _STATE_KEY))).scalar_one_or_none()
    payload = _serialize(engine)
    if row is None:
        session.add(BotState(key=_STATE_KEY, value=payload))
    else:
        row.value = payload
    await session.commit()
