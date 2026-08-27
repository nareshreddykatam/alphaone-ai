"""Phase 5: CoinDCX position/wallet/trade synchronization. Uses a fake
provider (not real HTTP) so these tests exercise the sync LOGIC --
matching, idempotency, honest failure handling -- independent of the
already-tested HTTP/auth layer (tests/unit/test_coindcx_provider.py).
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import (
    Trade, TradeStatus, TradeSource, SignalMatchStatus, ConnectionState,
    Signal, AccountSnapshot, SyncEvent, SyncStatus,
)
from services.portfolio.account import get_or_create_default_account
from services.exchange.coindcx_sync import sync_balance, sync_positions, sync_trade_fills


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class FakeProvider:
    def __init__(self, balance=None, positions=None, trade_history=None):
        self._balance = balance or {"status": "OK", "total_equity": 1000.0, "available_balance": 900.0, "used_margin": 100.0}
        self._positions = positions or []
        self._trade_history = trade_history or []

    async def get_balance(self):
        return self._balance

    async def get_open_positions(self):
        return self._positions

    async def get_trade_history(self, symbol="BTC/USDT", from_date="", to_date=""):
        return self._trade_history


def _position(pos_id="p1", symbol="B-BTC_USDT", side="LONG", entry=100.0, mark=110.0, qty=1.0, leverage=5.0):
    active_pos = qty if side == "LONG" else -qty
    return {
        "exchange_position_id": pos_id, "symbol": symbol, "side": side, "quantity": qty,
        "entry_price": entry, "mark_price": mark, "liquidation_price": 50.0,
        "leverage": leverage, "margin": 20.0, "margin_type": "isolated",
        "unrealized_pnl": (mark - entry) * active_pos, "updated_at": 123,
    }


@pytest.mark.asyncio
async def test_sync_balance_ok_creates_snapshot_and_marks_live(session_maker):
    async with session_maker() as session:
        provider = FakeProvider()
        result = await sync_balance(session, provider)
        assert result["status"] == "OK"

        account = await get_or_create_default_account(session)
        assert account.connection_status == ConnectionState.LIVE.value
        assert account.last_synced_at is not None

        snapshots = (await session.execute(select(AccountSnapshot))).scalars().all()
        assert len(snapshots) == 1
        assert snapshots[0].equity == 1000.0
        assert snapshots[0].used_margin == 100.0


@pytest.mark.asyncio
async def test_sync_balance_not_configured_never_claims_live(session_maker):
    async with session_maker() as session:
        provider = FakeProvider(balance={"status": "NOT_CONFIGURED", "total_equity": None, "available_balance": None, "used_margin": None})
        await sync_balance(session, provider)
        account = await get_or_create_default_account(session)
        assert account.connection_status == ConnectionState.NOT_CONFIGURED.value


@pytest.mark.asyncio
async def test_sync_balance_failure_marks_disconnected_and_logs_event(session_maker):
    async with session_maker() as session:
        provider = FakeProvider(balance={"status": "CONNECTION_LOST", "total_equity": None, "available_balance": None, "used_margin": None})
        await sync_balance(session, provider)
        account = await get_or_create_default_account(session)
        assert account.connection_status == ConnectionState.DISCONNECTED.value

        events = (await session.execute(select(SyncEvent))).scalars().all()
        assert any(e.status == SyncStatus.FAILED.value for e in events)


@pytest.mark.asyncio
async def test_new_position_with_no_signal_candidate_is_unmatched(session_maker):
    async with session_maker() as session:
        provider = FakeProvider(positions=[_position()])
        result = await sync_positions(session, provider)
        assert len(result["opened"]) == 1
        trade = result["opened"][0]
        assert trade.match_status == SignalMatchStatus.UNMATCHED.value
        assert trade.signal_id is None
        assert trade.source == TradeSource.COINDCX_SYNC.value
        assert trade.is_manual_entry is False
        assert trade.symbol == "BTC/USDT"  # denormalized from B-BTC_USDT


@pytest.mark.asyncio
async def test_new_position_auto_matches_a_confident_signal(session_maker):
    async with session_maker() as session:
        now = datetime.utcnow()
        session.add(Signal(signal_id="SIG-1", timestamp=now, symbol="BTC/USDT", signal_type="LONG", confidence=0.0, entry_price=100.0))
        await session.commit()

        provider = FakeProvider(positions=[_position(entry=100.0)])
        result = await sync_positions(session, provider)
        trade = result["opened"][0]
        assert trade.match_status == SignalMatchStatus.AUTO_MATCHED.value
        assert trade.signal_id == "SIG-1"


@pytest.mark.asyncio
async def test_new_position_with_two_close_signals_is_ambiguous(session_maker):
    async with session_maker() as session:
        now = datetime.utcnow()
        session.add(Signal(signal_id="SIG-A", timestamp=now, symbol="BTC/USDT", signal_type="LONG", confidence=0.0, entry_price=100.0))
        session.add(Signal(signal_id="SIG-B", timestamp=now, symbol="BTC/USDT", signal_type="LONG", confidence=0.0, entry_price=100.05))
        await session.commit()

        provider = FakeProvider(positions=[_position(entry=100.02)])
        result = await sync_positions(session, provider)
        trade = result["opened"][0]
        assert trade.match_status == SignalMatchStatus.AMBIGUOUS.value
        assert trade.signal_id is None  # never auto-picked


@pytest.mark.asyncio
async def test_existing_position_gets_live_mark_price_and_pnl_updated(session_maker):
    async with session_maker() as session:
        provider = FakeProvider(positions=[_position(mark=110.0)])
        result = await sync_positions(session, provider)
        trade_id = result["opened"][0].trade_id

        provider2 = FakeProvider(positions=[_position(mark=115.0)])
        result2 = await sync_positions(session, provider2)
        assert len(result2["updated"]) == 1
        assert result2["updated"][0].trade_id == trade_id
        assert result2["updated"][0].mark_price == 115.0


@pytest.mark.asyncio
async def test_disappeared_position_closes_using_real_fill_data(session_maker):
    async with session_maker() as session:
        provider = FakeProvider(positions=[_position(pos_id="p1", entry=100.0, qty=1.0)])
        result = await sync_positions(session, provider)
        trade_id = result["opened"][0].trade_id

        provider2 = FakeProvider(
            positions=[],  # position disappeared
            trade_history=[{"price": 120.0, "quantity": 1.0, "side": "sell", "order_id": "o1", "timestamp": 1}],
        )
        result2 = await sync_positions(session, provider2)
        assert len(result2["closed"]) == 1
        closed = result2["closed"][0]
        assert closed.trade_id == trade_id
        assert closed.status == TradeStatus.CLOSED.value
        assert closed.exit_price == 120.0
        assert closed.pnl > 0


@pytest.mark.asyncio
async def test_disappeared_position_with_no_closing_fill_stays_open_and_flags_event(session_maker):
    async with session_maker() as session:
        provider = FakeProvider(positions=[_position(pos_id="p1")])
        result = await sync_positions(session, provider)
        trade_id = result["opened"][0].trade_id

        provider2 = FakeProvider(positions=[], trade_history=[])  # no fills found at all
        result2 = await sync_positions(session, provider2)
        assert result2["closed"] == []

        trade = (await session.execute(select(Trade).where(Trade.trade_id == trade_id))).scalar_one()
        assert trade.status == TradeStatus.OPEN.value  # never guessed closed

        events = (await session.execute(select(SyncEvent))).scalars().all()
        assert any("no closing fill was found" in (e.detail or "") for e in events)


@pytest.mark.asyncio
async def test_sync_trade_fills_is_idempotent(session_maker):
    async with session_maker() as session:
        provider = FakeProvider(positions=[_position(pos_id="p1")])
        await sync_positions(session, provider)  # creates a Trade to attach fills to

        fills = [{"price": 100.0, "quantity": 0.5, "side": "buy", "order_id": "o1", "timestamp": 1000}]
        provider_with_fills = FakeProvider(trade_history=fills)

        first = await sync_trade_fills(session, provider_with_fills)
        second = await sync_trade_fills(session, provider_with_fills)  # same fills again
        assert first == 1
        assert second == 0  # already ingested, not duplicated
