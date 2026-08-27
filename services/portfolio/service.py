"""Portfolio / P&L accounting (Phase 4, sections 6, 20, 26, 33, 34).

The single most important rule this module enforces structurally: the three
performance views below are computed from three different tables and are
NEVER combined into one number.

- Backtest performance      <- BacktestRun / BacktestMetric   (Phase 2/3 historical research)
- AlphaOne signal performance <- SignalOutcome                (what following every signal, hypothetically, would have earned)
- User actual performance   <- Trade                          (what the user actually earned manually trading)

Deposits/withdrawals are tracked in their own tables and are excluded from
every P&L and equity-curve calculation -- they are cash movements, not
trading performance.
"""
from collections import defaultdict
from datetime import datetime
from typing import Optional
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import (
    Trade,
    TradeStatus,
    SignalOutcome,
    SignalOutcomeType,
    BacktestRun,
    BacktestMetric,
    Deposit,
    Withdrawal,
    AccountSnapshot,
)

_CLOSED_STATUSES = (TradeStatus.CLOSED.value, TradeStatus.PARTIALLY_CLOSED.value)


def period_key(dt: datetime, period: str) -> str:
    if period == "daily":
        return dt.strftime("%Y-%m-%d")
    if period == "weekly":
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if period == "monthly":
        return dt.strftime("%Y-%m")
    raise ValueError(f"unknown period {period!r}, expected daily/weekly/monthly")


