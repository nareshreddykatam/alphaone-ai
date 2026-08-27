from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.config import get_settings
from database.schema import get_db
from database.schema.models import AccountSnapshot, Deposit, Withdrawal
from services.exchange.coindcx import CoinDCXReadOnlyAccountProvider
from services.exchange.coindcx_sync import sync_balance, sync_positions
from services.exchange.sync import get_last_sync_event, is_stale
from services.portfolio.account import get_or_create_default_account
from services.portfolio.service import reconcile_account

_settings = get_settings()


def _provider() -> CoinDCXReadOnlyAccountProvider:
    return CoinDCXReadOnlyAccountProvider(_settings.coindcx_api_key, _settings.coindcx_api_secret)

router = APIRouter()


class SnapshotIn(BaseModel):
    equity: float
    timestamp: Optional[datetime] = None
    note: Optional[str] = None


class CashMovementIn(BaseModel):
    amount: float
    timestamp: Optional[datetime] = None
    note: Optional[str] = None


@router.get("/")
async def get_account(db: AsyncSession = Depends(get_db)):
    account = await get_or_create_default_account(db)
    return {
        "id": str(account.id),
        "exchange": account.exchange,
        "connection_status": account.connection_status,
        "base_currency": account.base_currency,
        "label": account.label,
    }


@router.get("/snapshots")
async def list_snapshots(db: AsyncSession = Depends(get_db)):
    account = await get_or_create_default_account(db)
    result = await db.execute(
        select(AccountSnapshot).where(AccountSnapshot.account_id == account.id).order_by(AccountSnapshot.timestamp)
    )
    rows = result.scalars().all()
    return {"snapshots": [{"timestamp": r.timestamp, "equity": r.equity, "source": r.source, "note": r.note} for r in rows]}


@router.post("/snapshots")
async def add_snapshot(payload: SnapshotIn, db: AsyncSession = Depends(get_db)):
    account = await get_or_create_default_account(db)
    if payload.equity <= 0:
        raise HTTPException(status_code=400, detail="equity must be positive")
    snapshot = AccountSnapshot(
        account_id=account.id, timestamp=payload.timestamp or datetime.utcnow(),
        equity=payload.equity, source="MANUAL", note=payload.note,
    )
    db.add(snapshot)
    await db.commit()
    return {"status": "recorded"}


@router.get("/deposits")
async def list_deposits(db: AsyncSession = Depends(get_db)):
    account = await get_or_create_default_account(db)
    result = await db.execute(select(Deposit).where(Deposit.account_id == account.id).order_by(Deposit.timestamp))
    rows = result.scalars().all()
    return {"deposits": [{"amount": r.amount, "timestamp": r.timestamp, "note": r.note} for r in rows]}


@router.post("/deposits")
async def add_deposit(payload: CashMovementIn, db: AsyncSession = Depends(get_db)):
    account = await get_or_create_default_account(db)
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")
    db.add(Deposit(account_id=account.id, amount=payload.amount, timestamp=payload.timestamp or datetime.utcnow(), note=payload.note))
    await db.commit()
    return {"status": "recorded"}


@router.get("/withdrawals")
async def list_withdrawals(db: AsyncSession = Depends(get_db)):
    account = await get_or_create_default_account(db)
    result = await db.execute(select(Withdrawal).where(Withdrawal.account_id == account.id).order_by(Withdrawal.timestamp))
    rows = result.scalars().all()
    return {"withdrawals": [{"amount": r.amount, "timestamp": r.timestamp, "note": r.note} for r in rows]}


@router.post("/withdrawals")
async def add_withdrawal(payload: CashMovementIn, db: AsyncSession = Depends(get_db)):
    account = await get_or_create_default_account(db)
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be positive")
    db.add(Withdrawal(account_id=account.id, amount=payload.amount, timestamp=payload.timestamp or datetime.utcnow(), note=payload.note))
    await db.commit()
    return {"status": "recorded"}


@router.get("/reconcile")
async def reconcile(initial_equity: float = 0.0, db: AsyncSession = Depends(get_db)):
    account = await get_or_create_default_account(db)
    return await reconcile_account(db, account.id, initial_equity=initial_equity)


@router.post("/sync")
async def sync_account(db: AsyncSession = Depends(get_db)):
    """Real CoinDCX sync: balance + open positions (matched to signals
    where confident, see services/signal_matching/matcher.py). Reports
    NOT_CONFIGURED honestly if no COINDCX_API_KEY/SECRET are set -- never
    fabricates a successful connection."""
    provider = _provider()
    try:
        balance = await sync_balance(db, provider)
        positions = None
        if balance["status"] == "OK":
            result = await sync_positions(db, provider)
            positions = {
                "opened": len(result["opened"]), "updated": len(result["updated"]), "closed": len(result["closed"]),
            }
        return {"balance": balance, "positions": positions}
    finally:
        await provider.close()


@router.get("/balance")
async def get_balance(db: AsyncSession = Depends(get_db)):
    provider = _provider()
    try:
        return await provider.get_balance()
    finally:
        await provider.close()


@router.get("/positions")
async def get_positions(db: AsyncSession = Depends(get_db)):
    provider = _provider()
    try:
        return {"positions": await provider.get_open_positions()}
    finally:
        await provider.close()


@router.get("/sync-status")
async def sync_status(db: AsyncSession = Depends(get_db)):
    event = await get_last_sync_event(db)
    return {
        "last_sync": {"status": event.status, "detail": event.detail, "timestamp": event.timestamp} if event else None,
        "is_stale": is_stale(event),
    }
