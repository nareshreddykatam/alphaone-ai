# Data quality methodology

Implemented in `services/data_quality/validator.py` and
`services/data_quality/report.py`.

## Principle: detect and label, never repair

Every check in this module only inspects and annotates data. **Nothing
here interpolates a missing candle, corrects an invalid OHLC row, or
silently drops a duplicate.** A `Candle` row's `quality_status` /
`quality_reason` columns (added in migration `002_...`) record what was
found; it is the caller's (ingestion job's / researcher's) responsibility
to decide whether to exclude flagged periods from a given analysis.

This is deliberate: silently repairing financial market data hides the
fact that a gap or bad print occurred, which can make a backtest look
cleaner than the data actually supports.

## Checks performed

- **Duplicates** -- exact-timestamp repeats (`check_duplicates`).
- **Ordering** -- any non-monotonic timestamp sequence (`check_ordering`).
- **Invalid timestamps** -- `NaT` / unparseable (`check_invalid_timestamps`).
- **Invalid OHLC relationships** (`check_invalid_ohlc`): a row is invalid if
  any of the following hold:
  - `high < max(open, close, low)`
  - `low > min(open, close, high)`
  - `high < low`
  - any of open/high/low/close `<= 0`
  - `volume < 0`
- **Gaps** (`check_gaps`) -- any interval between consecutive candles more
  than 1.5x the expected timeframe interval is reported as a gap, with the
  number of missing candles implied by the interval.
- **Staleness** (`check_staleness`) -- the most recent candle is more than
  3x the timeframe's interval older than the reference "as of" time.

## Report contents

`DataQualityReport` (and its text/JSON rendering in `report.py`) carries:
dataset period, actual vs. expected row count, coverage %, missing count,
duplicate count, invalid count, out-of-order count, gap locations (start,
end, missing-candle count), a breakdown of *why* rows were invalid, and a
staleness flag/reason.

## Dataset coverage (as of Phase 2.5)

Extended from Phase 2's ~1 year to ~3 years for the timeframes prioritized
for research (1h, 4h, 15m, 5m, 1d), to expose baselines to more market
regimes (2023's consolidation, 2024-2025 trend/volatility, etc.) rather than
one narrow trailing year. 1m candles were deliberately **left at their
existing 1-year coverage** -- extending 1m to 3 years would have meant
~1.5M additional rows for comparatively little incremental research value
at that resolution, and the Phase 2.5 brief explicitly says not to let a
large 1m download block the rest of the work. All extended timeframes
validated at **100% coverage, 0 gaps, 0 duplicates, 0 invalid rows** (see
the per-timeframe JSON reports in `reports/`).

| Timeframe | Coverage period | Rows |
|---|---|---|
| 1m | ~1 year (2025-08-26 -> 2026-08-26) | 525,600 |
| 5m | ~3 years (2023-08-27 -> 2026-08-26) | 315,360 |
| 15m | ~3 years | 105,120 |
| 1h | ~3 years | 26,280 |
| 4h | ~3 years | 6,570 |
| 1d | ~3 years | 1,095 |

## Known, documented gaps in coverage (not silently hidden)

- **Open interest**: Binance's public API only serves ~30 days of OI
  history regardless of the requested start date. A 365-day OHLCV dataset
  will only ever have ~30 days of matching OI data. This is an exchange
  limitation, not an ingestion bug -- see `docs/known_limitations.md`.
- **Liquidations**: Binance has no public historical-liquidation backfill
  endpoint. Only a recent/live snapshot is obtainable
  (`fetch_recent_liquidations`). The data-quality report's
  `liquidation_coverage` field must be read as "recent-only," never as a
  complete historical series -- do not use it as a dense feature over the
  full backtest period without accounting for this.

## Downstream contract

Feature generation and backtesting should be run only over periods with
`quality_status == "valid"` at the required coverage level for the
analysis at hand; a period with `stale=True` or a large `missing_count`
relative to `expected_row_count` should be treated as invalid for signal
generation, per the Phase 2 requirement that a missing critical candle
must prevent affected signals and (where practical) affected backtest
periods -- rather than being silently interpolated over.
