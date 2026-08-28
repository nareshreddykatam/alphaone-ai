"""Periodic reconciliation of AlphaOne's local execution state against
the real CoinDCX account (Phase 19-20). Read-only -- uses only the
existing, already-verified CoinDCXReadOnlyAccountProvider.get_open_positions()
(services/exchange/coindcx.py), never a new endpoint. Never assumes the
local DB is the final truth: every discrepancy is reported, none is
silently resolved by overwriting one side or the other.
"""
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import BotState, LiveExecution, LiveExecutionStatus
from services.exchange.base import ExchangeAccountProvider

# Contract Audit V2, Phase 10: how far a local entry_price may differ from
# the exchange's own reported entry_price before it counts as a real
# discrepancy rather than float/rounding noise.
ENTRY_PRICE_MISMATCH_TOLERANCE_PCT = 0.5

RECONCILIATION_STATE_KEY = "live_execution_last_reconciliation"


@dataclass
class ReconciliationMismatch:
    symbol: str
    kind: str  # "UNEXPECTED_OPEN_POSITION" / "MISSING_POSITION" / "QUANTITY_MISMATCH" / "SIDE_MISMATCH" / "ENTRY_PRICE_MISMATCH"
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
        if local.entry_price is not None and exch.get("entry_price") is not None and float(local.entry_price) > 0:
            deviation_pct = abs(float(exch["entry_price"]) - float(local.entry_price)) / float(local.entry_price) * 100
            if deviation_pct > ENTRY_PRICE_MISMATCH_TOLERANCE_PCT:
                mismatches.append(ReconciliationMismatch(
                    symbol=symbol, kind="ENTRY_PRICE_MISMATCH",
                    detail=f"Local entry_price={local.entry_price}, exchange entry_price={exch.get('entry_price')} "
                           f"({deviation_pct:.2f}% apart, tolerance {ENTRY_PRICE_MISMATCH_TOLERANCE_PCT}%).",
                ))

    return ReconciliationReport(
        checked_at_ok=True, local_open_symbols=set(local_by_symbol), exchange_open_symbols=set(exchange_by_symbol),
        mismatches=mismatches,
    )


async def record_reconciliation_result(session: AsyncSession, report: ReconciliationReport) -> None:
    """Persists the last reconciliation outcome durably (same BotState
    pattern as services/live_execution/kill_switch.py's emergency stop) so
    the RECONCILIATION_OK gate (services/live_execution/gates.py) can
    check it on every candidate without re-running a real exchange call
    per candidate -- reconciliation itself only needs to run periodically
    (Phase 10: "periodically reconcile"), but its LAST RESULT must gate
    every single new entry until the next successful run clears it."""
    row = (await session.execute(select(BotState).where(BotState.key == RECONCILIATION_STATE_KEY))).scalar_one_or_none()
    value = {
        "is_consistent": report.is_consistent,
        "checked_at_ok": report.checked_at_ok,
        "mismatch_count": len(report.mismatches),
        "mismatches": [{"symbol": m.symbol, "kind": m.kind, "detail": m.detail} for m in report.mismatches],
        "recorded_at": datetime.utcnow().isoformat(),
    }
    if row is None:
        session.add(BotState(key=RECONCILIATION_STATE_KEY, value=value))
    else:
        row.value = value
    await session.commit()


async def get_last_reconciliation_status(session: AsyncSession) -> tuple[bool, str]:
    """Returns (ok, reason) for the RECONCILIATION_OK gate. FAIL-CLOSED if
    reconciliation has never run in this database at all -- Phase 9's
    fail-safe-entry list requires this gate to genuinely gate, and "never
    checked" must never be silently treated as "consistent" (Section 34:
    "if something cannot be safely determined, leave live execution
    disabled")."""
    row = (await session.execute(select(BotState).where(BotState.key == RECONCILIATION_STATE_KEY))).scalar_one_or_none()
    if row is None:
        return False, "Position reconciliation has never run -- cannot confirm local state matches the real CoinDCX account."
    value = row.value
    if not value.get("checked_at_ok", False):
        return False, "The last reconciliation attempt could not read the real CoinDCX account (exchange read failure)."
    if not value.get("is_consistent", False):
        return False, f"The last reconciliation found {value.get('mismatch_count', '?')} discrepancy(ies) between local state and the real CoinDCX account -- halting new entries."
    return True, "OK"
