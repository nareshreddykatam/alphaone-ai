"""Exploratory (not tuning) analysis: bucket a completed backtest's trades
by the market regime active at each trade's entry, and report per-regime
performance. Purely descriptive -- never used to adjust strategy parameters.
"""
from dataclasses import dataclass, field

import pandas as pd

from services.backtester.engine import BacktestResult
from services.signal_engine.regime import detect_regime_series


@dataclass
class RegimeBucketStats:
    regime: str
    trades: int
    win_rate: float
    total_pnl: float
    profit_factor: float
    average_pnl: float


def compute_regime_series(feature_df: pd.DataFrame) -> pd.Series:
    """feature_df must already have the regime-detector's input columns
    computed (FeatureEngine.compute_features output) and a `timestamp`
    column aligned to the same candles the backtest ran on."""
    return detect_regime_series(feature_df)


def bucket_trades_by_regime(result: BacktestResult, feature_df: pd.DataFrame) -> dict[str, RegimeBucketStats]:
    """Maps each trade's entry_time to the regime active at (or just before)
    that timestamp, using only information available up to that point --
    the regime series itself is already causal (see detect_regime_series).
    """
    regime_series = compute_regime_series(feature_df)
    timestamps = feature_df["timestamp"].reset_index(drop=True)
    regime_series = regime_series.reset_index(drop=True)

    buckets: dict[str, list] = {}
    for trade in result.trades:
        idx = timestamps.searchsorted(trade.entry_time, side="right") - 1
        if idx < 0 or idx >= len(regime_series):
            regime = "UNKNOWN"
        else:
            regime = regime_series.iloc[idx]
        buckets.setdefault(regime, []).append(trade)

    stats: dict[str, RegimeBucketStats] = {}
    for regime, trades in buckets.items():
        pnls = [t.pnl for t in trades]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p <= 0]
        gross_profit = sum(winners) if winners else 0
        gross_loss = abs(sum(losers)) if losers else 1
        stats[regime] = RegimeBucketStats(
            regime=regime,
            trades=len(trades),
            win_rate=round(len(winners) / len(trades) * 100, 2) if trades else 0,
            total_pnl=round(sum(pnls), 2),
            profit_factor=round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0,
            average_pnl=round(sum(pnls) / len(trades), 2) if trades else 0,
        )
    return stats


def format_regime_report(stats: dict[str, RegimeBucketStats]) -> str:
    if not stats:
        return "No trades to bucket by regime."
    lines = ["Regime breakdown (exploratory, not used to tune the strategy):"]
    lines.append(f"{'Regime':<20}{'Trades':>8}{'WinRate%':>10}{'PF':>8}{'TotalPnL':>12}{'AvgPnL':>10}")
    for regime, s in sorted(stats.items(), key=lambda kv: -kv[1].trades):
        lines.append(f"{regime:<20}{s.trades:>8}{s.win_rate:>10.2f}{s.profit_factor:>8.2f}{s.total_pnl:>12.2f}{s.average_pnl:>10.2f}")
    total_trades = sum(s.trades for s in stats.values())
    if total_trades < 30:
        lines.append(f"\nNOTE: only {total_trades} total trades -- per-regime buckets are too small for any statistical conclusion.")
    small_buckets = [r for r, s in stats.items() if s.trades < 5]
    if small_buckets:
        lines.append(f"NOTE: regimes with < 5 trades (not meaningful individually): {small_buckets}")
    return "\n".join(lines)
