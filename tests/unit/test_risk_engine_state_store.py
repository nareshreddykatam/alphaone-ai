"""Phase 4, section 28: the Risk Dashboard reads RiskEngine state across
stateless HTTP requests, so it must persist and rehydrate exactly -- a
mismatch here would silently reset HARD_KILL on every request, defeating
the whole point of a manual-only reset (Phase 2.6)."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from services.risk_engine.engine import RiskEngine, RiskConfig, RiskStatus
from services.risk_engine.state_store import load_risk_engine, save_risk_engine


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_first_load_with_no_saved_state_returns_fresh_active_engine(session_maker):
    async with session_maker() as session:
        engine = await load_risk_engine(session, initial_equity=5000.0)
        assert engine.get_risk_status() == RiskStatus.ACTIVE
        assert engine.state.current_equity == 5000.0


@pytest.mark.asyncio
async def test_hard_kill_survives_a_save_and_reload_roundtrip(session_maker):
    async with session_maker() as session:
        engine = await load_risk_engine(session, config=RiskConfig(max_drawdown_pct=5.0), initial_equity=10000.0)
        engine.record_trade_result(-6.0)  # breaches 5% drawdown -> hard kill
        assert engine.state.kill_switch is True
        await save_risk_engine(session, engine)

    async with session_maker() as session:
        pass  # reuse same in-memory db via a fresh "request" session

    # reload using the SAME session_maker (same underlying engine/db)
    async with session_maker() as session:
        reloaded = await load_risk_engine(session)
        assert reloaded.get_risk_status() == RiskStatus.HARD_KILL
        assert reloaded.state.kill_switch is True
        assert reloaded.state.current_equity == pytest.approx(engine.state.current_equity)


@pytest.mark.asyncio
async def test_cooldown_until_and_daily_pnl_round_trip_exactly(session_maker):
    async with session_maker() as session:
        engine = await load_risk_engine(
            session, config=RiskConfig(cooldown_consecutive_losses=2, cooldown_minutes=30, max_daily_loss_pct=50.0)
        )
        now = datetime(2026, 1, 1, 12, 0, 0)
        engine.record_trade_result(-1.0, now=now)
        engine.record_trade_result(-1.0, now=now)
        assert engine.state.cooldown_until == now + timedelta(minutes=30)
        await save_risk_engine(session, engine)

    async with session_maker() as session:
        reloaded = await load_risk_engine(session)
        assert reloaded.state.cooldown_until == now + timedelta(minutes=30)
        assert reloaded.state.daily_pnl_pct == pytest.approx(-2.0)
        # still within cooldown window
        assert reloaded.get_risk_status(now + timedelta(minutes=5)) == RiskStatus.COOLDOWN
        # after cooldown expires
        assert reloaded.get_risk_status(now + timedelta(minutes=31)) == RiskStatus.ACTIVE


@pytest.mark.asyncio
async def test_reset_hard_kill_persists_across_reload(session_maker):
    # max_daily_loss_pct set below the drawdown so ONLY the hard-kill
    # mechanism is exercised here -- the daily-loss/hard-kill interaction
    # (they're deliberately independent, Phase 2.6) is covered elsewhere.
    async with session_maker() as session:
        engine = await load_risk_engine(session, config=RiskConfig(max_drawdown_pct=5.0, max_daily_loss_pct=50.0))
        engine.record_trade_result(-6.0)
        await save_risk_engine(session, engine)

    async with session_maker() as session:
        engine = await load_risk_engine(session)
        engine.reset_hard_kill()
        await save_risk_engine(session, engine)

    async with session_maker() as session:
        reloaded = await load_risk_engine(session)
        assert reloaded.get_risk_status() == RiskStatus.ACTIVE
        assert reloaded.state.kill_switch is False
