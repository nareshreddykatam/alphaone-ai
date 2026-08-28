"""Phase 3: named, separately-toggleable feature groups for the ablation
study, plus multi-timeframe context (4h primary + 1h confirmation).

Every column here is built exclusively from the existing causal feature
engine (services/feature_engine/*) plus a couple of simple, clearly
causal transforms of already-causal columns (trend slope, ATR%). Nothing
in this file reads a future bar -- see tests/leakage/test_no_lookahead_features.py
and tests/unit/test_feature_groups.py for the truncation-invariance proof
extended to these additions.
"""
import numpy as np
import pandas as pd

from ml.features.strategy_features import assemble_strategy_features
from services.feature_engine.engine import FeatureEngine
from services.signal_engine.regime import detect_regime_series, MarketRegimeDetector

CONTEXT_1H_COLUMNS = ["rsi_14", "adx_14", "ema20_above_ema50", "uptrend", "downtrend", "realized_vol_20"]
CONTEXT_PREFIX = "ctx_1h_"

REGIME_NAMES = list(MarketRegimeDetector.REGIMES.values())

# Static group membership, filtered against whatever columns actually exist
# in a given assembled dataframe (so a run without derivatives data, e.g.,
# doesn't crash -- it just has an empty/smaller derivatives group).
_GROUP_DEFINITIONS: dict[str, list[str]] = {
    "trend": [
        "ema_9", "ema_20", "ema_50", "ema_100", "ema_200", "sma_20", "sma_50",
        "ema9_above_ema20", "ema20_above_ema50", "ema50_above_ema200",
        "price_vs_ema9", "price_vs_ema20", "price_vs_ema50", "price_vs_ema200",
        "price_vs_vwap", "returns", "log_returns", "trend_slope_20",
    ],
    "momentum": [
        "rsi_14", "rsi_7", "rsi_overbought", "rsi_oversold",
        "macd", "macd_signal", "macd_histogram", "macd_bullish",
        "roc_10", "momentum_10", "adx_14", "plus_di_14", "minus_di_14",
    ],
    "volatility": [
        "atr_14", "atr_7", "atr_pct_14", "realized_vol_10", "realized_vol_20", "realized_vol_50",
        "vol_ratio_short_long", "bb_width", "bb_pct", "high_low_range",
        "range_sma_20", "range_expanding", "range_contracting", "garman_klass_vol",
    ],
    "volume": [
        "volume_sma_20", "volume_sma_50", "relative_volume", "volume_change",
        "volume_spike", "volume_dry", "obv", "volume_price_divergence", "buy_sell_volume_ratio",
    ],
    "structure": [
        "higher_high", "higher_low", "lower_high", "lower_low",
        "uptrend", "downtrend", "break_of_structure",
        "near_resistance", "near_support", "consolidation", "donchian_position_20",
    ],
    "regime": [f"regime_{name}" for name in REGIME_NAMES],
    "derivatives": [
        "funding_rate", "funding_rate_sma_8", "funding_extreme_high", "funding_extreme_low",
        "funding_trend", "open_interest", "oi_change", "oi_change_sma", "oi_acceleration", "price_oi_corr",
        # Deliberately EXCLUDED: long_liquidations / short_liquidations / liq_spike.
        # No historical liquidation data exists (Binance has no public
        # backfill endpoint -- see docs/known_limitations.md); including
        # these would train on a column that is always zero historically
        # and only ever populated live, which is not a real feature, it's
        # a fabricated one.
    ],
    "context_1h": [f"{CONTEXT_PREFIX}{c}" for c in CONTEXT_1H_COLUMNS],
    # AI Trading V1, Phase 7: each PRODUCTION_ELIGIBLE rule-based strategy's
    # own LONG/SHORT/NO_TRADE output as a feature, plus simple consensus
    # aggregates -- see ml/features/strategy_features.py. Only populated
    # when assemble_features(..., include_strategy_signals=True) is used;
    # otherwise this group is empty (filtered out by get_feature_groups
    # against whatever columns actually exist).
    "strategy_signals": [
        "strat_S05_DONCHIAN_ADX_4H", "strat_S06_SUPERTREND_ATR_4H",
        "strat_V3_KAMA_TREND_4H", "strat_V3_RANGE_EXPANSION_4H",
        "strat_net_direction", "strat_agree_long", "strat_agree_short",
    ],
}

ABLATION_CONFIGS: dict[str, list[str]] = {
    "A_technical_only": ["trend", "momentum", "volatility", "volume"],
    "B_technical_structure": ["trend", "momentum", "volatility", "volume", "structure"],
    "C_technical_structure_regime": ["trend", "momentum", "volatility", "volume", "structure", "regime"],
    "D_technical_structure_regime_derivatives": [
        "trend", "momentum", "volatility", "volume", "structure", "regime", "derivatives",
    ],
    # AI Trading V1: does the existing validated strategy registry's own
    # output add anything a technical/structure/regime model doesn't
    # already have access to? Deliberately built on C (not D) -- combining
    # with the sparse ~1-month derivatives data would confound the two
    # additions' individual effect, and Phase 3 already found derivatives
    # unusable at this sample size (docs/known_limitations.md).
    "E_technical_structure_regime_strategies": [
        "trend", "momentum", "volatility", "volume", "structure", "regime", "strategy_signals",
    ],
}
# context_1h is added to every ablation model (see module docstring) -- it's
# a timeframe-architecture choice, not one of the four information-group
# axes the ablation study is comparing.


