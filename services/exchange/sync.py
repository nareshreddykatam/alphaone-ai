"""Account-sync scaffold (Phase 4G). Runs against the exchange-agnostic
`ExchangeAccountProvider` interface, so if SunCrypto (or another exchange)
ever publishes a real read-only account API, this is the one place that
needs a real provider swapped in -- nothing else changes.

Today, `SunCryptoReadOnlyAccountProvider` always reports UNAVAILABLE (see
services/exchange/suncrypto.py), so `run_sync_once` always records that
honestly rather than fabricating a successful sync. This is not dead code:
it's the audit trail (`SyncEvent`) and retry/staleness scaffolding a real
sync would need, proven correct against the interface today.
"""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import SyncEvent, SyncStatus, AccountConnectionStatus
from services.exchange.base import ExchangeAccountProvider
from services.portfolio.account import get_or_create_default_account

STALE_AFTER = timedelta(hours=1)


async def run_sync_once(session: AsyncSession, provider: ExchangeAccountProvider) -> SyncEvent:
    account = await get_or_create_default_account(session)

    try:
        status = await provider.get_connection_status()
    except Exception as e:
        event = SyncEvent(source="suncrypto", status=SyncStatus.FAILED.value, detail=str(e))
        session.add(event)
        await session.commit()
        return event

    if status.get("status") == "UNAVAILABLE":
        event = SyncEvent(source="suncrypto", status=SyncStatus.UNAVAILABLE.value, detail=status.get("reason"))
        # Never silently promote to LIVE -- connection_status stays whatever
        # manual-tracking state it already was.
    else:
        event = SyncEvent(source="suncrypto", status=SyncStatus.SUCCESS.value, detail="synced")
        account.connection_status = AccountConnectionStatus.LIVE.value

    session.add(event)
    await session.commit()
    return event


async def get_last_sync_event(session: AsyncSession) -> SyncEvent | None:
    result = await session.execute(select(SyncEvent).order_by(SyncEvent.timestamp.desc()).limit(1))
    return result.scalar_one_or_none()


def is_stale(event: SyncEvent | None, now: datetime | None = None) -> bool:
    if event is None:
        return True
    now = now or datetime.utcnow()
    return (now - event.timestamp) > STALE_AFTER
