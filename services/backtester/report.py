"""Renders a BacktestResult into the structured report format requested for
Phase 2. Never fabricates a number -- every field is read directly off the
BacktestResult/run metadata. If a strategy lost money, this prints the loss.
"""
import csv
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from services.backtester.engine import BacktestResult


@dataclass
class RunMetadata:
    strategy_name: str
    symbol: str
    timeframe: str
    period_start: datetime
    period_end: datetime
    dataset_version: str | None = None
    code_version: str | None = None
    strategy_version: str = "v1"
    funding_coverage: str | None = None


def to_text(meta: RunMetadata, result: BacktestResult) -> str:
    lines = [
        "=" * 60,
        "ALPHAONE AI",
        f"{meta.symbol} PERPETUAL",
        "",
        "BACKTEST REPORT",
        "=" * 60,
        "",
        "Period:",
        f"{meta.period_start} -> {meta.period_end}",
        "",
        "Strategy:",
        f"{meta.strategy_name} ({meta.strategy_version})",
        "",
        "Timeframe:",
        meta.timeframe,
        "",
        "Trades:",
        str(result.total_trades),
        "",
        "Win Rate:",
        f"{result.win_rate}%",
        "",
        "Profit Factor:",
        f"{result.profit_factor}",
        "",
        "Expectancy:",
        f"{result.average_r} R",
        "",
        "Sharpe:",
        f"{result.sharpe_ratio}",
        "",
        "Sortino:",
        f"{result.sortino_ratio}",
        "",
        "Maximum Drawdown:",
        f"{result.max_drawdown_pct}%",
        "",
        "Net Return:",
        f"{result.total_pnl_pct}%",
        "",
        "Initial Capital:",
        f"${result.initial_capital:,.2f}",
        "",
        "Final Capital:",
        f"${result.final_capital:,.2f}",
        "",
        "Fees:",
        f"${result.total_fees:,.2f}",
        "",
        "Funding:",
        f"${result.total_funding:,.2f}",
        "",
        "Additional metrics:",
        f"  Winning trades:        {result.winning_trades}",
        f"  Losing trades:         {result.losing_trades}",
        f"  Average trade:         ${result.average_trade_pnl:,.2f}",
        f"  Average winning trade: ${result.average_winning_trade:,.2f}",
        f"  Average losing trade:  ${result.average_losing_trade:,.2f}",
        f"  Largest win:           ${result.largest_win:,.2f}",
        f"  Largest loss:          ${result.largest_loss:,.2f}",
        f"  Max consecutive wins:  {result.consecutive_wins}",
        f"  Max consecutive losses:{result.consecutive_losses}",
        f"  Recovery factor:       {result.recovery_factor}",
    ]
    if meta.dataset_version:
        lines += ["", "Dataset version:", meta.dataset_version]
    if meta.code_version:
        lines += ["", "Code version:", meta.code_version]
    lines += ["", "Funding coverage:", meta.funding_coverage or "none available (flat-average estimate used)"]

    lines.append("=" * 60)
    if result.total_pnl < 0:
        lines.append("RESULT: This strategy LOST money over this period after costs.")
    elif result.total_trades == 0:
        lines.append("RESULT: No trades were generated over this period.")
    else:
        lines.append("RESULT: This strategy was profitable over this period after costs.")
    lines.append("=" * 60)

    return "\n".join(lines)


def equity_curve_csv(result: BacktestResult) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "equity", "price"])
    for row in result.equity_curve:
        writer.writerow([row["timestamp"], row["equity"], row["price"]])
    return buf.getvalue()


def drawdown_curve_csv(result: BacktestResult) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "equity", "peak_equity", "drawdown_pct"])
    peak = None
    for row in result.equity_curve:
        eq = row["equity"]
        peak = eq if peak is None else max(peak, eq)
        dd_pct = (peak - eq) / peak * 100 if peak > 0 else 0
        writer.writerow([row["timestamp"], eq, peak, round(dd_pct, 4)])
    return buf.getvalue()


def write_report(meta: RunMetadata, result: BacktestResult, out_dir: str) -> dict[str, str]:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    slug = meta.strategy_name.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("/", "-")

    report_path = out_path / f"{slug}_report.txt"
    equity_path = out_path / f"{slug}_equity_curve.csv"
    drawdown_path = out_path / f"{slug}_drawdown_curve.csv"

    report_path.write_text(to_text(meta, result), encoding="utf-8")
    equity_path.write_text(equity_curve_csv(result), encoding="utf-8")
    drawdown_path.write_text(drawdown_curve_csv(result), encoding="utf-8")

    return {
        "report": str(report_path),
        "equity_curve": str(equity_path),
        "drawdown_curve": str(drawdown_path),
    }
