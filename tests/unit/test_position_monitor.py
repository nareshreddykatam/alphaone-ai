"""Phase 4I: position monitoring only ever RECOMMENDS an exit -- it must
never close a trade itself, and must not re-alert the same breach forever."""
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import Trade, TradeStatus
from services.portfolio.account import get_or_create_default_account
from services.position_monitor.monitor import (
    check_open_positions, get_new_exit_alerts, STOP_LOSS_HIT, TAKE_PROFIT_HIT,
)


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _open_trade(session, account_id, side, entry, sl, tp1):
    trade = Trade(
        trade_id=f"T-{side}-{entry}", side=side, entry_price=entry, quantity=1.0,
        stop_loss=sl, take_profit_1=tp1, entry_time=datetime(2026, 1, 1),
        status=TradeStatus.OPEN.value, account_id=account_id,
    )
    session.add(trade)
    await session.commit()
    return trade


@pytest.mark.asyncio
async def test_long_stop_loss_breach_detected(session_maker):
    async with session_maker() as session:
        account = await get_or_create_default_account(session)
        await _open_trade(session, account.id, "LONG", 100.0, 90.0, 120.0)

        alerts = await check_open_positions(session, current_price=89.0)
        assert len(alerts) == 1
        assert alerts[0].reason == STOP_LOSS_HIT


@pytest.mark.asyncio
async def test_short_take_profit_breach_detected(session_maker):
    async with session_maker() as session:
        account = await get_or_create_default_account(session)
        await _open_trade(session, account.id, "SHORT", 100.0, 110.0, 80.0)

        alerts = await check_open_positions(session, current_price=79.0)
        assert len(alerts) == 1
        assert alerts[0].reason == TAKE_PROFIT_HIT


@pytest.mark.asyncio
async def test_no_breach_when_price_is_between_levels(session_maker):
    async with session_maker() as session:
        account = await get_or_create_default_account(session)
        await _open_trade(session, account.id, "LONG", 100.0, 90.0, 120.0)

        alerts = await check_open_positions(session, current_price=105.0)
        assert alerts == []


@pytest.mark.asyncio
async def test_monitor_never_closes_the_trade_itself(session_maker):
    async with session_maker() as session:
        account = await get_or_create_default_account(session)
        trade = await _open_trade(session, account.id, "LONG", 100.0, 90.0, 120.0)

        await check_open_positions(session, current_price=89.0)
        await get_new_exit_alerts(session, current_price=89.0)

        refreshed = (await session.execute(select(Trade).where(Trade.trade_id == trade.trade_id))).scalar_one()
        assert refreshed.status == TradeStatus.OPEN.value  # still open -- never auto-closed
        assert refreshed.exit_price is None


@pytest.mark.asyncio
async def test_same_breach_is_not_re_alerted(session_maker):
    async with session_maker() as session:
        account = await get_or_create_default_account(session)
        await _open_trade(session, account.id, "LONG", 100.0, 90.0, 120.0)

        first = await get_new_exit_alerts(session, current_price=89.0)
        assert len(first) == 1

        second = await get_new_exit_alerts(session, current_price=89.0)
        assert second == []  # already alerted, not repeated
