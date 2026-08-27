"""CoinDCX account synchronization (Phase 5, sections 10-17, 40). Pulls
positions/wallet/trades from CoinDCXReadOnlyAccountProvider and updates
the DB idempotently. Never places, cancels, or modifies anything -- this
module only ever reads from CoinDCX and writes to AlphaOne's own DB.

New exchange-detected positions are matched to AlphaOne signals using the
SAME matcher built in Phase 4 (services/signal_matching/matcher.py) --
confident match -> AUTO_MATCHED, ambiguous -> AMBIGUOUS (surfaced via the
existing GET /journal/{trade_id}/match-candidates endpoint), no candidate
-> UNMATCHED. Never fabricates a signal relationship.

When a previously-tracked position disappears, this looks for the real
closing fill(s) via get_trade_history() and closes the Trade with the
volume-weighted average of those real prices (services.trade_journal.journal.record_exit,
reused as-is). If no real closing fill can be found, the trade is left
open and a FAILED SyncEvent is recorded -- never guessed shut.
"""
import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import (
    Trade, TradeExecution, TradeStatus, TradeSource, ExecutionType,
    DataSourceKind, SignalMatchStatus, ConnectionState, AccountSnapshot, SyncEvent, SyncStatus,
)
from services.exchange.coindcx import CoinDCXReadOnlyAccountProvider, denormalize_symbol
from services.portfolio.account import get_or_create_default_account
from services.signal_matching.matcher import find_candidate_signals, pick_confident_match
from services.trade_journal.journal import record_exit, TradeJournalError


def _new_trade_id() -> str:
    return f"COINDCX-{uuid.uuid4().hex[:10].upper()}"


def _exchange_transaction_id(fill: dict) -> str:
    """CoinDCX documents no single unique id per trade fill -- derive a
    deterministic one from the fields that are documented (see
    docs/coindcx_api_findings.md)."""
    return f"{fill.get('order_id')}:{fill.get('timestamp')}:{fill.get('price')}:{fill.get('quantity')}:{fill.get('side')}"


async def sync_balance(session: AsyncSession, provider: CoinDCXReadOnlyAccountProvider) -> dict:
    account = await get_or_create_default_account(session)
    balance = await provider.get_balance()

    if balance["status"] == "OK":
        account.connection_status = ConnectionState.LIVE.value
        account.last_synced_at = datetime.utcnow()
        session.add(AccountSnapshot(
            account_id=account.id, timestamp=datetime.utcnow(), equity=balance["total_equity"],
            available_balance=balance["available_balance"], used_margin=balance["used_margin"],
            source=DataSourceKind.LIVE.value,
        ))
        session.add(SyncEvent(source="coindcx", status=SyncStatus.SUCCESS.value, detail="balance synced"))
    elif balance["status"] == "NOT_CONFIGURED":
        account.connection_status = ConnectionState.NOT_CONFIGURED.value
    else:
        account.connection_status = ConnectionState.DISCONNECTED.value
        session.add(SyncEvent(source="coindcx", status=SyncStatus.FAILED.value, detail=str(balance)))

    await session.commit()
    return balance


async def _create_trade_from_position(session: AsyncSession, account_id, pos: dict) -> Trade:
    symbol = denormalize_symbol(pos["symbol"])
    entry_time = datetime.utcnow()

    candidates = await find_candidate_signals(
        session, symbol=symbol, side=pos["side"], entry_price=pos["entry_price"], timestamp=entry_time,
    )
    match = pick_confident_match(candidates)
    if match is not None:
        match_status, signal_id, confidence = SignalMatchStatus.AUTO_MATCHED.value, match.signal_id, match.confidence
    elif candidates:
        match_status, signal_id, confidence = SignalMatchStatus.AMBIGUOUS.value, None, None
    else:
        match_status, signal_id, confidence = SignalMatchStatus.UNMATCHED.value, None, None

    trade = Trade(
        trade_id=_new_trade_id(), signal_id=signal_id, symbol=symbol, side=pos["side"],
        status=TradeStatus.OPEN.value, mode="live", entry_price=pos["entry_price"], quantity=pos["quantity"],
        leverage=int(pos["leverage"]) if pos.get("leverage") else 1, entry_time=entry_time,
        is_manual_entry=False, source=TradeSource.COINDCX_SYNC.value, matched_signal_confidence=confidence,
        account_id=account_id, exchange_position_id=pos["exchange_position_id"],
        mark_price=pos["mark_price"], liquidation_price=pos["liquidation_price"],
        unrealized_pnl=pos["unrealized_pnl"], margin=pos["margin"],
        data_source=DataSourceKind.LIVE.value, match_status=match_status, last_synced_at=entry_time,
    )
    session.add(trade)
    await session.flush()
    session.add(TradeExecution(
        trade_id=trade.trade_id, execution_type=ExecutionType.ENTRY.value,
        price=pos["entry_price"], quantity=pos["quantity"], timestamp=entry_time,
    ))
    return trade


