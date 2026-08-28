"""Model / AI-orchestrator health monitoring (AI Trading V1, Phase 13).

Computes a coarse HEALTHY / WARNING / DEGRADED / DISABLED status from real,
already-persisted data -- recent AI paper trades (Trade rows, mode="paper",
source=AI_PAPER) and, when a model is actually deployed, recent Prediction
rows (services/signal_engine/ai_orchestrator.py writes one per model-backed
decision) -- never a fabricated or hardcoded status. Used as a gate:
services/scheduler/jobs.py's ai_paper_trading_job refuses to open a NEW
paper position while status is DISABLED (mirrors the existing
is_signal_generation_paused gate), though it always keeps managing
already-open positions' SL/TP regardless of status -- a degraded model is
never a reason to abandon risk management on a live paper position.
"""
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import Trade, Prediction, TradeSource, TradeStatus


class ModelHealthStatus:
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    DEGRADED = "DEGRADED"
    DISABLED = "DISABLED"


@dataclass
class ModelHealthReport:
    status: str
    reasons: list = field(default_factory=list)
    recent_paper_trades: int = 0
    recent_win_rate: float = None
    recent_profit_factor: float = None
    recent_max_drawdown_pct: float = None
    prediction_class_distribution: dict = None
    lookback_trades: int = 0


# Thresholds -- fixed, documented, pre-declared (not tuned to produce a
# particular headline status), mirroring RiskEngine's own configured-
# constant pattern. Only evaluated once at least MIN_TRADES_FOR_EVALUATION
# paper trades exist -- a handful of trades is too small a sample to call
# "degraded" (the same >=15-trade floor select_best_params already uses
# elsewhere in this codebase for the same reason).
MIN_TRADES_FOR_EVALUATION = 15
LOOKBACK_TRADES = 30
DISABLED_PF_THRESHOLD = 0.4       # a real, severe breach -- roughly RiskEngine's own hard-kill posture, not a soft warning
DISABLED_DRAWDOWN_PCT = 25.0
DEGRADED_PF_THRESHOLD = 0.7
DEGRADED_DRAWDOWN_PCT = 15.0
WARNING_PF_THRESHOLD = 1.0
WARNING_DRAWDOWN_PCT = 10.0
PREDICTION_CLASS_IMBALANCE_WARNING = 0.90  # one class >=90% of recent predictions is a real drift signal

# Reference starting equity for the drawdown calculation below -- matches
# PaperTrader's own default initial_equity (services/paper_trader/engine.py).
# A $0 baseline would make the percentage wildly sensitive to whichever
# trade happens to close first (e.g. a single early loss on a near-zero
# "peak" can look like an enormous percentage drawdown) -- a realistic
# reference account size is what makes "max_dd_pct" mean the same thing
# here as it does everywhere else in this codebase (RiskEngine, Backtester).
DRAWDOWN_REFERENCE_EQUITY = 10000.0


async def evaluate_model_health(session: AsyncSession) -> ModelHealthReport:
    result = await session.execute(
        select(Trade).where(
            Trade.mode == "paper", Trade.source == TradeSource.AI_PAPER.value,
            Trade.status == TradeStatus.CLOSED.value,
        ).order_by(Trade.exit_time.desc()).limit(LOOKBACK_TRADES)
    )
    trades = list(result.scalars().all())

    report = ModelHealthReport(status=ModelHealthStatus.HEALTHY, lookback_trades=len(trades))

    if len(trades) >= MIN_TRADES_FOR_EVALUATION:
        wins = [t for t in trades if (t.pnl or 0) > 0]
        losses = [t for t in trades if (t.pnl or 0) <= 0]
        gross_profit = sum(t.pnl for t in wins) if wins else 0.0
        gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0.0
        pf = (gross_profit / gross_loss) if gross_loss > 0 else None
        win_rate = len(wins) / len(trades) * 100

        equity = DRAWDOWN_REFERENCE_EQUITY
        peak = DRAWDOWN_REFERENCE_EQUITY
        max_dd = 0.0
        for t in sorted(trades, key=lambda tr: tr.exit_time or datetime.min):
            equity += (t.pnl or 0)
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak * 100)

        report.recent_paper_trades = len(trades)
        report.recent_win_rate = round(win_rate, 2)
        report.recent_profit_factor = round(pf, 2) if pf is not None else None
        report.recent_max_drawdown_pct = round(max_dd, 2)

        if (pf is not None and pf < DISABLED_PF_THRESHOLD) or max_dd >= DISABLED_DRAWDOWN_PCT:
            report.status = ModelHealthStatus.DISABLED
            report.reasons.append(
                f"Last {len(trades)} AI paper trades: PF={report.recent_profit_factor}, "
                f"max_dd={report.recent_max_drawdown_pct}% -- severe enough (PF<{DISABLED_PF_THRESHOLD} or "
                f"drawdown>={DISABLED_DRAWDOWN_PCT}%) that new AI paper positions are refused until reviewed."
            )
        elif (pf is not None and pf < DEGRADED_PF_THRESHOLD) or max_dd >= DEGRADED_DRAWDOWN_PCT:
            report.status = ModelHealthStatus.DEGRADED
            report.reasons.append(
                f"Last {len(trades)} AI paper trades: PF={report.recent_profit_factor}, "
                f"max_dd={report.recent_max_drawdown_pct}% -- below the degraded threshold "
                f"(PF<{DEGRADED_PF_THRESHOLD} or drawdown>={DEGRADED_DRAWDOWN_PCT}%)."
            )
        elif (pf is not None and pf < WARNING_PF_THRESHOLD) or max_dd >= WARNING_DRAWDOWN_PCT:
            report.status = ModelHealthStatus.WARNING
            report.reasons.append(
                f"Last {len(trades)} AI paper trades: PF={report.recent_profit_factor}, "
                f"max_dd={report.recent_max_drawdown_pct}% -- below the warning threshold."
            )

    pred_result = await session.execute(select(Prediction).order_by(Prediction.timestamp.desc()).limit(LOOKBACK_TRADES))
    predictions = list(pred_result.scalars().all())
    if len(predictions) >= MIN_TRADES_FOR_EVALUATION:
        counts = {"LONG": 0, "SHORT": 0, "NO_TRADE": 0}
        for p in predictions:
            counts[p.signal_type] = counts.get(p.signal_type, 0) + 1
        total = len(predictions)
        report.prediction_class_distribution = {k: round(v / total, 3) for k, v in counts.items()}
        max_share = max(report.prediction_class_distribution.values())
        if max_share >= PREDICTION_CLASS_IMBALANCE_WARNING:
            dominant = max(report.prediction_class_distribution, key=report.prediction_class_distribution.get)
            if report.status == ModelHealthStatus.HEALTHY:
                report.status = ModelHealthStatus.WARNING
            report.reasons.append(
                f"{max_share:.0%} of the last {total} predictions were '{dominant}' -- possible feature/regime "
                f"drift away from the distribution the model was validated on."
            )

    return report
