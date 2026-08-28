"""Durable persistence for paper-trading decisions (AI Trading V1, Phase 11).

The in-memory PaperTrader (services/paper_trader/engine.py) owns the live
open/SL/TP decision loop for one scheduler process's lifetime (its equity
curve and risk-engine state reset on a process restart -- the same
documented limitation RiskEngine's own notional equity tracker already
carries, see docs/known_limitations.md). This module mirrors every one of
its events into the Trade/TradeExecution tables -- the SAME tables and
`mode="paper"` convention the existing manual/CoinDCX-synced trade journal
already uses (database/schema/models.py), not a new, separate paper-trade
table -- so paper-trade HISTORY survives a restart and is queryable by the
API/frontend even though the running equity number does not.
"""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import (
    Trade, TradeExecution, ExecutionType, TradeStatus, TradeSource, DataSourceKind,
)
from services.paper_trader.engine import PaperPosition


async def get_open_paper_trade(session: AsyncSession, symbol: str = "BTC/USDT") -> Optional[Trade]:
    result = await session.execute(
        select(Trade).where(
            Trade.symbol == symbol, Trade.mode == "paper", Trade.status.in_(
                [TradeStatus.OPEN.value, TradeStatus.PARTIALLY_CLOSED.value]
            ),
        ).order_by(Trade.entry_time.desc())
    )
    return result.scalars().first()


async def persist_paper_open(session: AsyncSession, position: PaperPosition, symbol: str = "BTC/USDT") -> Trade:
    trade = Trade(
        trade_id=position.trade_id,
        signal_id=position.signal_id,
        symbol=symbol,
        side=position.side,
        status=TradeStatus.OPEN.value,
        mode="paper",
        entry_price=position.entry_price,
        stop_loss=position.stop_loss,
        take_profit_1=position.take_profit_1,
        take_profit_2=position.take_profit_2,
        take_profit_3=position.take_profit_3,
        quantity=position.quantity,
        leverage=position.leverage,
        entry_time=position.entry_time,
        market_regime=position.market_regime,
        is_manual_entry=False,
        source=TradeSource.AI_PAPER.value,
        data_source=DataSourceKind.SYNCED.value,
    )
    session.add(trade)
    session.add(TradeExecution(
        trade_id=position.trade_id, execution_type=ExecutionType.ENTRY.value,
        price=position.entry_price, quantity=position.quantity, timestamp=position.entry_time,
        note=f"AI paper entry -- strategy_evidence={position.strategy_name or 'n/a'}",
    ))
    await session.commit()
    return trade


async def persist_paper_event(session: AsyncSession, event: dict) -> None:
    """Mirrors one PaperTrader.process_candle() event (partial_exit or
    exit) into TradeExecution + the parent Trade's status/exit fields.
    Never re-derives PnL -- uses the exact numbers PaperTrader already
    computed, so the DB record and the live decision loop can never
    disagree about a trade's outcome."""
    result = await session.execute(select(Trade).where(Trade.trade_id == event["trade_id"]))
    trade = result.scalar_one_or_none()
    if trade is None:
        return  # trade row missing (e.g. process restarted mid-position) -- nothing to update

    if event["event_type"] == "partial_exit":
        session.add(TradeExecution(
            trade_id=event["trade_id"], execution_type=ExecutionType.PARTIAL_EXIT.value,
            price=event["exit_price"], quantity=event["quantity"], timestamp=event["exit_time"],
            note=f"TP{event['target_index']} partial exit, pnl={event['pnl']}",
        ))
        trade.status = TradeStatus.PARTIALLY_CLOSED.value
    else:  # "exit" -- full close (stop_loss or the final remaining target)
        session.add(TradeExecution(
            trade_id=event["trade_id"], execution_type=ExecutionType.EXIT.value,
            price=event["exit_price"], quantity=event["quantity"], timestamp=event["exit_time"],
            note=f"Closed: {event['exit_reason']}",
        ))
        trade.status = TradeStatus.CLOSED.value
        trade.exit_price = event["exit_price"]
        trade.exit_time = event["exit_time"]
        trade.pnl = event["pnl"]
        trade.pnl_pct = event["pnl_pct"]
        trade.fees = event["fees"]
        trade.r_multiple = event["r_multiple"]
        trade.exit_reason = event["exit_reason"]

    await session.commit()
