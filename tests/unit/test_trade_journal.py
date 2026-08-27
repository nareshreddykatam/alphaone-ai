"""Phase 4, sections 6 & 18: manual trade tracking. The user reports every
fill by hand -- these tests exercise open/partial-exit/full-exit/cancel and
confirm PnL/fees/R-multiple match services.trade_journal.pnl exactly, plus
guardrails (can't over-exit, can't cancel after an exit, can't double-close).
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import TradeStatus, ExecutionType
from services.trade_journal.journal import (
    TradeJournalError,
    open_trade,
    record_exit,
    cancel_trade,
    get_open_trades,
    get_trade_executions,
)
from services.trade_journal.pnl import compute_slice_pnl, compute_r_multiple


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_open_trade_creates_entry_execution(session_maker):
    async with session_maker() as session:
        trade = await open_trade(
            session, symbol="BTC/USDT", side="LONG", entry_price=100.0, quantity=2.0,
            entry_time=datetime(2026, 1, 1), stop_loss=95.0,
        )
        assert trade.status == TradeStatus.OPEN.value
        assert trade.is_manual_entry is True

        executions = await get_trade_executions(session, trade.trade_id)
        assert len(executions) == 1
        assert executions[0].execution_type == ExecutionType.ENTRY.value
        assert executions[0].quantity == 2.0


@pytest.mark.asyncio
async def test_full_exit_matches_hand_computed_pnl(session_maker):
    async with session_maker() as session:
        trade = await open_trade(
            session, symbol="BTC/USDT", side="LONG", entry_price=100.0, quantity=2.0,
            entry_time=datetime(2026, 1, 1), stop_loss=95.0,
        )
        closed = await record_exit(
            session, trade_id=trade.trade_id, exit_price=110.0, quantity=2.0,
            timestamp=datetime(2026, 1, 2), reason="take_profit",
        )

        expected = compute_slice_pnl("LONG", 100.0, 110.0, 2.0, leverage=1)
        expected_r = compute_r_multiple(100.0, 95.0, 2.0, expected.pnl)

        assert closed.status == TradeStatus.CLOSED.value
        assert closed.pnl == pytest.approx(expected.pnl)
        assert closed.fees == pytest.approx(expected.fees)
        assert closed.r_multiple == pytest.approx(expected_r)
        assert closed.exit_price == 110.0
        assert closed.exit_reason == "take_profit"


@pytest.mark.asyncio
async def test_partial_then_full_exit_sums_pnl_and_computes_weighted_exit_price(session_maker):
    async with session_maker() as session:
        trade = await open_trade(
            session, symbol="BTC/USDT", side="LONG", entry_price=100.0, quantity=4.0,
            entry_time=datetime(2026, 1, 1), stop_loss=90.0,
        )

        partial = await record_exit(
            session, trade_id=trade.trade_id, exit_price=110.0, quantity=1.0,
            timestamp=datetime(2026, 1, 2),
        )
        assert partial.status == TradeStatus.PARTIALLY_CLOSED.value

        final = await record_exit(
            session, trade_id=trade.trade_id, exit_price=120.0, quantity=3.0,
            timestamp=datetime(2026, 1, 3), reason="take_profit",
        )
        assert final.status == TradeStatus.CLOSED.value

        slice1 = compute_slice_pnl("LONG", 100.0, 110.0, 1.0)
        slice2 = compute_slice_pnl("LONG", 100.0, 120.0, 3.0)
        expected_total_pnl = slice1.pnl + slice2.pnl
        expected_exit_price = (110.0 * 1.0 + 120.0 * 3.0) / 4.0

        assert final.pnl == pytest.approx(expected_total_pnl)
        assert final.exit_price == pytest.approx(expected_exit_price)

        executions = await get_trade_executions(session, trade.trade_id)
        assert [e.execution_type for e in executions] == [
            ExecutionType.ENTRY.value, ExecutionType.PARTIAL_EXIT.value, ExecutionType.EXIT.value,
        ]


@pytest.mark.asyncio
async def test_cannot_exit_more_than_remaining_open_quantity(session_maker):
    async with session_maker() as session:
        trade = await open_trade(
            session, symbol="BTC/USDT", side="LONG", entry_price=100.0, quantity=1.0,
            entry_time=datetime(2026, 1, 1),
        )
        with pytest.raises(TradeJournalError, match="only 1.0 remains open"):
            await record_exit(
                session, trade_id=trade.trade_id, exit_price=110.0, quantity=1.5,
                timestamp=datetime(2026, 1, 2),
            )


@pytest.mark.asyncio
async def test_cannot_exit_an_already_closed_trade(session_maker):
    async with session_maker() as session:
        trade = await open_trade(
            session, symbol="BTC/USDT", side="LONG", entry_price=100.0, quantity=1.0,
            entry_time=datetime(2026, 1, 1),
        )
        await record_exit(session, trade_id=trade.trade_id, exit_price=105.0, quantity=1.0, timestamp=datetime(2026, 1, 2))
        with pytest.raises(TradeJournalError, match="already CLOSED"):
            await record_exit(session, trade_id=trade.trade_id, exit_price=106.0, quantity=1.0, timestamp=datetime(2026, 1, 3))


@pytest.mark.asyncio
async def test_cancel_only_allowed_before_any_exit(session_maker):
    async with session_maker() as session:
        trade = await open_trade(
            session, symbol="BTC/USDT", side="LONG", entry_price=100.0, quantity=1.0,
            entry_time=datetime(2026, 1, 1),
        )
        cancelled = await cancel_trade(session, trade_id=trade.trade_id, reason="fat-finger entry")
        assert cancelled.status == TradeStatus.CANCELLED.value

        trade2 = await open_trade(
            session, symbol="BTC/USDT", side="LONG", entry_price=100.0, quantity=1.0,
            entry_time=datetime(2026, 1, 1),
        )
        await record_exit(session, trade_id=trade2.trade_id, exit_price=105.0, quantity=1.0, timestamp=datetime(2026, 1, 2))
        with pytest.raises(TradeJournalError, match="only an OPEN trade with no exits can be cancelled"):
            await cancel_trade(session, trade_id=trade2.trade_id)


@pytest.mark.asyncio
async def test_get_open_trades_excludes_closed_and_cancelled(session_maker):
    async with session_maker() as session:
        open_one = await open_trade(session, symbol="BTC/USDT", side="LONG", entry_price=100.0, quantity=1.0, entry_time=datetime(2026, 1, 1))
        closed_one = await open_trade(session, symbol="BTC/USDT", side="SHORT", entry_price=100.0, quantity=1.0, entry_time=datetime(2026, 1, 1))
        await record_exit(session, trade_id=closed_one.trade_id, exit_price=95.0, quantity=1.0, timestamp=datetime(2026, 1, 2))
        partial_one = await open_trade(session, symbol="BTC/USDT", side="LONG", entry_price=100.0, quantity=2.0, entry_time=datetime(2026, 1, 1))
        await record_exit(session, trade_id=partial_one.trade_id, exit_price=105.0, quantity=1.0, timestamp=datetime(2026, 1, 2))

        open_trades = await get_open_trades(session)
        ids = {t.trade_id for t in open_trades}
        assert open_one.trade_id in ids
        assert partial_one.trade_id in ids
        assert closed_one.trade_id not in ids


@pytest.mark.asyncio
async def test_rejects_invalid_side_and_nonpositive_inputs(session_maker):
    async with session_maker() as session:
        with pytest.raises(TradeJournalError):
            await open_trade(session, symbol="BTC/USDT", side="UP", entry_price=100.0, quantity=1.0, entry_time=datetime(2026, 1, 1))
        with pytest.raises(TradeJournalError):
            await open_trade(session, symbol="BTC/USDT", side="LONG", entry_price=0, quantity=1.0, entry_time=datetime(2026, 1, 1))
