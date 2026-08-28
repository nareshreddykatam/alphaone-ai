"""The multi-gate safety check every live-execution candidate must clear
(Live Futures Auto-Trading V1, Phase 12). Every gate is evaluated and
recorded even after an earlier one fails -- Phase 25 wants a full audit
snapshot per candidate, not a short-circuited partial one. `approved` is
True only if every single gate passes.

Reuses existing, already-reviewed components wherever one exists (the
fixed-margin risk engine for margin/leverage/daily-budget, the emergency-
stop flag, the real CoinDCX/market-data health checks) rather than
reimplementing any of them.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.config import get_settings
from database.schema.models import LiveExecution, LiveExecutionStatus
from services.live_execution.kill_switch import is_emergency_stop_active
from services.risk_engine.fixed_margin import check_fixed_margin_trade, DAILY_TRADE_MAX

# Shared with apps/api/routers/live_execution_status.py so the status
# endpoint's "automatic_trading" field can never drift from what the
# actual gate check enforces -- one source of truth for whether real
# order submission is even structurally possible today (see
# ORDER_CONTRACT_VERIFIED's own explanation below).
ORDER_CONTRACT_VERIFIED = False

# A signal older than this is not "fresh" -- deliberately conservative
# for a 4h-primary-timeframe system; well within a candle's own period.
MAX_SIGNAL_AGE_SECONDS = 300


@dataclass
class LiveExecutionCandidate:
    source: str  # LiveExecutionSource value
    symbol: str
    direction: str  # LONG / SHORT
    entry_price: float
    stop_loss: Optional[float]
    take_profit_1: Optional[float]
    take_profit_2: Optional[float] = None
    take_profit_3: Optional[float] = None
    signal_timestamp: Optional[datetime] = None
    signal_id: Optional[str] = None
    instrument: Optional[str] = None
    # The caller (not this module) must have already run the real
    # scanner/eligibility check (services/scanner/multi_coin.py) -- this
    # module only records whatever the caller determined, it never
    # re-derives instrument eligibility itself.
    instrument_eligible: bool = False
    instrument_eligibility_reason: str = "Not checked"


@dataclass
class GateResult:
    name: str
    passed: bool
    reason: str


@dataclass
class GateReport:
    approved: bool
    results: list

    def as_dict(self) -> dict:
        return {r.name: {"passed": r.passed, "reason": r.reason} for r in self.results}

    def first_failure_reason(self) -> Optional[str]:
        for r in self.results:
            if not r.passed:
                return f"{r.name}: {r.reason}"
        return None


def validate_sl_tp_structure(direction: str, entry_price: float, stop_loss: Optional[float], take_profit_1: Optional[float]) -> tuple[bool, str]:
    if stop_loss is None:
        return False, "No stop-loss provided -- a live position may never open without one."
    if take_profit_1 is None:
        return False, "No take-profit provided."
    if direction == "LONG":
        if not (stop_loss < entry_price < take_profit_1):
            return False, f"LONG SL/TP geometry invalid: SL={stop_loss} Entry={entry_price} TP1={take_profit_1}"
    elif direction == "SHORT":
        if not (stop_loss > entry_price > take_profit_1):
            return False, f"SHORT SL/TP geometry invalid: SL={stop_loss} Entry={entry_price} TP1={take_profit_1}"
    else:
        return False, f"Unknown direction: {direction!r} (must be LONG or SHORT)"
    return True, "OK"


async def count_open_live_positions(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count(LiveExecution.id)).where(
            LiveExecution.status.in_([LiveExecutionStatus.POSITION_OPEN.value, LiveExecutionStatus.PARTIAL_EXIT.value])
        )
    )
    return result.scalar_one()


async def check_all_live_execution_gates(
    session: AsyncSession,
    candidate: LiveExecutionCandidate,
    usdt_inr_rate: Optional[float],
    market_data_healthy: bool,
    coindcx_account_healthy: bool,
    daily_loss_ok: bool,
    daily_loss_reason: str,
    now: Optional[datetime] = None,
) -> GateReport:
    """`daily_loss_ok`/`daily_loss_reason` are REQUIRED, not defaulted --
    a caller that forgets to run services/live_execution/daily_loss.py's
    check must be forced to pass an explicit value rather than silently
    failing open (Phase 15's hard safety gate must never be skippable by
    omission)."""
    settings = get_settings()
    results: list[GateResult] = []
    now = now or datetime.utcnow()

    results.append(GateResult("AUTOMATIC_TRADING_ENABLED", settings.automatic_trading_enabled, "settings.automatic_trading_enabled"))
    results.append(GateResult("LIVE_EXECUTION_ARMED", settings.live_execution_armed, "settings.live_execution_armed"))

    emergency_active = await is_emergency_stop_active(session)
    results.append(GateResult(
        "EMERGENCY_STOP_CLEAR", not emergency_active,
        "emergency stop is active -- no new entries" if emergency_active else "OK",
    ))

    results.append(GateResult(
        "MARKET_DATA_HEALTHY", market_data_healthy,
        "OK" if market_data_healthy else "CoinDCX live market data is not LIVE",
    ))
    results.append(GateResult(
        "COINDCX_ACCOUNT_HEALTHY", coindcx_account_healthy,
        "OK" if coindcx_account_healthy else "CoinDCX account status is not OK",
    ))
    results.append(GateResult("INSTRUMENT_ELIGIBLE", candidate.instrument_eligible, candidate.instrument_eligibility_reason))

    signal_fresh, fresh_reason = False, "No signal timestamp provided"
    if candidate.signal_timestamp is not None:
        age = (now - candidate.signal_timestamp).total_seconds()
        signal_fresh = 0 <= age <= MAX_SIGNAL_AGE_SECONDS
        fresh_reason = "OK" if signal_fresh else f"Signal is {age:.0f}s old (max {MAX_SIGNAL_AGE_SECONDS}s)"
    results.append(GateResult("SIGNAL_FRESH", signal_fresh, fresh_reason))

    sl_ok, sl_reason = validate_sl_tp_structure(candidate.direction, candidate.entry_price, candidate.stop_loss, candidate.take_profit_1)
    results.append(GateResult("VALID_SL", candidate.stop_loss is not None and sl_ok, sl_reason))
    results.append(GateResult("VALID_TP", candidate.take_profit_1 is not None and sl_ok, sl_reason))

    open_positions = await count_open_live_positions(session)
    position_ok = open_positions < settings.max_open_positions_live
    results.append(GateResult(
        "POSITION_LIMIT_OK", position_ok,
        "OK" if position_ok else f"{open_positions}/{settings.max_open_positions_live} live positions already open",
    ))

    results.append(GateResult("DAILY_LOSS_LIMIT_OK", daily_loss_ok, daily_loss_reason))

    risk_check = await check_fixed_margin_trade(session, candidate.entry_price, usdt_inr_rate, now=now)
    results.append(GateResult("RISK_ENGINE_APPROVED", risk_check.approved, risk_check.reason))
    daily_ok = risk_check.budget is not None and risk_check.budget.can_open_new_entry
    results.append(GateResult(
        "DAILY_LIMIT_NOT_EXCEEDED", daily_ok,
        "OK" if daily_ok else f"{risk_check.budget.trades_today}/{DAILY_TRADE_MAX} daily entries already used" if risk_check.budget else "budget unavailable",
    ))

    # The exchange order-creation contract (the exact request body for
    # POST /exchange/v1/derivatives/futures/orders/create, and per-
    # instrument quantity step/min-notional/price precision) has never
    # been verified against a real account -- docs/coindcx_api_findings.md
    # documents the endpoint's PATH (so AlphaOne can deliberately avoid
    # ever calling it) but not its parameters. Per this task's own explicit
    # instruction ("if something cannot be safely determined, leave live
    # execution disabled and document exactly what remains unknown"), this
    # gate can NEVER pass today, regardless of every other gate -- see
    # services/live_execution/executor.py and
    # reports/LIVE_EXECUTION_V1_REPORT.txt.
    results.append(GateResult(
        "ORDER_CONTRACT_VERIFIED", ORDER_CONTRACT_VERIFIED,
        "CoinDCX's real futures order-creation request contract (parameters, quantity precision, leverage-setting "
        "mechanism, native SL/TP attachment) has not been verified against a live account -- see "
        "docs/coindcx_api_findings.md and reports/LIVE_EXECUTION_V1_REPORT.txt.",
    ))

    approved = all(r.passed for r in results)
    return GateReport(approved=approved, results=results)
