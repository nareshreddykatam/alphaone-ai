"""Strategy-signal features for the AI orchestrator (AI Trading V1, Phase 7).

Turns each PRODUCTION_ELIGIBLE rule-based strategy's own LONG/SHORT/NO_TRADE
output into a per-bar feature column, computed the exact same way that
strategy's own live evaluation and backtest already compute it (same
precompute + signal_func pair Backtester.run() itself calls -- see
docs/execution_semantics.md) -- never a separate re-implementation of any
strategy's logic that could silently diverge from its real behavior.

Only strategies that are PRODUCTION_ELIGIBLE TODAY (services/signal_engine/
multi_strategy.py) are used as features. A RESEARCH_ONLY or REJECTED
strategy's signal is not real evidence of anything and would just be
feeding the model noise dressed up as a feature.
"""
import numpy as np
import pandas as pd

from ml.evaluation.baselines import trend_following_signal_func, _precompute_indicators as _precompute_s05
from ml.evaluation.multi_strategy_signals import MULTI_STRATEGIES

STRATEGY_FEATURE_IDS = [
    "S05_DONCHIAN_ADX_4H", "S06_SUPERTREND_ATR_4H", "V3_KAMA_TREND_4H", "V3_RANGE_EXPANSION_4H",
]

_SIGNAL_TO_INT = {"LONG": 1, "SHORT": -1}


def _get_precompute_and_signal(strategy_id: str):
    if strategy_id == "S05_DONCHIAN_ADX_4H":
        return _precompute_s05, trend_following_signal_func(20, 25)
    spec = MULTI_STRATEGIES[strategy_id]
    return spec["precompute"], spec["factory"]()


def compute_strategy_signal_series(df: pd.DataFrame, strategy_id: str, min_bars: int = 60) -> pd.Series:
    """One strategy's full historical signal series: -1 (SHORT) / 0
    (NO_TRADE) / 1 (LONG) at every bar, computed the identical way
    Backtester.run() calls it -- precompute ONCE over the full causal
    history (already proven truncation-invariant), then
    signal_func(df.iloc[:i+1]) per bar, so each row only ever reflects
    information available up to and including that bar."""
    precompute_fn, signal_func = _get_precompute_and_signal(strategy_id)
    prepared = precompute_fn(df)
    n = len(prepared)
    out = np.zeros(n)
    for i in range(min_bars, n):
        raw = signal_func(prepared.iloc[:i + 1])
        if raw is not None:
            out[i] = _SIGNAL_TO_INT.get(raw.get("signal_type"), 0)
    return pd.Series(out, index=df.index)


def assemble_strategy_features(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Adds one `strat_<ID>` column per production-eligible strategy plus
    three aggregate consensus columns. Never suppresses or requires
    agreement between strategies -- these are read-only features describing
    what each strategy independently said at that bar, for the model to
    weigh however training finds useful."""
    df = df.copy()
    cols = []
    for sid in STRATEGY_FEATURE_IDS:
        col = f"strat_{sid}"
        df[col] = compute_strategy_signal_series(df, sid)
        cols.append(col)
    df["strat_net_direction"] = df[cols].sum(axis=1)
    df["strat_agree_long"] = (df[cols] == 1).sum(axis=1)
    df["strat_agree_short"] = (df[cols] == -1).sum(axis=1)
    agg_cols = ["strat_net_direction", "strat_agree_long", "strat_agree_short"]
    return df, cols + agg_cols
