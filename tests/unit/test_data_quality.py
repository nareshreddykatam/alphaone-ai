from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from services.data_quality.validator import (
    validate_candles, label_quality, check_gaps, check_duplicates,
    check_invalid_ohlc, check_staleness, compute_expected_row_count,
)


def _clean_df(n=48, start=None):
    start = start or datetime(2024, 1, 1)
    rows = []
    for i in range(n):
        ts = start + timedelta(hours=i)
        rows.append({"timestamp": ts, "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10})
    return pd.DataFrame(rows)


def test_valid_data_reports_full_coverage_and_no_issues():
    df = _clean_df(48)
    report = validate_candles(df, "BTC/USDT", "1h", as_of=df["timestamp"].max())

    assert report.coverage_pct == 100.0
    assert report.missing_count == 0
    assert report.duplicate_count == 0
    assert report.invalid_count == 0
    assert report.out_of_order_count == 0
    assert report.stale is False


def test_gap_detection_reports_missing_candles():
    df = _clean_df(48)
    df = df[df["timestamp"] != df["timestamp"].iloc[10]].reset_index(drop=True)  # remove 1 candle

    report = validate_candles(df, "BTC/USDT", "1h", as_of=df["timestamp"].max())
    assert report.missing_count == 1
    assert len(report.gaps) == 1
    assert report.gaps[0].missing_candles == 1
    assert report.coverage_pct < 100.0


def test_duplicate_detection():
    df = _clean_df(48)
    df = pd.concat([df, df.iloc[[5]]], ignore_index=True)

    report = validate_candles(df, "BTC/USDT", "1h")
    assert report.duplicate_count == 1
    assert df["timestamp"].iloc[5] in report.duplicate_timestamps


@pytest.mark.parametrize("mutate,expected_reason", [
    (lambda df: df.assign(high=df["low"] - 1), "high_below_max(open,close,low)"),
    (lambda df: df.assign(low=df["high"] + 1), "low_above_min(open,close,high)"),
    (lambda df: df.assign(open=-1.0), "non_positive_price"),
    (lambda df: df.assign(volume=-5.0), "negative_volume"),
])
def test_invalid_ohlc_variants_are_detected(mutate, expected_reason):
    df = _clean_df(10)
    bad_row = 3
    mutated = mutate(df.copy())
    df.loc[bad_row, mutated.columns] = mutated.loc[bad_row]

    mask = check_invalid_ohlc(df)
    assert mask.iloc[bad_row]

    labeled = label_quality(df)
    assert labeled.loc[bad_row, "quality_status"] == "invalid"
    assert expected_reason in labeled.loc[bad_row, "quality_reason"]


def test_staleness_detected_when_last_candle_too_old():
    df = _clean_df(48)
    as_of = df["timestamp"].max() + timedelta(hours=10)  # way more than 3x the 1h interval
    stale, reason = check_staleness(df, "1h", as_of=as_of)
    assert stale is True
    assert reason is not None


def test_empty_dataframe_reports_as_stale_with_zero_rows():
    df = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    report = validate_candles(df, "BTC/USDT", "1h")
    assert report.row_count == 0
    assert report.stale is True
    assert report.coverage_pct == 0.0


def test_expected_row_count_matches_interval_math():
    start = datetime(2024, 1, 1)
    end = start + timedelta(hours=23)
    assert compute_expected_row_count(start, end, "1h") == 24