def _add_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "atr_14" in df.columns:
        df["atr_pct_14"] = df["atr_14"] / df["close"].replace(0, np.nan)
    if "ema_50" in df.columns:
        df["trend_slope_20"] = (df["ema_50"] - df["ema_50"].shift(20)) / df["close"].replace(0, np.nan)
    return df


def _one_hot_regime(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    regime_series = detect_regime_series(df)
    for name in REGIME_NAMES:
        df[f"regime_{name}"] = (regime_series == name).astype(int)
    return df


def _merge_context_timeframe(primary: pd.DataFrame, context: pd.DataFrame) -> pd.DataFrame:
    """Backward as-of merge: each primary (4h) bar gets the most recent
    context (1h) reading known at or before its own timestamp -- never a
    context reading from after the primary bar's own timestamp.

    The context columns are renamed to their `ctx_1h_*` target names
    BEFORE merging, not via merge_asof's `suffixes` -- the primary
    dataframe already has its OWN same-named columns (rsi_14, uptrend,
    etc, from its own feature computation), so relying on automatic
    suffixing would silently collide with those instead of the context
    columns actually being merged.
    """
    primary = primary.copy()
    available_cols = [c for c in CONTEXT_1H_COLUMNS if c in context.columns]
    if not available_cols:
        for c in CONTEXT_1H_COLUMNS:
            primary[f"{CONTEXT_PREFIX}{c}"] = np.nan
        return primary

    ctx = context[["timestamp"] + available_cols].sort_values("timestamp").copy()
    ctx = ctx.rename(columns={c: f"{CONTEXT_PREFIX}{c}" for c in available_cols})

    merged = pd.merge_asof(primary.sort_values("timestamp"), ctx, on="timestamp", direction="backward")
    merged = merged.set_index(primary.index)
    for c in CONTEXT_1H_COLUMNS:
        if f"{CONTEXT_PREFIX}{c}" not in merged.columns:
            merged[f"{CONTEXT_PREFIX}{c}"] = np.nan
    return merged


def assemble_features(
    df_primary: pd.DataFrame,
    df_context: pd.DataFrame | None = None,
    funding_rates: pd.DataFrame | None = None,
    open_interest: pd.DataFrame | None = None,
    include_strategy_signals: bool = False,
) -> tuple[pd.DataFrame, list[str]]:
    """Builds the full Phase 3 feature set on the primary (4h) timeframe.

    Returns (assembled_df, feature_names) where feature_names is every
    column produced by the feature engine plus this module's additions
    (regime one-hot, derived trend/ATR% columns, 1h context, and -- only
    when `include_strategy_signals=True` -- each production-eligible
    strategy's own signal, see ml/features/strategy_features.py) -- NOT
    filtered to any particular ablation group; use `select_features()` for
    that.
    """
    engine = FeatureEngine()
    df = engine.compute_features(df_primary, funding_rates=funding_rates, open_interest=open_interest)
    df = _add_derived_columns(df)
    df = _one_hot_regime(df)

    if df_context is not None and not df_context.empty:
        context_features = FeatureEngine().compute_features(df_context)
        df = _merge_context_timeframe(df, context_features)
    else:
        for c in CONTEXT_1H_COLUMNS:
            df[f"{CONTEXT_PREFIX}{c}"] = np.nan

    if include_strategy_signals:
        df, _ = assemble_strategy_features(df)

    feature_names = [c for c in df.columns if c not in (
        "timestamp", "open", "high", "low", "close", "volume", "symbol", "timeframe",
    )]
    return df, feature_names


def get_feature_groups(df: pd.DataFrame) -> dict[str, list[str]]:
    """Group definitions filtered to columns that actually exist in `df`."""
    return {group: [c for c in cols if c in df.columns] for group, cols in _GROUP_DEFINITIONS.items()}


def select_features(df: pd.DataFrame, ablation_name: str) -> list[str]:
    if ablation_name not in ABLATION_CONFIGS:
        raise ValueError(f"Unknown ablation config: {ablation_name}. Options: {list(ABLATION_CONFIGS)}")
    groups = get_feature_groups(df)
    group_names = ABLATION_CONFIGS[ablation_name] + ["context_1h"]
    cols: list[str] = []
    seen = set()
    for g in group_names:
        for c in groups.get(g, []):
            if c not in seen:
                cols.append(c)
                seen.add(c)
    return cols
