"""Periodic reconciliation of AlphaOne's local execution state against
the real CoinDCX account (Phase 19-20). Read-only -- uses only the
existing, already-verified CoinDCXReadOnlyAccountProvider.get_open_positions()
(services/exchange/coindcx.py), never a new endpoint. Never assumes the
local DB is the final truth: every discrepancy is reported, none is
silently resolved by overwriting one side or the other.
"""
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import LiveExecution, LiveExecutionStatus
from services.exchange.base import ExchangeAccountProvider


@dataclass
class ReconciliationMismatch:
    symbol: str
    kind: str  # "UNEXPECTED_OPEN_POSITION" / "MISSING_POSITION" / "QUANTITY_MISMATCH" / "SIDE_MISMATCH"
    detail: str


@dataclass
class ReconciliationReport:
    checked_at_ok: bool
    local_open_symbols: set
    exchange_open_symbols: set
    mismatches: list = field(default_factory=list)

    @property
    def is_consistent(self) -> bool:
        return self.checked_at_ok and not self.mismatches


async def reconcile_positions(session: AsyncSession, provider: ExchangeAccountProvider) -> ReconciliationReport:
    local_rows = (await session.execute(
        select(LiveExecution).where(
            LiveExecution.status.in_([LiveExecutionStatus.POSITION_OPEN.value, LiveExecutionStatus.PARTIAL_EXIT.value])
        )
    )).scalars().all()
    local_by_symbol = {row.symbol: row for row in local_rows}

    try:
        exchange_positions = await provider.get_open_positions()
    except Exception:
        return ReconciliationReport(checked_at_ok=False, local_open_symbols=set(local_by_symbol), exchange_open_symbols=set())

    exchange_by_symbol = {p["symbol"]: p for p in exchange_positions}

    mismatches: list[ReconciliationMismatch] = []

    for symbol, position in exchange_by_symbol.items():
        if symbol not in local_by_symbol:
            mismatches.append(ReconciliationMismatch(
                symbol=symbol, kind="UNEXPECTED_OPEN_POSITION",
                detail=f"CoinDCX shows an open {position.get('side')} position on {symbol} that AlphaOne has no local record of.",
            ))

    for symbol, local in local_by_symbol.items():
        if symbol not in exchange_by_symbol:
            mismatches.append(ReconciliationMismatch(
                symbol=symbol, kind="MISSING_POSITION",
                detail=f"AlphaOne's local state shows {symbol} as open/partially-closed but CoinDCX reports no such position.",
            ))
            continue
        exch = exchange_by_symbol[symbol]
        exch_side = "LONG" if exch.get("side") in ("LONG", "buy") else "SHORT"
        if exch_side != local.direction:
            mismatches.append(ReconciliationMismatch(
                symbol=symbol, kind="SIDE_MISMATCH",
                detail=f"Local direction={local.direction}, exchange side={exch.get('side')}.",
            ))
        if local.quantity is not None and exch.get("quantity") is not None:
            if abs(float(exch["quantity"]) - float(local.quantity)) > 1e-9:
                mismatches.append(ReconciliationMismatch(
                    symbol=symbol, kind="QUANTITY_MISMATCH",
                    detail=f"Local quantity={local.quantity}, exchange quantity={exch.get('quantity')}.",
                ))

    return ReconciliationReport(
        checked_at_ok=True, local_open_symbols=set(local_by_symbol), exchange_open_symbols=set(exchange_by_symbol),
        mismatches=mismatches,
    )