async def _close_disappeared_position(session: AsyncSession, provider: CoinDCXReadOnlyAccountProvider, trade: Trade) -> Optional[Trade]:
    since = trade.last_synced_at or trade.entry_time
    fills = await provider.get_trade_history(
        symbol=trade.symbol,
        from_date=since.strftime("%Y-%m-%d"),
        to_date=datetime.utcnow().strftime("%Y-%m-%d"),
    )
    closing_side = "sell" if trade.side == "LONG" else "buy"
    relevant = [f for f in fills if f.get("side") == closing_side]

    if not relevant:
        session.add(SyncEvent(
            source="coindcx", status=SyncStatus.FAILED.value,
            detail=f"position {trade.exchange_position_id} ({trade.trade_id}) disappeared but no closing fill was found",
        ))
        return None

    total_qty = sum(float(f["quantity"]) for f in relevant)
    vwap_exit = sum(float(f["price"]) * float(f["quantity"]) for f in relevant) / total_qty
    remaining = trade.quantity  # best-known remaining open quantity at sync time

    try:
        closed = await record_exit(
            session, trade_id=trade.trade_id, exit_price=vwap_exit, quantity=min(total_qty, remaining),
            timestamp=datetime.utcnow(), reason="coindcx_position_closed",
        )
    except TradeJournalError as e:
        session.add(SyncEvent(source="coindcx", status=SyncStatus.FAILED.value, detail=str(e)))
        return None
    return closed


async def sync_positions(session: AsyncSession, provider: CoinDCXReadOnlyAccountProvider) -> dict:
    """Returns {"opened": [...], "updated": [...], "closed": [...]}."""
    account = await get_or_create_default_account(session)
    live_positions = await provider.get_open_positions()
    live_by_id = {p["exchange_position_id"]: p for p in live_positions}

    result = await session.execute(
        select(Trade).where(
            Trade.account_id == account.id,
            Trade.source == TradeSource.COINDCX_SYNC.value,
            Trade.status.in_([TradeStatus.OPEN.value, TradeStatus.PARTIALLY_CLOSED.value]),
        )
    )
    existing_open = {t.exchange_position_id: t for t in result.scalars().all()}

    opened, updated, closed = [], [], []
    now = datetime.utcnow()

    for pos_id, pos in live_by_id.items():
        if pos_id in existing_open:
            trade = existing_open[pos_id]
            trade.mark_price = pos["mark_price"]
            trade.unrealized_pnl = pos["unrealized_pnl"]
            trade.liquidation_price = pos["liquidation_price"]
            trade.margin = pos["margin"]
            trade.last_synced_at = now
            updated.append(trade)
        else:
            trade = await _create_trade_from_position(session, account.id, pos)
            opened.append(trade)

    for pos_id, trade in existing_open.items():
        if pos_id not in live_by_id:
            result_trade = await _close_disappeared_position(session, provider, trade)
            if result_trade is not None:
                closed.append(result_trade)

    account.last_synced_at = now
    await session.commit()
    return {"opened": opened, "updated": updated, "closed": closed}


async def sync_trade_fills(session: AsyncSession, provider: CoinDCXReadOnlyAccountProvider, symbol: str = "BTC/USDT") -> int:
    """Idempotently ingests raw trade fills as TradeExecution audit rows
    (section 15-16) -- keyed on a deterministic exchange_transaction_id
    since CoinDCX documents no single unique fill id. Safe to call
    repeatedly; duplicates are skipped via the DB's unique index."""
    fills = await provider.get_trade_history(symbol=symbol)
    inserted = 0
    for fill in fills:
        exchange_transaction_id = _exchange_transaction_id(fill)
        existing = await session.execute(
            select(TradeExecution).where(TradeExecution.exchange_transaction_id == exchange_transaction_id)
        )
        if existing.scalar_one_or_none() is not None:
            continue

        trade_result = await session.execute(
            select(Trade).where(Trade.exchange_position_id.is_not(None), Trade.symbol == symbol)
            .order_by(Trade.entry_time.desc())
        )
        trade_row = trade_result.scalars().first()
        if trade_row is None:
            # No known Trade to attach this fill to yet (e.g. the position
            # sync hasn't run for this symbol) -- TradeExecution.trade_id is
            # a real FK, so skip rather than create a dangling row. The
            # fill will be picked up on a later sync once a Trade exists.
            continue

        session.add(TradeExecution(
            trade_id=trade_row.trade_id,
            execution_type=ExecutionType.ENTRY.value,
            price=float(fill.get("price", 0)), quantity=float(fill.get("quantity", 0)),
            timestamp=datetime.utcfromtimestamp(float(fill.get("timestamp", 0)) / 1000) if fill.get("timestamp") else datetime.utcnow(),
            exchange_transaction_id=exchange_transaction_id,
            note=f"raw fill sync, side={fill.get('side')}",
        ))
        inserted += 1

    if inserted:
        await session.commit()
    return inserted
