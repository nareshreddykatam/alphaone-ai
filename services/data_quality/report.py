import json
from dataclasses import asdict

from services.data_quality.validator import DataQualityReport


def to_json(report: DataQualityReport) -> str:
    d = asdict(report)
    d["period_start"] = str(report.period_start) if report.period_start else None
    d["period_end"] = str(report.period_end) if report.period_end else None
    d["coverage_pct"] = report.coverage_pct
    d["duplicate_timestamps"] = [str(t) for t in report.duplicate_timestamps]
    d["gaps"] = [
        {"start": str(g.start), "end": str(g.end), "missing_candles": g.missing_candles}
        for g in report.gaps
    ]
    return json.dumps(d, indent=2, default=str)


def to_text(report: DataQualityReport) -> str:
    lines = [
        "=" * 60,
        "DATA QUALITY REPORT",
        "=" * 60,
        f"Symbol:              {report.symbol}",
        f"Timeframe:           {report.timeframe}",
        f"Period:              {report.period_start} -> {report.period_end}",
        f"Rows (actual):       {report.row_count}",
        f"Rows (expected):     {report.expected_row_count}",
        f"Coverage:            {report.coverage_pct}%",
        f"Missing rows:        {report.missing_count}",
        f"Duplicate rows:      {report.duplicate_count}",
        f"Out-of-order rows:   {report.out_of_order_count}",
        f"Invalid rows:        {report.invalid_count}",
        f"Stale:               {report.stale}" + (f" ({report.stale_reason})" if report.stale_reason else ""),
        f"Liquidation coverage:{' ' if len(report.liquidation_coverage) else ''} {report.liquidation_coverage}",
    ]
    if report.invalid_reasons:
        lines.append("Invalid row reasons:")
        for reason, count in report.invalid_reasons.items():
            lines.append(f"  - {reason}: {count}")
    if report.gaps:
        lines.append(f"Gap locations ({len(report.gaps)}):")
        for g in report.gaps[:50]:
            lines.append(f"  - {g.start} -> {g.end}  ({g.missing_candles} missing)")
        if len(report.gaps) > 50:
            lines.append(f"  ... and {len(report.gaps) - 50} more")
    lines.append("=" * 60)
    return "\n".join(lines)
