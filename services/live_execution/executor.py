"""The single entry point for turning a candidate signal into (attempted)
real execution -- Signal -> Validation -> Risk Engine -> Paper/Live
Execution (Live Futures Auto-Trading V1). Every candidate produces
exactly one durable LiveExecution row, created idempotently so a
Telegram reconnect, duplicate message, scheduler retry, or concurrent
worker can never process the same real-world signal twice (Phase 10-11).

Because services/live_execution/gates.py's ORDER_CONTRACT_VERIFIED gate
can never pass (see that module), every candidate that reaches this
function today ends at REJECTED -- this is intentional, not a bug to fix
later without also fixing the underlying missing contract verification.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import LiveExecution, LiveExecutionStatus
from services.live_execution.gates import LiveExecutionCandidate, check_all_live_execution_gates
from services.live_execution.idempotency import compute_idempotency_key
from services.live_execution.order_client import OrderContractNotVerifiedError, submit_futures_order


async def get_existing_execution(session: AsyncSession, idempotency_key: str) -> Optional[LiveExecution]:
    return (await session.execute(
        select(LiveExecution).where(LiveExecution.idempotency_key == idempotency_key)
    )).scalar_one_or_none()


async def process_live_execution_candidate(
    session: AsyncSession,
    candidate: LiveExecutionCandidate,
    usdt_inr_rate: Optional[float],
    market_data_healthy: bool,
    coindcx_account_healthy: bool,
    daily_loss_ok: bool,
    daily_loss_reason: str,
    reconciliation_ok: bool,
    reconciliation_reason: str,
) -> LiveExecution:
    """Idempotent: calling this twice for the objectively same signal
    (same computed idempotency key) returns the SAME row both times,
    never creates a second one and never re-attempts execution for an
    already-decided candidate."""
    idempotency_key = compute_idempotency_key(candidate.source, candidate.symbol, signal_id=candidate.signal_id)

    existing = await get_existing_execution(session, idempotency_key)
    if existing is not None:
        return existing

    execution = LiveExecution(
        idempotency_key=idempotency_key, source=candidate.source, signal_id=candidate.signal_id,
        symbol=candidate.symbol, instrument=candidate.instrument, direction=candidate.direction,
        status=LiveExecutionStatus.RECEIVED.value,
        entry_price=candidate.entry_price, stop_loss=candidate.stop_loss,
        take_profit_1=candidate.take_profit_1, take_profit_2=candidate.take_profit_2, take_profit_3=candidate.take_profit_3,
    )
    session.add(execution)
    try:
        await session.commit()
    except IntegrityError:
        # A concurrent worker won the race and inserted the identical
        # idempotency_key first (Phase 11) -- this IS the real safety
        # mechanism, not a fallback. Roll back our own attempt and defer
        # to whichever row actually landed.
        await session.rollback()
        winner = await get_existing_execution(session, idempotency_key)
        return winner if winner is not None else execution

    execution.status = LiveExecutionStatus.PARSED.value
    await session.commit()

    gate_report = await check_all_live_execution_gates(
        session, candidate, usdt_inr_rate, market_data_healthy, coindcx_account_healthy,
        daily_loss_ok, daily_loss_reason, reconciliation_ok, reconciliation_reason,
    )
    execution.gate_results = gate_report.as_dict()

    if not gate_report.approved:
        execution.status = LiveExecutionStatus.REJECTED.value
        execution.rejection_reason = gate_report.first_failure_reason()
        await session.commit()
        return execution

    execution.status = LiveExecutionStatus.VALIDATED.value
    execution.status = LiveExecutionStatus.RISK_APPROVED.value
    await session.commit()

    execution.status = LiveExecutionStatus.EXECUTION_ATTEMPTED.value
    await session.commit()

    try:
        # Unreachable in practice: gate_report.approved is never True
        # today (see gates.py's ORDER_CONTRACT_VERIFIED). Kept as real
        # code, not a comment, so the full intended pipeline shape is
        # visible and testable end-to-end once the contract is verified.
        submit_futures_order(
            instrument=candidate.instrument or candidate.symbol,
            side="buy" if candidate.direction == "LONG" else "sell",
            quantity=0, leverage=10,
            stop_loss=candidate.stop_loss, take_profit=candidate.take_profit_1,
            client_order_id=idempotency_key,
        )
    except OrderContractNotVerifiedError as e:
        execution.status = LiveExecutionStatus.REJECTED.value
        execution.rejection_reason = str(e)
        await session.commit()
        return execution

    return execution
