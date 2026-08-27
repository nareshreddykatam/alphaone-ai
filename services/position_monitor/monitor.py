"""Position monitoring + exit alerts (Phase 4I). Addresses a real gap
carried since the Phase 1 audit: SignalEngine never produced an EXIT
signal at all. This checks the user's open manually-tracked positions
against the latest known price and recommends an exit when the position's
own stop-loss or take-profit would have been hit -- it NEVER closes the
trade itself. The user must still execute the exit on CoinDCX and then
record it via POST /journal/{trade_id}/exit; this is a recommendation
only, consistent with the platform's absolute no-auto-trading rule.

Alerts are deduplicated via NotificationLog so the same breach isn't
re-alerted on every check -- keyed by trade_id+reason, not by time, since
the breach condition doesn't change until the trade is closed or its
levels change.
"""
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import NotificationLog
from services.portfolio.account import get_or_create_default_account
from services.trade_journal.journal import get_open_trades
from services.trade_journal.pnl import compute_slice_pnl

STOP_LOSS_HIT = "stop_loss_hit"
TAKE_PROFIT_HIT = "take_profit_hit"


@dataclass
class ExitAlert:
    trade_id: str
    symbol: str
    side: str
    reason: str
    trigger_price: float
    current_price: float
    entry_price: float
    pnl: float  # unrealized PnL if exited at current_price right now -- a preview, not a real close


def _make_alert(trade, reason: str, trigger_price: float, current_price: float) -> ExitAlert:
    pnl = compute_slice_pnl(trade.side, trade.entry_price, current_price, trade.quantity, trade.leverage).pnl
    return ExitAlert(
        trade_id=trade.trade_id, symbol=trade.symbol, side=trade.side, reason=reason,
        trigger_price=trigger_price, current_price=current_price, entry_price=trade.entry_price, pnl=pnl,
    )


def _check_trade(trade, current_price: float) -> Optional[ExitAlert]:
    if trade.side == "LONG":
        if trade.stop_loss is not None and current_price <= trade.stop_loss:
            return _make_alert(trade, STOP_LOSS_HIT, trade.stop_loss, current_price)
        if trade.take_profit_1 is not None and current_price >= trade.take_profit_1:
            return _make_alert(trade, TAKE_PROFIT_HIT, trade.take_profit_1, current_price)
    else:  # SHORT
        if trade.stop_loss is not None and current_price >= trade.stop_loss:
            return _make_alert(trade, STOP_LOSS_HIT, trade.stop_loss, current_price)
        if trade.take_profit_1 is not None and current_price <= trade.take_profit_1:
            return _make_alert(trade, TAKE_PROFIT_HIT, trade.take_profit_1, current_price)
    return None


def _dedup_key(alert: ExitAlert) -> str:
    return f"exit_alert:{alert.trade_id}:{alert.reason}"


async def _already_alerted(session: AsyncSession, key: str) -> bool:
    row = (await session.execute(select(NotificationLog).where(NotificationLog.message_type == key))).scalar_one_or_none()
    return row is not None


async def check_open_positions(session: AsyncSession, current_price: float, symbol: str = "BTC/USDT") -> list[ExitAlert]:
    """Read-only check, no side effects, no dedup -- returns every current
    breach among open positions regardless of whether it was already
    alerted. Use get_new_exit_alerts for the deduplicated, alert-worthy set."""
    account = await get_or_create_default_account(session)
    trades = await get_open_trades(session, account_id=account.id)
    alerts = []
    for trade in trades:
        if trade.symbol != symbol:
            continue
        alert = _check_trade(trade, current_price)
        if alert is not None:
            alerts.append(alert)
    return alerts


async def get_new_exit_alerts(session: AsyncSession, current_price: float, symbol: str = "BTC/USDT") -> list[ExitAlert]:
    """Deduplicated: only returns breaches not already recorded in
    NotificationLog, and records each newly-surfaced one so it isn't
    repeated on the next check."""
    all_alerts = await check_open_positions(session, current_price, symbol)
    new_alerts = []
    for alert in all_alerts:
        key = _dedup_key(alert)
        if not await _already_alerted(session, key):
            new_alerts.append(alert)
            session.add(NotificationLog(channel="internal", message_type=key, status="pending"))
    if new_alerts:
        await session.commit()
    return new_alerts
