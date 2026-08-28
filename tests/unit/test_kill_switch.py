"""Live Futures Auto-Trading V1, Phase 14: the durable emergency-stop
mechanism. Backed by the existing BotState table (not an in-memory flag)
so it survives process/Railway/scheduler restarts -- every test here uses
a FRESH session per read to prove state is genuinely persisted, not just
cached on one session object. Also proves there is no HTTP write path."""
import inspect

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from services.live_execution.kill_switch import (
    is_emergency_stop_active, get_emergency_stop_detail,
    activate_emergency_stop, clear_emergency_stop,
)


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_inactive_by_default(session_maker):
    async with session_maker() as session:
        assert await is_emergency_stop_active(session) is False
        assert await get_emergency_stop_detail(session) is None


async def test_activate_persists_across_a_fresh_session(session_maker):
    """Simulates a restart: activation happens on one session, the check
    happens on a completely separate, freshly-opened one."""
    async with session_maker() as session:
        await activate_emergency_stop(session, reason="manual halt for testing")

    async with session_maker() as new_session:
        assert await is_emergency_stop_active(new_session) is True
        detail = await get_emergency_stop_detail(new_session)
        assert detail["reason"] == "manual halt for testing"
        assert detail["active"] is True
        assert "activated_at" in detail


async def test_clear_persists_across_a_fresh_session(session_maker):
    async with session_maker() as session:
        await activate_emergency_stop(session, reason="halt")
    async with session_maker() as session2:
        await clear_emergency_stop(session2)
    async with session_maker() as session3:
        assert await is_emergency_stop_active(session3) is False
        detail = await get_emergency_stop_detail(session3)
        assert detail["active"] is False
        assert "cleared_at" in detail


async def test_clear_when_never_activated_is_a_safe_noop(session_maker):
    async with session_maker() as session:
        await clear_emergency_stop(session)  # must not raise
        assert await is_emergency_stop_active(session) is False


async def test_activate_twice_overwrites_with_the_latest_reason(session_maker):
    async with session_maker() as session:
        await activate_emergency_stop(session, reason="first reason")
        await activate_emergency_stop(session, reason="second reason")
    async with session_maker() as session2:
        detail = await get_emergency_stop_detail(session2)
        assert detail["reason"] == "second reason"
        assert detail["active"] is True


async def test_activate_then_clear_then_reactivate_round_trips_correctly(session_maker):
    async with session_maker() as session:
        await activate_emergency_stop(session, reason="halt 1")
    async with session_maker() as session2:
        await clear_emergency_stop(session2)
    async with session_maker() as session3:
        assert await is_emergency_stop_active(session3) is False
    async with session_maker() as session4:
        await activate_emergency_stop(session4, reason="halt 2")
    async with session_maker() as session5:
        assert await is_emergency_stop_active(session5) is True
        detail = await get_emergency_stop_detail(session5)
        assert detail["reason"] == "halt 2"


def test_kill_switch_module_exposes_no_http_write_endpoint():
    """Phase 14: 'do NOT implement an easily-triggered PUBLIC endpoint
    anyone can use to manipulate trading' -- activate/clear must only be
    reachable as plain internal function calls, never wired to a FastAPI
    router anywhere. Confirms the module itself defines no router/app
    object and is not a fastapi module at all."""
    import services.live_execution.kill_switch as kill_switch_module
    source = inspect.getsource(kill_switch_module)
    assert "APIRouter" not in source
    assert "@router" not in source
    assert "fastapi" not in source.lower()


def test_live_execution_status_router_has_no_kill_switch_write_route():
    import apps.api.routers.live_execution_status as status_module
    source = inspect.getsource(status_module)
    for forbidden in ("@router.post", "@router.put", "@router.patch", "@router.delete"):
        assert forbidden not in source
    assert "activate_emergency_stop" not in source
    assert "clear_emergency_stop" not in source
