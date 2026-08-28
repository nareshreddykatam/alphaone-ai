"""Connects a VALID ExternalSignal to an actual (paper) position -- the
one and only bridge point where a parsed/validated Telegram message can
become a trade. Every gate here can say no; none of them can be skipped.

Signal -> Validation -> Risk Engine -> Paper Execution.

There is deliberately no path from here to a real CoinDCX order call --
see tests/unit/test_no_order_placement_capability.py's coverage of this
module.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import ExternalSignal, TradeSource
from services.paper_trader.engine import PaperPosition
from services.paper_trader.persistence import get_open_paper_trade, persist_paper_open
from services.risk_engine.fixed_margin import check_fixed_margin_trade
from services.telegram_signals.live_state import multi_coin_paper_trader


async def execute_valid_signal(
    session: AsyncSession, signal: ExternalSignal, usdt_inr_rate: Optional[float],
) -> tuple[Optional[PaperPosition], str]:
    """Returns (position_or_None, reason). Only ever called for a VALID
    ExternalSignal -- any other status was already rejected upstream in
    the parser/ingestion pipeline and never reaches here."""
    if signal.status != "VALID":
        return None, f"Signal status is {signal.status}, not VALID -- never executed."

    existing = await get_open_paper_trade(session, signal.symbol, source=TradeSource.TELEGRAM_EXTERNAL.value)
    if existing is not None:
        return None, f"An open TELEGRAM_EXTERNAL position already exists on {signal.symbol} ({existing.trade_id})."

    check = await check_fixed_margin_trade(session, signal.entry_price, usdt_inr_rate)
    if not check.approved:
        return None, check.reason

    trade_id = f"TG-{signal.id}"
    position = PaperPosition(
        trade_id=trade_id, signal_id=str(signal.id), side=signal.direction,
        entry_price=signal.entry_price, entry_time=signal.created_at or datetime.utcnow(),
        stop_loss=signal.stop_loss, take_profit_1=signal.take_profit_1,
        take_profit_2=signal.take_profit_2, take_profit_3=signal.take_profit_3,
        quantity=check.sizing.quantity, leverage=check.sizing.leverage, market_regime="UNKNOWN",
        strategy_name=f"TELEGRAM:{signal.source_channel}",
    )

    multi_coin_paper_trader.positions[trade_id] = position
    multi_coin_paper_trader.risk_engine.record_position_open()

    await persist_paper_open(session, position, symbol=signal.symbol, source=TradeSource.TELEGRAM_EXTERNAL.value)
    signal.trade_id = trade_id
    await session.commit()

    return position, "OK"
