"""Manual trade tracking (Phase 4, sections 6 & 18): the user executes every
trade by hand on CoinDCX and reports it here. This module never talks to
an exchange and never infers a fill -- every price/quantity/timestamp is
exactly what the caller (API layer, eventually a form) supplies. Supports
partial exits via TradeExecution rows so a position can be scaled out of
over time, matching the DB schema's OPEN/PARTIALLY_CLOSED/CLOSED/CANCELLED
lifecycle.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import Trade, TradeExecution, TradeStatus, ExecutionType
from services.trade_journal.pnl import compute_slice_pnl, compute_r_multiple


class TradeJournalError(Exception):
    pass


def _new_trade_id() -> str:
    return f"MANUAL-{uuid.uuid4().hex[:10].upper()}"


async def _closed_quantity(session: AsyncSession, trade_id: str) -> float:
    result = await session.execute(
        select(TradeExecution).where(
            TradeExecution.trade_id == trade_id,
            TradeExecution.execution_type.in_([ExecutionType.PARTIAL_EXIT.value, ExecutionType.EXIT.value]),
        )
    )
    return sum(e.quantity for e in result.scalars().all())


async def open_trade(
    session: AsyncSession,
    *,
    symbol: str,
    side: str,
    entry_price: float,
    quantity: float,
    entry_time: datetime,
    stop_loss: Optional[float] = None,
    take_profit_1: Optional[float] = None,
    take_profit_2: Optional[float] = None,
    take_profit_3: Optional[float] = None,
    leverage: int = 1,
    signal_id: Optional[str] = None,
    account_id: Optional[uuid.UUID] = None,
    matched_signal_confidence: Optional[float] = None,
) -> Trade:
    if side not in ("LONG", "SHORT"):
        raise TradeJournalError(f"side must be LONG or SHORT, got {side!r}")
    if entry_price <= 0 or quantity <= 0:
        raise TradeJournalError("entry_price and quantity must be positive")

    trade = Trade(
        trade_id=_new_trade_id(),
        signal_id=signal_id,
        symbol=symbol,
        side=side,
        status=TradeStatus.OPEN.value,
        mode="live",
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit_1=take_profit_1,
        take_profit_2=take_profit_2,
        take_profit_3=take_profit_3,
        quantity=quantity,
        leverage=leverage,
        entry_time=entry_time,
        is_manual_entry=True,
        source="MANUAL",
        matched_signal_confidence=matched_signal_confidence,
        account_id=account_id,
    )
    session.add(trade)
    await session.flush()

    session.add(
        TradeExecution(
            trade_id=trade.trade_id,
            execution_type=ExecutionType.ENTRY.value,
            price=entry_price,
            quantity=quantity,
            timestamp=entry_time,
        )
    )
    await session.commit()
    await session.refresh(trade)
    return trade


async def record_exit(
    session: AsyncSession,
    *,
    trade_id: str,
    exit_price: float,
    quantity: float,
    timestamp: datetime,
    reason: Optional[str] = None,
    note: Optional[str] = None,
) -> Trade:
    """Record a full or partial exit. `quantity` is how much of the position
    is being closed now, never more than what's still open."""
    result = await session.execute(select(Trade).where(Trade.trade_id == trade_id))
    trade = result.scalar_one_or_none()
    if trade is None:
        raise TradeJournalError(f"no trade with trade_id={trade_id!r}")
    if trade.status in (TradeStatus.CLOSED.value, TradeStatus.CANCELLED.value):
        raise TradeJournalError(f"trade {trade_id} is already {trade.status}, cannot record another exit")
    if exit_price <= 0 or quantity <= 0:
        raise TradeJournalError("exit_price and quantity must be positive")

    already_closed = await _closed_quantity(session, trade_id)
    remaining = trade.quantity - already_closed
    if quantity > remaining + 1e-9:
        raise TradeJournalError(
            f"cannot exit {quantity} of trade {trade_id}, only {remaining} remains open"
        )

    is_full_close = quantity >= remaining - 1e-9
    execution_type = ExecutionType.EXIT.value if is_full_close else ExecutionType.PARTIAL_EXIT.value

    slice_pnl = compute_slice_pnl(trade.side, trade.entry_price, exit_price, quantity, trade.leverage)

    session.add(
        TradeExecution(
            trade_id=trade_id,
            execution_type=execution_type,
            price=exit_price,
            quantity=quantity,
            timestamp=timestamp,
            note=note,
        )
    )

    total_closed = already_closed + quantity
    prior_exit_notional = (trade.exit_price or 0) * already_closed
    trade.exit_price = (prior_exit_notional + exit_price * quantity) / total_closed

    trade.pnl = (trade.pnl or 0) + slice_pnl.pnl
    trade.fees = (trade.fees or 0) + slice_pnl.fees
    trade.pnl_pct = (trade.pnl / (trade.entry_price * trade.quantity)) * 100 if trade.entry_price and trade.quantity else 0.0

    if is_full_close:
        trade.status = TradeStatus.CLOSED.value
        trade.exit_time = timestamp
        trade.exit_reason = reason
        trade.r_multiple = compute_r_multiple(trade.entry_price, trade.stop_loss, trade.quantity, trade.pnl) if trade.stop_loss else 0.0
    else:
        trade.status = TradeStatus.PARTIALLY_CLOSED.value

    await session.commit()
    await session.refresh(trade)
    return trade


async def cancel_trade(session: AsyncSession, *, trade_id: str, reason: Optional[str] = None) -> Trade:
    """Cancel a trade that was logged but never actually had an exit -- e.g.
    a data-entry mistake. Not allowed once any exit has been recorded, since
    that would silently discard real P&L history."""
    result = await session.execute(select(Trade).where(Trade.trade_id == trade_id))
    trade = result.scalar_one_or_none()
    if trade is None:
        raise TradeJournalError(f"no trade with trade_id={trade_id!r}")
    if trade.status != TradeStatus.OPEN.value:
        raise TradeJournalError(f"trade {trade_id} is {trade.status}, only an OPEN trade with no exits can be cancelled")

    trade.status = TradeStatus.CANCELLED.value
    trade.exit_reason = reason or "cancelled"
    trade.exit_time = datetime.utcnow()
    await session.commit()
    await session.refresh(trade)
    return trade


async def get_open_trades(session: AsyncSession, account_id: Optional[uuid.UUID] = None) -> list[Trade]:
    query = select(Trade).where(Trade.status.in_([TradeStatus.OPEN.value, TradeStatus.PARTIALLY_CLOSED.value]))
    if account_id is not None:
        query = query.where(Trade.account_id == account_id)
    result = await session.execute(query)
    return list(result.scalars().all())


async def set_signal_match(
    session: AsyncSession, *, trade_id: str, signal_id: str, confidence: Optional[float] = None
) -> Trade:
    """Manually confirm (or override) which signal a trade should be linked
    to -- used for the ambiguous-match confirmation flow (Phase 4H)."""
    result = await session.execute(select(Trade).where(Trade.trade_id == trade_id))
    trade = result.scalar_one_or_none()
    if trade is None:
        raise TradeJournalError(f"no trade with trade_id={trade_id!r}")
    trade.signal_id = signal_id
    trade.matched_signal_confidence = confidence
    await session.commit()
    await session.refresh(trade)
    return trade


async def get_trade_executions(session: AsyncSession, trade_id: str) -> list[TradeExecution]:
    result = await session.execute(
        select(TradeExecution).where(TradeExecution.trade_id == trade_id).order_by(TradeExecution.timestamp)
    )
    return list(result.scalars().all())
