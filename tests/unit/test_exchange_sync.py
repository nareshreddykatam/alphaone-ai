"""Phase 4G: the sync scaffold must record an honest audit trail and never
promote an account to LIVE when the provider reports UNAVAILABLE (which is
SunCrypto's real, current state -- no authenticated account API exists)."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import SyncEvent, SyncStatus, ConnectionState
from services.exchange.base import ExchangeAccountProvider
from services.exchange.suncrypto import SunCryptoReadOnlyAccountProvider
from services.exchange.sync import run_sync_once, get_last_sync_event, is_stale
from services.portfolio.account import get_or_create_default_account


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class _AlwaysFailsProvider(ExchangeAccountProvider):
    async def get_connection_status(self):
        raise ConnectionError("simulated network failure")

    async def get_balance(self):
        raise NotImplementedError

    async def get_open_positions(self):
        raise NotImplementedError

    async def get_trade_history(self):
        raise NotImplementedError


class _FakeLiveProvider(ExchangeAccountProvider):
    async def get_connection_status(self):
        return {"status": "OK"}

    async def get_balance(self):
        return {}

    async def get_open_positions(self):
        return []

    async def get_trade_history(self):
        return []


@pytest.mark.asyncio
async def test_sync_with_suncrypto_provider_reports_unavailable_and_stays_unconfigured(session_maker):
    """SunCrypto is kept only for historical reference (Phase 5 replaced it
    with CoinDCX as the active exchange) -- this proves the generic sync
    scaffold still behaves correctly against it: never fabricates a
    connection."""
    async with session_maker() as session:
        account = await get_or_create_default_account(session)
        assert account.connection_status == ConnectionState.NOT_CONFIGURED.value

        event = await run_sync_once(session, SunCryptoReadOnlyAccountProvider())
        assert event.status == SyncStatus.UNAVAILABLE.value
        assert "no documented authenticated account API" in event.detail

        await session.refresh(account)
        assert account.connection_status == ConnectionState.NOT_CONFIGURED.value  # never silently promoted


@pytest.mark.asyncio
async def test_sync_failure_is_recorded_not_swallowed(session_maker):
    async with session_maker() as session:
        event = await run_sync_once(session, _AlwaysFailsProvider())
        assert event.status == SyncStatus.FAILED.value
        assert "simulated network failure" in event.detail


@pytest.mark.asyncio
async def test_a_hypothetically_working_provider_would_promote_to_live(session_maker):
    """Proves the scaffold is genuinely wired, not hardcoded to UNAVAILABLE
    -- if a provider ever reports a real connection, the account correctly
    updates. This is what would happen if SunCrypto ever published a real API."""
    async with session_maker() as session:
        account = await get_or_create_default_account(session)
        event = await run_sync_once(session, _FakeLiveProvider())
        assert event.status == SyncStatus.SUCCESS.value
        await session.refresh(account)
        assert account.connection_status == ConnectionState.LIVE.value


@pytest.mark.asyncio
async def test_staleness_check(session_maker):
    async with session_maker() as session:
        assert await get_last_sync_event(session) is None
        assert is_stale(None) is True

        await run_sync_once(session, SunCryptoReadOnlyAccountProvider())
        event = await get_last_sync_event(session)
        assert is_stale(event, now=event.timestamp) is False
        assert is_stale(event, now=event.timestamp + timedelta(hours=2)) is True