async def get_user_actual_performance(session: AsyncSession, account_id: Optional[uuid.UUID] = None) -> dict:
    """What the user actually earned executing trades manually. Never
    includes hypothetical/backtest numbers."""
    query = select(Trade).where(Trade.status.in_(_CLOSED_STATUSES))
    if account_id is not None:
        query = query.where(Trade.account_id == account_id)
    trades = list((await session.execute(query)).scalars().all())

    total_trades = len(trades)
    if total_trades == 0:
        return {
            "total_trades": 0, "total_pnl": 0.0, "total_fees": 0.0, "total_funding": 0.0,
            "win_rate": None, "winning_trades": 0, "losing_trades": 0,
            "average_r": None, "profit_factor": None,
        }

    total_pnl = sum(t.pnl or 0 for t in trades)
    total_fees = sum(t.fees or 0 for t in trades)
    total_funding = sum(t.funding or 0 for t in trades)
    wins = [t for t in trades if (t.pnl or 0) > 0]
    losses = [t for t in trades if (t.pnl or 0) < 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    r_values = [t.r_multiple for t in trades if t.r_multiple]

    return {
        "total_trades": total_trades,
        "total_pnl": total_pnl,
        "total_fees": total_fees,
        "total_funding": total_funding,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": len(wins) / total_trades,
        "average_r": (sum(r_values) / len(r_values)) if r_values else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
    }


async def get_alphaone_signal_performance(session: AsyncSession) -> dict:
    """What following every AlphaOne signal, hypothetically, would have
    earned -- entirely independent of whether the user actually took any
    of them. See get_missed_signals for the taken-vs-not split."""
    outcomes = list((await session.execute(
        select(SignalOutcome).where(SignalOutcome.outcome != SignalOutcomeType.PENDING.value)
    )).scalars().all())

    evaluated = [o for o in outcomes if o.outcome != SignalOutcomeType.EXPIRED.value]
    total_signals = len(outcomes)
    if total_signals == 0:
        return {
            "total_signals": 0, "total_hypothetical_pnl_pct": None,
            "win_rate": None, "no_trade_rate": None,
        }

    wins = [o for o in evaluated if o.outcome == SignalOutcomeType.WIN.value]
    losses = [o for o in evaluated if o.outcome == SignalOutcomeType.LOSS.value]
    no_trades = [o for o in outcomes if o.outcome == SignalOutcomeType.NO_TRADE.value]
    resolved = wins + losses
    pnl_pcts = [o.hypothetical_pnl_pct for o in resolved if o.hypothetical_pnl_pct is not None]

    return {
        "total_signals": total_signals,
        "resolved_signals": len(resolved),
        "total_hypothetical_pnl_pct": sum(pnl_pcts) if pnl_pcts else None,
        "win_rate": (len(wins) / len(resolved)) if resolved else None,
        "no_trade_rate": len(no_trades) / total_signals,
    }


async def get_missed_signals(session: AsyncSession) -> dict:
    """Section 22: separate stats for all resolved signals vs only the ones
    the user actually took. A missed trade's hypothetical PnL must never be
    counted as real P&L -- this function only ever reads SignalOutcome."""
    outcomes = list((await session.execute(
        select(SignalOutcome).where(
            SignalOutcome.outcome.in_([SignalOutcomeType.WIN.value, SignalOutcomeType.LOSS.value])
        )
    )).scalars().all())

    taken = [o for o in outcomes if o.was_taken_by_user]
    missed = [o for o in outcomes if not o.was_taken_by_user]

    def _summary(rows):
        if not rows:
            return {"count": 0, "win_rate": None, "total_hypothetical_pnl_pct": None}
        wins = sum(1 for o in rows if o.outcome == SignalOutcomeType.WIN.value)
        pcts = [o.hypothetical_pnl_pct for o in rows if o.hypothetical_pnl_pct is not None]
        return {
            "count": len(rows),
            "win_rate": wins / len(rows),
            "total_hypothetical_pnl_pct": sum(pcts) if pcts else None,
        }

    return {"all_signals": _summary(outcomes), "user_taken": _summary(taken), "missed": _summary(missed)}


async def get_backtest_performance(session: AsyncSession, run_id: Optional[uuid.UUID] = None) -> Optional[dict]:
    """Historical research result (Phase 2/3) -- read-only reference to
    BacktestRun/BacktestMetric, never blended with live numbers."""
    if run_id is not None:
        run = (await session.execute(select(BacktestRun).where(BacktestRun.id == run_id))).scalar_one_or_none()
    else:
        run = (await session.execute(select(BacktestRun).order_by(BacktestRun.created_at.desc()))).scalars().first()
    if run is None:
        return None

    metric = (await session.execute(
        select(BacktestMetric).where(BacktestMetric.run_id == run.id).order_by(BacktestMetric.created_at.desc())
    )).scalars().first()
    if metric is None:
        return None

    return {
        "run_id": str(run.id),
        "strategy_name": run.strategy_name,
        "timeframe": run.timeframe,
        "dataset_start": run.dataset_start,
        "dataset_end": run.dataset_end,
        "total_pnl_pct": metric.total_pnl_pct,
        "win_rate": metric.win_rate,
        "profit_factor": metric.profit_factor,
        "sharpe_ratio": metric.sharpe_ratio,
        "max_drawdown_pct": metric.max_drawdown_pct,
        "total_trades": metric.total_trades,
    }


async def get_equity_curve(session: AsyncSession, account_id: uuid.UUID, initial_equity: float = 0.0) -> list[dict]:
    """Trading-only equity curve: initial_equity + cumulative closed-trade
    PnL over time. Deposits/withdrawals are deliberately excluded (section
    26) so this reflects trading skill, not cash movements."""
    trades = list((await session.execute(
        select(Trade)
        .where(Trade.account_id == account_id, Trade.status.in_(_CLOSED_STATUSES))
        .order_by(Trade.exit_time)
    )).scalars().all())

    curve = []
    running = initial_equity
    for t in trades:
        running += (t.pnl or 0)
        curve.append({"timestamp": t.exit_time, "equity": running, "trade_id": t.trade_id})
    return curve


async def get_pnl_breakdown(session: AsyncSession, account_id: uuid.UUID, period: str = "daily") -> list[dict]:
    """Gross/fees/funding/net P&L bucketed by period. gross = net + fees +
    funding (fees/funding are already netted into Trade.pnl at close time --
    see services/trade_journal/pnl.py)."""
    trades = list((await session.execute(
        select(Trade)
        .where(Trade.account_id == account_id, Trade.status.in_(_CLOSED_STATUSES))
        .order_by(Trade.exit_time)
    )).scalars().all())

    buckets: dict[str, dict] = defaultdict(lambda: {"net": 0.0, "fees": 0.0, "funding": 0.0, "trades": 0})
    for t in trades:
        key = period_key(t.exit_time, period)
        buckets[key]["net"] += t.pnl or 0
        buckets[key]["fees"] += t.fees or 0
        buckets[key]["funding"] += t.funding or 0
        buckets[key]["trades"] += 1

    return [
        {
            "period": key,
            "gross": b["net"] + b["fees"] + b["funding"],
            "fees": b["fees"],
            "funding": b["funding"],
            "net": b["net"],
            "trades": b["trades"],
        }
        for key, b in sorted(buckets.items())
    ]


async def reconcile_account(session: AsyncSession, account_id: uuid.UUID, initial_equity: float = 0.0, tolerance: float = 1.0) -> dict:
    """Compares the theoretical equity (initial + deposits - withdrawals +
    cumulative trade PnL) against the user's most recently reported
    AccountSnapshot. Flags a mismatch -- never silently adjusts either
    number (section 33)."""
    trades = list((await session.execute(
        select(Trade).where(Trade.account_id == account_id, Trade.status.in_(_CLOSED_STATUSES))
    )).scalars().all())
    deposits = list((await session.execute(select(Deposit).where(Deposit.account_id == account_id))).scalars().all())
    withdrawals = list((await session.execute(select(Withdrawal).where(Withdrawal.account_id == account_id))).scalars().all())
    latest_snapshot = (await session.execute(
        select(AccountSnapshot).where(AccountSnapshot.account_id == account_id).order_by(AccountSnapshot.timestamp.desc())
    )).scalars().first()

    theoretical = (
        initial_equity
        + sum(d.amount for d in deposits)
        - sum(w.amount for w in withdrawals)
        + sum(t.pnl or 0 for t in trades)
    )

    if latest_snapshot is None:
        return {
            "status": "NO_SNAPSHOT", "theoretical_equity": theoretical,
            "reported_equity": None, "difference": None, "is_mismatched": None,
        }

    difference = latest_snapshot.equity - theoretical
    return {
        "status": "OK" if abs(difference) <= tolerance else "MISMATCH",
        "theoretical_equity": theoretical,
        "reported_equity": latest_snapshot.equity,
        "difference": difference,
        "is_mismatched": abs(difference) > tolerance,
        "snapshot_timestamp": latest_snapshot.timestamp,
    }
