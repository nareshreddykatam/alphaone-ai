"""Multi-Coin AI Futures System, Phases 17-19: Rs.200 margin, EXACTLY 10x
leverage (never dynamic), a 10-trade/day TARGET that must never force
low-quality trades, and a 15-trade/day HARD MAXIMUM that always overrides
the target."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import Trade, TradeStatus, TradeSource
from services.risk_engine.fixed_margin import (
    size_fixed_margin_trade, get_daily_trade_budget, check_fixed_margin_trade,
    FIXED_MARGIN_INR, FIXED_LEVERAGE, DAILY_TRADE_TARGET, DAILY_TRADE_MAX,
)


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _open_trade(i, entry_time, source=TradeSource.AI_PAPER.value, symbol="BTC/USDT"):
    return Trade(
        trade_id=f"T-{i}", symbol=symbol, side="LONG", status=TradeStatus.OPEN.value, mode="paper",
        source=source, entry_price=100, quantity=1, entry_time=entry_time,
    )


def test_leverage_is_always_exactly_10x_never_a_parameter():
    result = size_fixed_margin_trade(entry_price_usdt=80000.0, usdt_inr_rate=88.0)
    assert result.leverage == FIXED_LEVERAGE == 10


def test_margin_is_always_exactly_200_inr():
    result = size_fixed_margin_trade(entry_price_usdt=80000.0, usdt_inr_rate=88.0)
    assert result.margin_inr == FIXED_MARGIN_INR == 200.0


def test_sizing_math_is_correct():
    result = size_fixed_margin_trade(entry_price_usdt=80000.0, usdt_inr_rate=80.0)
    # margin_usdt = 200/80 = 2.5, notional = 2.5*10 = 25 USDT, qty = 25/80000
    assert result.approved is True
    assert result.notional_usdt == pytest.approx(25.0)
    assert result.quantity == pytest.approx(25.0 / 80000.0)


def test_no_live_inr_rate_blocks_the_trade_never_guesses():
    result = size_fixed_margin_trade(entry_price_usdt=80000.0, usdt_inr_rate=None)
    assert result.approved is False
    assert "rate" in result.reason.lower()


def test_zero_or_negative_rate_blocks_the_trade():
    assert size_fixed_margin_trade(80000.0, 0).approved is False
    assert size_fixed_margin_trade(80000.0, -5).approved is False


def test_invalid_entry_price_blocks_the_trade():
    assert size_fixed_margin_trade(0, 88.0).approved is False
    assert size_fixed_margin_trade(-100, 88.0).approved is False


async def test_daily_budget_counts_only_todays_fixed_margin_entries(session_maker):
    async with session_maker() as session:
        today = datetime(2026, 1, 15, 10, 0)
        yesterday = today - timedelta(days=1)
        session.add(_open_trade(1, today))
        session.add(_open_trade(2, today))
        session.add(_open_trade(3, yesterday))  # not today -- must not count
        session.add(_open_trade(4, today, source=TradeSource.MANUAL.value))  # not a fixed-margin source -- must not count
        await session.commit()

        budget = await get_daily_trade_budget(session, now=today.replace(hour=23))
        assert budget.trades_today == 2


async def test_daily_budget_combines_ai_and_telegram_sources_one_shared_pool(session_maker):
    async with session_maker() as session:
        today = datetime(2026, 1, 15, 10, 0)
        session.add(_open_trade(1, today, source=TradeSource.AI_PAPER.value))
        session.add(_open_trade(2, today, source=TradeSource.TELEGRAM_EXTERNAL.value))
        await session.commit()

        budget = await get_daily_trade_budget(session, now=today.replace(hour=23))
        assert budget.trades_today == 2


async def test_target_reached_does_not_block_trading_only_max_does(session_maker):
    async with session_maker() as session:
        today = datetime(2026, 1, 15, 10, 0)
        for i in range(DAILY_TRADE_TARGET):  # exactly at target (10), below max (15)
            session.add(_open_trade(i, today))
        await session.commit()

        budget = await get_daily_trade_budget(session, now=today.replace(hour=23))
        assert budget.target_reached is True
        assert budget.can_open_new_entry is True  # target reached is NOT a block

        result = await check_fixed_margin_trade(session, 80000.0, 88.0, now=today.replace(hour=23))
        assert result.approved is True


async def test_hard_max_blocks_new_entries_even_with_a_valid_rate(session_maker):
    async with session_maker() as session:
        today = datetime(2026, 1, 15, 10, 0)
        for i in range(DAILY_TRADE_MAX):  # at the hard max (15)
            session.add(_open_trade(i, today))
        await session.commit()

        result = await check_fixed_margin_trade(session, 80000.0, 88.0, now=today.replace(hour=23))
        assert result.approved is False
        assert "maximum" in result.reason.lower()


async def test_risk_blocks_always_override_target_even_at_zero_trades_today(session_maker):
    """Under the 15-trade max but with NO live rate -- risk still blocks."""
    async with session_maker() as session:
        result = await check_fixed_margin_trade(session, 80000.0, None)
        assert result.approved is False


async def test_counter_resets_at_next_utc_day(session_maker):
    async with session_maker() as session:
        yesterday = datetime(2026, 1, 14, 23, 0)
        for i in range(DAILY_TRADE_MAX):
            session.add(_open_trade(i, yesterday))
        await session.commit()

        # A new UTC day -- the counter must be back to 0, not still maxed.
        budget = await get_daily_trade_budget(session, now=datetime(2026, 1, 15, 0, 5))
        assert budget.trades_today == 0
        assert budget.can_open_new_entry is True


async def test_closing_a_position_does_not_change_the_entry_count(session_maker):
    """Phase 18: 'Closing a position does not reset the counter' -- and
    symmetrically, closing does not consume an extra budget slot either.
    The count is purely OPEN-EVENT based (entry_time), regardless of
    whether the trade is later closed."""
    async with session_maker() as session:
        today = datetime(2026, 1, 15, 10, 0)
        t = _open_trade(1, today)
        t.status = TradeStatus.CLOSED.value
        t.exit_time = today + timedelta(hours=1)
        session.add(t)
        await session.commit()

        budget = await get_daily_trade_budget(session, now=today.replace(hour=23))
        assert budget.trades_today == 1
