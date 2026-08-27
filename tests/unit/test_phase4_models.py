"""Phase 4, section 32: new DB tables (accounts, snapshots, deposits/
withdrawals, trade executions, signal outcomes, sync events) must round-trip
correctly and the Trade->Account link must resolve. Not a business-logic
test -- just confirms the schema is usable end to end."""
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import (
    Account,
    AccountSnapshot,
    Deposit,
    Withdrawal,
    Trade,
    TradeExecution,
    Signal,
    SignalOutcome,
    SyncEvent,
    AccountConnectionStatus,
    ExecutionType,
    SignalOutcomeType,
    SyncStatus,
)


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_account_defaults_to_not_connected(session_maker):
    async with session_maker() as session:
        account = Account()
        session.add(account)
        await session.commit()
        await session.refresh(account)
        assert account.connection_status == AccountConnectionStatus.NOT_CONNECTED.value
        # Phase 5: CoinDCX is the active exchange (see services/portfolio/account.py)
        assert account.exchange == "coindcx"


@pytest.mark.asyncio
async def test_trade_links_to_account_and_executions(session_maker):
    async with session_maker() as session:
        account = Account()
        session.add(account)
        await session.flush()

        trade = Trade(
            trade_id="T1",
            side="LONG",
            entry_price=100.0,
            quantity=1.0,
            entry_time=datetime(2026, 1, 1),
            account_id=account.id,
        )
        session.add(trade)
        await session.flush()

        execution = TradeExecution(
            trade_id=trade.trade_id,
            execution_type=ExecutionType.ENTRY.value,
            price=100.0,
            quantity=1.0,
            timestamp=datetime(2026, 1, 1),
        )
        session.add(execution)
        await session.commit()

        result = await session.execute(select(Trade).where(Trade.trade_id == "T1"))
        fetched = result.scalar_one()
        assert fetched.account_id == account.id
        assert fetched.is_manual_entry is True
        assert fetched.source == "MANUAL"

        result = await session.execute(
            select(TradeExecution).where(TradeExecution.trade_id == "T1")
        )
        executions = result.scalars().all()
        assert len(executions) == 1
        assert executions[0].execution_type == ExecutionType.ENTRY.value


@pytest.mark.asyncio
async def test_deposits_and_withdrawals_are_separate_tables_from_trades(session_maker):
    async with session_maker() as session:
        account = Account()
        session.add(account)
        await session.flush()

        session.add(Deposit(account_id=account.id, amount=1000.0, timestamp=datetime(2026, 1, 1)))
        session.add(Withdrawal(account_id=account.id, amount=200.0, timestamp=datetime(2026, 1, 2)))
        await session.commit()

        deposits = (await session.execute(select(Deposit))).scalars().all()
        withdrawals = (await session.execute(select(Withdrawal))).scalars().all()
        trades = (await session.execute(select(Trade))).scalars().all()
        assert len(deposits) == 1 and deposits[0].amount == 1000.0
        assert len(withdrawals) == 1 and withdrawals[0].amount == 200.0
        assert len(trades) == 0  # deposits/withdrawals never create Trade rows


@pytest.mark.asyncio
async def test_signal_outcome_tracks_hypothetical_result_independent_of_trade(session_maker):
    async with session_maker() as session:
        signal = Signal(
            signal_id="S1",
            timestamp=datetime(2026, 1, 1),
            signal_type="LONG",
            confidence=0.6,
        )
        session.add(signal)
        await session.flush()

        outcome = SignalOutcome(
            signal_id="S1",
            outcome=SignalOutcomeType.WIN.value,
            hypothetical_pnl=50.0,
            was_taken_by_user=False,
        )
        session.add(outcome)
        await session.commit()

        fetched = (
            await session.execute(select(SignalOutcome).where(SignalOutcome.signal_id == "S1"))
        ).scalar_one()
        assert fetched.outcome == SignalOutcomeType.WIN.value
        assert fetched.was_taken_by_user is False  # a WIN can exist with zero real trades


@pytest.mark.asyncio
async def test_account_snapshot_and_sync_event_roundtrip(session_maker):
    async with session_maker() as session:
        account = Account()
        session.add(account)
        await session.flush()

        session.add(AccountSnapshot(account_id=account.id, timestamp=datetime(2026, 1, 1), equity=10000.0))
        session.add(SyncEvent(source="suncrypto", status=SyncStatus.UNAVAILABLE.value, detail="no documented account API"))
        await session.commit()

        snapshots = (await session.execute(select(AccountSnapshot))).scalars().all()
        sync_events = (await session.execute(select(SyncEvent))).scalars().all()
        assert len(snapshots) == 1 and snapshots[0].equity == 10000.0
        assert len(sync_events) == 1 and sync_events[0].status == SyncStatus.UNAVAILABLE.value
