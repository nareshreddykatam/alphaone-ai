"""AI Trading V1, Phase 11: paper-trade history must survive in the
Trade/TradeExecution tables -- the same tables and mode="paper" convention
the existing trade journal already uses -- with the exact numbers the
in-memory PaperTrader already computed, never re-derived."""
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import Trade, TradeExecution, TradeStatus, TradeSource, ExecutionType
from services.paper_trader.engine import PaperTrader, PaperPosition
from services.paper_trader.persistence import get_open_paper_trade, persist_paper_open, persist_paper_event
from services.risk_engine.engine import RiskConfig


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _position(trade_id="PAPER-000001", tp2=None, tp3=None):
    return PaperPosition(
        trade_id=trade_id, signal_id="SIG-1", side="LONG", entry_price=100.0,
        entry_time=datetime(2026, 1, 1), stop_loss=90.0, take_profit_1=110.0,
        take_profit_2=tp2, take_profit_3=tp3, quantity=1.0, leverage=1,
        market_regime="TRENDING_BULLISH", strategy_name="S06_SUPERTREND_ATR_4H",
    )


async def test_persist_paper_open_creates_trade_and_entry_execution(session_maker):
    async with session_maker() as session:
        position = _position()
        trade = await persist_paper_open(session, position)

        assert trade.mode == "paper"
        assert trade.source == TradeSource.AI_PAPER.value
        assert trade.status == TradeStatus.OPEN.value
        assert trade.stop_loss == pytest.approx(90.0)
        assert trade.take_profit_1 == pytest.approx(110.0)

        executions = (await session.execute(select(TradeExecution))).scalars().all()
        assert len(executions) == 1
        assert executions[0].execution_type == ExecutionType.ENTRY.value


async def test_get_open_paper_trade_finds_open_and_partially_closed_but_not_closed(session_maker):
    async with session_maker() as session:
        await persist_paper_open(session, _position("PAPER-000001"))
        found = await get_open_paper_trade(session, "BTC/USDT")
        assert found is not None and found.trade_id == "PAPER-000001"

        await persist_paper_event(session, {
            "event_type": "exit", "trade_id": "PAPER-000001", "signal_id": "SIG-1", "side": "LONG",
            "exit_price": 110.0, "exit_time": datetime(2026, 1, 1, 4), "quantity": 1.0,
            "pnl": 10.0, "pnl_pct": 10.0, "fees": 0.1, "r_multiple": 1.0, "exit_reason": "take_profit",
        })
        found_after_close = await get_open_paper_trade(session, "BTC/USDT")
        assert found_after_close is None


async def test_persist_partial_exit_marks_partially_closed_without_closing_trade(session_maker):
    async with session_maker() as session:
        await persist_paper_open(session, _position("PAPER-000002", tp2=120.0, tp3=130.0))

        await persist_paper_event(session, {
            "event_type": "partial_exit", "trade_id": "PAPER-000002", "signal_id": "SIG-1",
            "side": "LONG", "exit_price": 110.0, "exit_time": datetime(2026, 1, 1, 4),
            "quantity": 0.4, "target_index": 1, "pnl": 4.0, "fees": 0.05, "equity_after": 10004.0,
        })

        trade = (await session.execute(select(Trade).where(Trade.trade_id == "PAPER-000002"))).scalar_one()
        assert trade.status == TradeStatus.PARTIALLY_CLOSED.value
        assert trade.exit_time is None  # not fully closed yet -- no final exit fields set

        executions = (await session.execute(
            select(TradeExecution).where(TradeExecution.trade_id == "PAPER-000002")
        )).scalars().all()
        assert len(executions) == 2  # entry + partial exit
        assert any(e.execution_type == ExecutionType.PARTIAL_EXIT.value for e in executions)


async def test_persist_final_exit_writes_trade_level_pnl_fields(session_maker):
    async with session_maker() as session:
        await persist_paper_open(session, _position("PAPER-000003"))
        await persist_paper_event(session, {
            "event_type": "exit", "trade_id": "PAPER-000003", "signal_id": "SIG-1", "side": "LONG",
            "exit_price": 90.0, "exit_time": datetime(2026, 1, 1, 4), "quantity": 1.0,
            "pnl": -10.0, "pnl_pct": -10.0, "fees": 0.1, "r_multiple": -1.0, "exit_reason": "stop_loss",
        })
        trade = (await session.execute(select(Trade).where(Trade.trade_id == "PAPER-000003"))).scalar_one()
        assert trade.status == TradeStatus.CLOSED.value
        assert trade.pnl == pytest.approx(-10.0)
        assert trade.exit_reason == "stop_loss"


async def test_persist_event_for_unknown_trade_id_is_a_safe_no_op(session_maker):
    async with session_maker() as session:
        # Never crashes if the Trade row is missing (e.g. process restarted
        # mid-position) -- just nothing to update.
        await persist_paper_event(session, {
            "event_type": "exit", "trade_id": "DOES-NOT-EXIST", "signal_id": "SIG-1", "side": "LONG",
            "exit_price": 90.0, "exit_time": datetime(2026, 1, 1), "quantity": 1.0,
            "pnl": -10.0, "pnl_pct": -10.0, "fees": 0.1, "r_multiple": -1.0, "exit_reason": "stop_loss",
        })
