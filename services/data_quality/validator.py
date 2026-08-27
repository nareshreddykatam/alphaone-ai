"""Data-quality validation for OHLCV candle data.

Every check here only *detects and labels* problems -- nothing here ever
interpolates a missing candle, repairs an invalid one, or drops a row
silently. Callers (ingestion / reporting scripts) decide what to do with
the labels; the quality layer's only job is to tell the truth about the
data as it stands.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

TIMEFRAME_TO_TIMEDELTA: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "1w": timedelta(weeks=1),
}


@dataclass
class Gap:
    start: datetime
    end: datetime
    missing_candles: int


@dataclass
class DataQualityReport:
    symbol: str
    timeframe: str
    period_start: datetime | None
    period_end: datetime | None
    row_count: int
    expected_row_count: int
    missing_count: int
    duplicate_count: int
    invalid_count: int
    out_of_order_count: int
    stale: bool
    stale_reason: str | None
    gaps: list[Gap] = field(default_factory=list)
    invalid_reasons: dict[str, int] = field(default_factory=dict)
    duplicate_timestamps: list[datetime] = field(default_factory=list)
    liquidation_coverage: str = "not_assessed"

    @property
    def coverage_pct(self) -> float:
        if self.expected_row_count <= 0:
            return 0.0
        return round(min(self.row_count, self.expected_row_count) / self.expected_row_count * 100, 4)


def check_duplicates(df: pd.DataFrame) -> tuple[int, list[datetime]]:
    dupes = df["timestamp"][df["timestamp"].duplicated(keep=False)]
    unique_dupe_ts = sorted(dupes.unique().tolist())
    return int(df["timestamp"].duplicated().sum()), unique_dupe_ts


def check_ordering(df: pd.DataFrame) -> int:
    ts = df["timestamp"]
    return int((ts.diff().dt.total_seconds() < 0).sum())


def check_invalid_timestamps(df: pd.DataFrame) -> int:
    return int(df["timestamp"].isna().sum())


def check_invalid_ohlc(df: pd.DataFrame) -> pd.Series:
    """Returns a boolean Series, True where the row is invalid."""
    o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]

    bad_high = h < pd.concat([o, c, l], axis=1).max(axis=1)
    bad_low = l > pd.concat([o, c, h], axis=1).min(axis=1)
    bad_high_low = h < l
    non_positive_price = (o <= 0) | (h <= 0) | (l <= 0) | (c <= 0)
    negative_volume = v < 0

    return bad_high | bad_low | bad_high_low | non_positive_price | negative_volume


def invalid_reason_breakdown(df: pd.DataFrame) -> dict[str, int]:
    o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]
    reasons = {
        "high_below_max(open,close,low)": int((h < pd.concat([o, c, l], axis=1).max(axis=1)).sum()),
        "low_above_min(open,close,high)": int((l > pd.concat([o, c, h], axis=1).min(axis=1)).sum()),
        "high_less_than_low": int((h < l).sum()),
        "non_positive_price": int(((o <= 0) | (h <= 0) | (l <= 0) | (c <= 0)).sum()),
        "negative_volume": int((v < 0).sum()),
    }
    return {k: v_ for k, v_ in reasons.items() if v_ > 0}


def check_gaps(df: pd.DataFrame, timeframe: str) -> list[Gap]:
    interval = TIMEFRAME_TO_TIMEDELTA.get(timeframe)
    if interval is None or len(df) < 2:
        return []

    ts = df["timestamp"].sort_values().reset_index(drop=True)
    diffs = ts.diff()

    gaps: list[Gap] = []
    for i in range(1, len(ts)):
        gap_delta = diffs.iloc[i]
        if gap_delta > interval * 1.5:
            missing = int(round(gap_delta / interval)) - 1
            if missing > 0:
                gaps.append(Gap(start=ts.iloc[i - 1], end=ts.iloc[i], missing_candles=missing))
    return gaps


def check_staleness(df: pd.DataFrame, timeframe: str, as_of: datetime | None = None, stale_multiple: float = 3.0) -> tuple[bool, str | None]:
    interval = TIMEFRAME_TO_TIMEDELTA.get(timeframe)
    if interval is None or df.empty:
        return True, "no data"

    as_of = as_of or df["timestamp"].max()
    last_ts = df["timestamp"].max()
    age = as_of - last_ts
    if age > interval * stale_multiple:
        return True, f"last candle is {age} old, more than {stale_multiple}x the {timeframe} interval"
    return False, None


def compute_expected_row_count(period_start: datetime, period_end: datetime, timeframe: str) -> int:
    interval = TIMEFRAME_TO_TIMEDELTA.get(timeframe)
    if interval is None or period_end <= period_start:
        return 0
    return int((period_end - period_start) / interval) + 1


def label_quality(df: pd.DataFrame) -> pd.DataFrame:
    """Returns a copy of df with `quality_status`/`quality_reason` columns set.

    Never mutates OHLCV values -- only annotates them. `invalid` rows fail an
    OHLC/price/volume sanity check; everything else is `valid` at the
    per-row level (gaps and staleness are period-level findings, reported
    separately in DataQualityReport, not stamped onto individual rows).
    """
    df = df.copy()
    invalid_mask = check_invalid_ohlc(df)
    df["quality_status"] = np.where(invalid_mask, "invalid", "valid")
    df["quality_reason"] = None
    if invalid_mask.any():
        reasons = []
        o, h, l, c, v = df["open"], df["high"], df["low"], df["close"], df["volume"]
        for idx in df.index[invalid_mask]:
            row_reasons = []
            if h[idx] < max(o[idx], c[idx], l[idx]):
                row_reasons.append("high_below_max(open,close,low)")
            if l[idx] > min(o[idx], c[idx], h[idx]):
                row_reasons.append("low_above_min(open,close,high)")
            if h[idx] < l[idx]:
                row_reasons.append("high_less_than_low")
            if o[idx] <= 0 or h[idx] <= 0 or l[idx] <= 0 or c[idx] <= 0:
                row_reasons.append("non_positive_price")
            if v[idx] < 0:
                row_reasons.append("negative_volume")
            reasons.append(";".join(row_reasons))
        df.loc[invalid_mask, "quality_reason"] = reasons
    return df


def validate_candles(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    as_of: datetime | None = None,
) -> DataQualityReport:
    if df.empty:
        return DataQualityReport(
            symbol=symbol, timeframe=timeframe, period_start=period_start, period_end=period_end,
            row_count=0, expected_row_count=0, missing_count=0, duplicate_count=0,
            invalid_count=0, out_of_order_count=0, stale=True, stale_reason="no data",
        )

    df = df.sort_values("timestamp").reset_index(drop=True)
    actual_start = period_start or df["timestamp"].min()
    actual_end = period_end or df["timestamp"].max()

    duplicate_count, duplicate_timestamps = check_duplicates(df)
    out_of_order_count = check_ordering(df)
    invalid_mask = check_invalid_ohlc(df)
    invalid_count = int(invalid_mask.sum())
    invalid_reasons = invalid_reason_breakdown(df)
    gaps = check_gaps(df, timeframe)
    missing_count = sum(g.missing_candles for g in gaps)
    stale, stale_reason = check_staleness(df, timeframe, as_of=as_of)
    expected_row_count = compute_expected_row_count(actual_start, actual_end, timeframe)

    return DataQualityReport(
        symbol=symbol,
        timeframe=timeframe,
        period_start=actual_start,
        period_end=actual_end,
        row_count=len(df),
        expected_row_count=expected_row_count,
        missing_count=missing_count,
        duplicate_count=duplicate_count,
        invalid_count=invalid_count,
        out_of_order_count=out_of_order_count,
        stale=stale,
        stale_reason=stale_reason,
        gaps=gaps,
        invalid_reasons=invalid_reasons,
        duplicate_timestamps=duplicate_timestamps,
    )
