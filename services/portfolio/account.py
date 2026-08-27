"""AlphaOne is single-account. This lazily creates that one Account row the
first time anything needs it, rather than requiring a separate setup step.

Phase 5: the active exchange is CoinDCX (SunCrypto's read-only account API
never existed -- see docs/known_limitations.md; Phase 4's SunCrypto
provider is kept for historical reference, not deleted, per Phase 5
section 7). connection_status starts at NOT_CONNECTED and only moves to
LIVE once a real CoinDCX sync succeeds (services/exchange/coindcx_sync.py)
-- it never silently claims a connection that doesn't exist.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import Account, ConnectionState


async def get_or_create_default_account(session: AsyncSession) -> Account:
    result = await session.execute(select(Account).order_by(Account.created_at).limit(1))
    account = result.scalar_one_or_none()
    if account is not None:
        return account

    account = Account(
        exchange="coindcx",
        mode="live",
        connection_status=ConnectionState.NOT_CONFIGURED.value,
        label="Primary",
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account
