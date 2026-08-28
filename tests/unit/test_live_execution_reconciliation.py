"""Live Futures Auto-Trading V1, Phase 19-20: periodic reconciliation of
local execution state against the real CoinDCX account. Read-only, built
entirely on the already-verified ExchangeAccountProvider.get_open_positions()
interface. Never assumes the local DB is final truth -- every discrepancy
is reported, none silently auto-resolved."""
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import LiveExecution, LiveExecutionStatus
from services.exchange.base import ExchangeAccountProvider
from services.live_execution.reconciliation import reconcile_positions


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class _FakeProvider(ExchangeAccountProvider):
    def __init__(self, positions=None, raise_on_positions=False):
        self._positions = positions or []
        self._raise = raise_on_positions

    async def get_connection_status(self):
        return {"status": "OK"}

    async def get_balance(self):
        return {"total_equity": 100000.0}

    async def get_open_positions(self):
        if self._raise:
            raise RuntimeError("CoinDCX API timeout")
        return self._positions

    async def get_trade_history(self):
        return []


def _local_open(symbol="BTC/USDT", direction="LONG", quantity=0.01, status=LiveExecutionStatus.POSITION_OPEN.value):
    return LiveExecution(
        idempotency_key=f"k-{symbol}", source="ALPHAONE_STRATEGY", symbol=symbol,
        direction=direction, status=status, quantity=quantity,
    )


async def test_no_local_and_no_exchange_positions_is_consistent(session_maker):
    async with session_maker() as session:
        report = await reconcile_positions(session, _FakeProvider(positions=[]))
        assert report.is_consistent is True
        assert report.mismatches == []


async def test_matching_local_and_exchange_position_is_consistent(session_maker):
    async with session_maker() as session:
        session.add(_local_open())
        await session.commit()
        provider = _FakeProvider(positions=[{"symbol": "BTC/USDT", "side": "LONG", "quantity": 0.01}])
        report = await reconcile_positions(session, provider)
        assert report.is_consistent is True


async def test_unexpected_exchange_position_with_no_local_record_is_flagged(session_maker):
    async with session_maker() as session:
        provider = _FakeProvider(positions=[{"symbol": "ETH/USDT", "side": "SHORT", "quantity": 1.5}])
        report = await reconcile_positions(session, provider)
        assert report.is_consistent is False
        kinds = [m.kind for m in report.mismatches]
        assert "UNEXPECTED_OPEN_POSITION" in kinds


async def test_local_position_missing_from_exchange_is_flagged(session_maker):
    async with session_maker() as session:
        session.add(_local_open(symbol="SOL/USDT"))
        await session.commit()
        report = await reconcile_positions(session, _FakeProvider(positions=[]))
        assert report.is_consistent is False
        kinds = [m.kind for m in report.mismatches]
        assert "MISSING_POSITION" in kinds


async def test_side_mismatch_is_flagged(session_maker):
    async with session_maker() as session:
        session.add(_local_open(symbol="BTC/USDT", direction="LONG"))
        await session.commit()
        provider = _FakeProvider(positions=[{"symbol": "BTC/USDT", "side": "SHORT", "quantity": 0.01}])
        report = await reconcile_positions(session, provider)
        assert report.is_consistent is False
        kinds = [m.kind for m in report.mismatches]
        assert "SIDE_MISMATCH" in kinds


async def test_quantity_mismatch_is_flagged(session_maker):
    async with session_maker() as session:
        session.add(_local_open(symbol="BTC/USDT", quantity=0.01))
        await session.commit()
        provider = _FakeProvider(positions=[{"symbol": "BTC/USDT", "side": "LONG", "quantity": 0.05}])
        report = await reconcile_positions(session, provider)
        assert report.is_consistent is False
        kinds = [m.kind for m in report.mismatches]
        assert "QUANTITY_MISMATCH" in kinds


async def test_partial_exit_status_is_included_in_local_open_set(session_maker):
    async with session_maker() as session:
        session.add(_local_open(symbol="BTC/USDT", status=LiveExecutionStatus.PARTIAL_EXIT.value))
        await session.commit()
        report = await reconcile_positions(session, _FakeProvider(positions=[{"symbol": "BTC/USDT", "side": "LONG", "quantity": 0.01}]))
        assert "BTC/USDT" in report.local_open_symbols


async def test_rejected_execution_is_never_considered_a_local_open_position(session_maker):
    """A REJECTED row (the overwhelming majority today, since
    ORDER_CONTRACT_VERIFIED can never pass) must never be mistaken for an
    open position -- otherwise every rejected candidate would spuriously
    show up as a MISSING_POSITION mismatch."""
    async with session_maker() as session:
        session.add(_local_open(symbol="BTC/USDT", status=LiveExecutionStatus.REJECTED.value))
        await session.commit()
        report = await reconcile_positions(session, _FakeProvider(positions=[]))
        assert report.is_consistent is True
        assert "BTC/USDT" not in report.local_open_symbols


async def test_provider_failure_is_reported_not_silently_treated_as_consistent(session_maker):
    """Phase 19: never assume the local DB is final truth -- a failed
    exchange read must be surfaced as checked_at_ok=False, never as a
    silent 'everything matches'."""
    async with session_maker() as session:
        session.add(_local_open(symbol="BTC/USDT"))
        await session.commit()
        report = await reconcile_positions(session, _FakeProvider(raise_on_positions=True))
        assert report.checked_at_ok is False
        assert report.is_consistent is False


async def test_multiple_mismatches_are_all_reported_not_short_circuited(session_maker):
    async with session_maker() as session:
        session.add(_local_open(symbol="BTC/USDT"))
        session.add(_local_open(symbol="ETH/USDT"))
        await session.commit()
        provider = _FakeProvider(positions=[{"symbol": "SOL/USDT", "side": "LONG", "quantity": 1.0}])
        report = await reconcile_positions(session, provider)
        assert len(report.mismatches) == 3  # BTC missing, ETH missing, SOL unexpected
