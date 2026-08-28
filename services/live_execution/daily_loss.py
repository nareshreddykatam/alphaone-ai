"""Daily loss limit for real-money execution (Phase 15). Audited: the
existing RiskConfig (services/risk_engine/engine.py) already has
`max_daily_loss_pct` (default 2.0%), applied there against a notional
research equity curve for backtesting. This module reuses that SAME
value and methodology, applied against the REAL CoinDCX account's real
current equity (read-only, already-verified
CoinDCXReadOnlyAccountProvider.get_balance()) instead of a notional
figure -- not a newly-invented number, and not left unguarded, per this
task's explicit instruction to preserve an existing limit rather than
invent an aggressive one.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import Trade, TradeStatus
from services.exchange.base import ExchangeAccountProvider
from services.risk_engine.engine import RiskConfig

DEFAULT_MAX_DAILY_LOSS_PCT = RiskConfig().max_daily_loss_pct  # 2.0 -- reused, not reinvented


@dataclass
class DailyLossCheck:
    approved: bool
    reason: str
    realized_pnl_inr_today: float = 0.0
    account_equity_inr: Optional[float] = None
    loss_limit_inr: Optional[float] = None


async def check_daily_loss_limit(
    session: AsyncSession, provider: ExchangeAccountProvider, now: Optional[datetime] = None,
    max_daily_loss_pct: float = DEFAULT_MAX_DAILY_LOSS_PCT,
) -> DailyLossCheck:
    now = now or datetime.utcnow()
    day_start = datetime(now.year, now.month, now.day)
    day_end = day_start + timedelta(days=1)

    balance = await provider.get_balance()
    equity = balance.get("total_equity")
    if equity is None:
        return DailyLossCheck(approved=False, reason="Real CoinDCX account equity is unavailable -- cannot evaluate the daily loss limit safely.")

    result = await session.execute(
        select(Trade).where(
            Trade.mode == "live", Trade.status == TradeStatus.CLOSED.value,
            Trade.exit_time >= day_start, Trade.exit_time < day_end,
        )
    )
    closed_today = result.scalars().all()
    realized_pnl = sum((t.pnl or 0.0) for t in closed_today)

    loss_limit = -abs(equity * (max_daily_loss_pct / 100))
    if realized_pnl <= loss_limit:
        return DailyLossCheck(
            approved=False,
            reason=f"Today's realized live PnL {realized_pnl:.2f} INR has reached the daily loss limit "
                   f"({max_daily_loss_pct}% of {equity:.2f} INR equity = {loss_limit:.2f} INR).",
            realized_pnl_inr_today=realized_pnl, account_equity_inr=equity, loss_limit_inr=loss_limit,
        )
    return DailyLossCheck(
        approved=True, reason="OK", realized_pnl_inr_today=realized_pnl, account_equity_inr=equity, loss_limit_inr=loss_limit,
    )
