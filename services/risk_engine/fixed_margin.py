"""Fixed-margin risk rules for the Multi-Coin AI Futures System (Phases
17-19): ₹200 margin, EXACTLY 10x leverage, a 10-trade/day TARGET (never a
forced minimum), and a 15-trade/day HARD MAXIMUM. Separate from the
existing percent-of-equity RiskEngine (services/risk_engine/engine.py,
still used by the original single-BTC-strategy paper trader) because this
system's constraint is a FIXED rupee amount per trade, not a percentage of
a growing/shrinking equity curve -- the two are genuinely different sizing
philosophies and conflating them would silently change one or the other's
documented behavior.

Leverage is a hardcoded constant, never a parameter accepted from a
signal, model, or strategy -- "the AI cannot choose leverage" is enforced
by this module simply never exposing a way to pass one in.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import Trade

FIXED_MARGIN_INR = 200.0
FIXED_LEVERAGE = 10  # EXACTLY 10x -- never dynamic, never overridden by any caller
DAILY_TRADE_TARGET = 10  # a target, not a forced minimum -- see check_daily_trade_budget
DAILY_TRADE_MAX = 15  # hard ceiling -- new ENTRIES only; managing/closing existing positions is always allowed

# Trade.source values that count against the shared daily budget --
# Phase 27: external Telegram signals follow the SAME constraints as
# AI/strategy paper trades, one shared pool, not a separate allowance.
FIXED_MARGIN_SOURCES = ("AI_PAPER", "TELEGRAM_EXTERNAL")


@dataclass
class FixedMarginSizing:
    approved: bool
    reason: str
    margin_inr: float = FIXED_MARGIN_INR
    leverage: int = FIXED_LEVERAGE
    notional_usdt: float = 0.0
    quantity: float = 0.0


def size_fixed_margin_trade(entry_price_usdt: float, usdt_inr_rate: Optional[float]) -> FixedMarginSizing:
    """Converts the fixed ₹200 margin into a USDT quantity at EXACTLY 10x
    leverage, using the current live USDT/INR rate. If no real rate is
    available, the trade is BLOCKED rather than sized off a guessed or
    stale rate -- "If 10x cannot be confirmed: BLOCK THE TRADE" (Phase 17)
    applies just as much to the margin conversion as to the leverage
    figure itself, since a trade sized off a fabricated rate would not
    actually be a real ₹200/10x position."""
    if usdt_inr_rate is None or usdt_inr_rate <= 0:
        return FixedMarginSizing(approved=False, reason="No live USDT/INR conversion rate available -- cannot size a real Rs.200 margin trade.")
    if entry_price_usdt is None or entry_price_usdt <= 0:
        return FixedMarginSizing(approved=False, reason="Invalid entry price.")

    margin_usdt = FIXED_MARGIN_INR / usdt_inr_rate
    notional_usdt = margin_usdt * FIXED_LEVERAGE
    quantity = notional_usdt / entry_price_usdt
    return FixedMarginSizing(approved=True, reason="OK", notional_usdt=round(notional_usdt, 2), quantity=quantity)


@dataclass
class DailyTradeBudget:
    trades_today: int
    target: int = DAILY_TRADE_TARGET
    max_allowed: int = DAILY_TRADE_MAX

    @property
    def can_open_new_entry(self) -> bool:
        return self.trades_today < self.max_allowed

    @property
    def target_reached(self) -> bool:
        return self.trades_today >= self.target


async def get_daily_trade_budget(session: AsyncSession, now: Optional[datetime] = None) -> DailyTradeBudget:
    """Counts today's (UTC calendar day) NEW entries across every fixed-
    margin source (AI_PAPER + TELEGRAM_EXTERNAL, Phase 27's shared pool) --
    a real DB count, not an in-memory counter, so it is correct across
    process restarts and shared consistently between the AI paper-trading
    job and the Telegram-signal pipeline. Closing/managing an existing
    position never increments this (Phase 18: "Closing a position does
    not reset the counter" -- and, symmetrically, does not decrement or
    otherwise affect the ENTRY count either)."""
    now = now or datetime.utcnow()
    day_start = datetime(now.year, now.month, now.day)
    day_end = day_start + timedelta(days=1)

    result = await session.execute(
        select(func.count(Trade.id)).where(
            Trade.mode == "paper", Trade.source.in_(FIXED_MARGIN_SOURCES),
            Trade.entry_time >= day_start, Trade.entry_time < day_end,
        )
    )
    count = result.scalar_one()
    return DailyTradeBudget(trades_today=count)


@dataclass
class FixedMarginTradeCheck:
    approved: bool
    reason: str
    sizing: Optional[FixedMarginSizing] = None
    budget: Optional[DailyTradeBudget] = None


async def check_fixed_margin_trade(
    session: AsyncSession, entry_price_usdt: float, usdt_inr_rate: Optional[float], now: Optional[datetime] = None,
) -> FixedMarginTradeCheck:
    """The single gate every fixed-margin paper trade (AI-sourced or
    Telegram-sourced) must pass before opening. Risk controls always
    override the 10-trade target (Phase 19): this function only ever
    BLOCKS on the 15-trade hard max, never on the 10-trade target, which
    is purely informational (DailyTradeBudget.target_reached)."""
    budget = await get_daily_trade_budget(session, now)
    if not budget.can_open_new_entry:
        return FixedMarginTradeCheck(
            approved=False, reason=f"Daily trade maximum reached ({budget.trades_today}/{DAILY_TRADE_MAX}).", budget=budget,
        )

    sizing = size_fixed_margin_trade(entry_price_usdt, usdt_inr_rate)
    if not sizing.approved:
        return FixedMarginTradeCheck(approved=False, reason=sizing.reason, sizing=sizing, budget=budget)

    return FixedMarginTradeCheck(approved=True, reason="OK", sizing=sizing, budget=budget)
