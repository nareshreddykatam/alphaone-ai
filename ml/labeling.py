"""Triple-barrier label generation for AlphaOne's LONG/SHORT/NO_TRADE model.

Replaces the Phase 1/2 approach of `DatasetLoader.create_labels` (label =
sign of the forward N-bar return past a flat threshold), which answers
"did price go up" rather than "was there a favorable risk-adjusted trading
opportunity." Per the Phase 3 brief, that distinction matters: a strategy
that predicts direction without regard to risk/reward is not what this
system is trying to build.

STRICT SEPARATION (see tests/leakage/test_label_leakage.py and
tests/unit/test_labeling.py):
  - The label MAY use future prices (that's what makes it a label at all).
  - The barrier WIDTH (ATR-based) is read at time T only -- never future ATR.
  - The feature vector used to PREDICT this label must never include
    anything computed from bars after T. This module only produces the
    `label` column; it never touches feature columns.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd


LONG_LABEL = 1
SHORT_LABEL = -1
NO_TRADE_LABEL = 0


@dataclass
class TripleBarrierConfig:
    horizon_bars: int = 12          # how many future bars the trade is allowed to develop over
    atr_col: str = "atr_14"         # ATR column read AT TIME T (already causal in the feature engine)
    tp_atr_multiple: float = 2.0    # take-profit distance = tp_atr_multiple * ATR(T)
    sl_atr_multiple: float = 1.0    # stop-loss distance   = sl_atr_multiple * ATR(T)
    min_risk_reward: float = 1.5    # a barrier configuration with worse R:R than this is not a valid setup
    entry_col: str = "open"         # entry uses the NEXT bar's open (matches Backtester's next-bar execution)


def compute_triple_barrier_labels(df: pd.DataFrame, config: TripleBarrierConfig | None = None) -> pd.DataFrame:
    """For each bar T, simulate BOTH a hypothetical LONG and a hypothetical
    SHORT entered at bar T+1's open (matching the backtester's own
    next-bar execution semantics -- see docs/execution_semantics.md), each
    with a stop and target sized off ATR(T) (known at T, not future), and
    look forward up to `horizon_bars` bars to see which barrier (if any) is
    touched first.

    Label:
      LONG   if the hypothetical long trade would hit its take-profit
             before its stop-loss (and before the horizon expires).
      SHORT  if the hypothetical short trade would hit its take-profit
             before its stop-loss.
      NO_TRADE  if neither resolves favorably, if both would (ambiguous /
             low conviction), or if there isn't enough forward data left
             to evaluate the full horizon (bars near the end of the
             dataset -- these rows are dropped, matching how a forward
             return label already had to drop its tail).

    This is a LABEL -- it is allowed to look at bars T+1..T+horizon_bars.
    The barrier WIDTH is the only thing derived from bar T itself.
    """
    config = config or TripleBarrierConfig()
    df = df.reset_index(drop=True)
    n = len(df)

    opens = df[config.entry_col].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    atr = df[config.atr_col].to_numpy(dtype=float)

    labels = np.full(n, np.nan)
    outcomes = np.full(n, "", dtype=object)  # for diagnostics: "long_win", "short_win", "no_trade", "insufficient_data"
    entry_prices = np.full(n, np.nan)
    long_targets = np.full(n, np.nan)
    long_stops = np.full(n, np.nan)
    short_targets = np.full(n, np.nan)
    short_stops = np.full(n, np.nan)
    label_end_idx = np.full(n, -1, dtype=int)  # last bar index this label's outcome depends on -- used for purge/embargo

    min_rr_ok = config.tp_atr_multiple / config.sl_atr_multiple >= config.min_risk_reward

    for t in range(n - 1):  # entry is at t+1, so t can go up to n-2
        entry_idx = t + 1
        if entry_idx + config.horizon_bars >= n:
            outcomes[t] = "insufficient_data"
            continue  # not enough forward bars to evaluate the full horizon -- drop later

        a = atr[t]
        if not np.isfinite(a) or a <= 0 or not min_rr_ok:
            outcomes[t] = "no_trade"
            labels[t] = NO_TRADE_LABEL
            label_end_idx[t] = entry_idx  # no horizon evaluated
            continue

        entry_price = opens[entry_idx]
        entry_prices[t] = entry_price

        long_tp = entry_price + config.tp_atr_multiple * a
        long_sl = entry_price - config.sl_atr_multiple * a
        short_tp = entry_price - config.tp_atr_multiple * a
        short_sl = entry_price + config.sl_atr_multiple * a
        long_targets[t], long_stops[t] = long_tp, long_sl
        short_targets[t], short_stops[t] = short_tp, short_sl

        long_hit, short_hit = None, None  # "tp" or "sl", first touch wins
        last_bar_seen = entry_idx
        for h in range(entry_idx, entry_idx + config.horizon_bars + 1):
            last_bar_seen = h
            bar_high, bar_low = highs[h], lows[h]

            if long_hit is None:
                # conservative: if a bar's range would touch BOTH TP and SL,
                # assume the stop resolves first (same convention as the
                # backtester's own simultaneous-SL/TP handling).
                if bar_low <= long_sl:
                    long_hit = "sl"
                elif bar_high >= long_tp:
                    long_hit = "tp"

            if short_hit is None:
                if bar_high >= short_sl:
                    short_hit = "sl"
                elif bar_low <= short_tp:
                    short_hit = "tp"

            if long_hit is not None and short_hit is not None:
                break

        label_end_idx[t] = last_bar_seen

        long_favorable = long_hit == "tp"
        short_favorable = short_hit == "tp"

        if long_favorable and not short_favorable:
            labels[t] = LONG_LABEL
            outcomes[t] = "long_win"
        elif short_favorable and not long_favorable:
            labels[t] = SHORT_LABEL
            outcomes[t] = "short_win"
        else:
            # neither resolved favorably, OR both did (ambiguous -- e.g. a
            # sharp whipsaw that would have stopped out immediately in one
            # direction while running to target in a fast reversal in the
            # other is not a conviction setup) -> NO_TRADE.
            labels[t] = NO_TRADE_LABEL
            outcomes[t] = "no_trade"

    out = df.copy()
    out["label"] = labels
    out["label_outcome"] = outcomes
    out["label_entry_price"] = entry_prices
    out["label_long_target"] = long_targets
    out["label_long_stop"] = long_stops
    out["label_short_target"] = short_targets
    out["label_short_stop"] = short_stops
    out["label_end_idx"] = label_end_idx
    out["label_horizon_bars"] = config.horizon_bars

    out = out.dropna(subset=["label"])
    out["label"] = out["label"].astype(int)
    return out


def label_distribution(labeled_df: pd.DataFrame) -> dict[str, float]:
    counts = labeled_df["label"].value_counts(normalize=True) * 100
    return {
        "LONG": round(float(counts.get(LONG_LABEL, 0.0)), 2),
        "SHORT": round(float(counts.get(SHORT_LABEL, 0.0)), 2),
        "NO_TRADE": round(float(counts.get(NO_TRADE_LABEL, 0.0)), 2),
    }
