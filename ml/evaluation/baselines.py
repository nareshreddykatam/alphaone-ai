"""Baseline strategies, run through the same Backtester as everything else.

Per the Phase 2 requirement, baselines must not get their own private cost
model -- they are `signal_func` closures fed into `Backtester.run(df, fn)`
exactly like any other strategy, so they automatically share fees, slippage,
funding, position sizing, next-bar execution timing, and risk-engine gating.
No baseline computes its own PnL loop anymore.

Indicators used by the signal functions are precomputed ONCE over the whole
dataset before the backtest loop runs, rather than recomputed from scratch
on every `df.iloc[:i+1]` call `Backtester.run` makes. This is purely a
performance optimization, not a correctness relaxation: every indicator
here (EMA/RSI/ATR/ADX/rolling max-min) is strictly causal, so its value at
row i is identical whether computed over the full series and read at row i,
or recomputed from scratch on a truncated `df.iloc[:i+1]` slice -- see
tests/leakage/test_no_lookahead_features.py for the general proof of that
property across the feature engine.
"""
import pandas as pd

from services.backtester.engine import Backtester, BacktestConfig, BacktestResult
from services.feature_engine.indicators import ema, rsi, atr, adx


def _default_sl_tp(entry_price: float, atr_val: float, side: str) -> tuple[float, float]:
    """A fixed 2x-ATR stop / 3x-ATR target, purely so every baseline has a
    concrete risk-managed exit -- not a claim that this is the optimal
    stop/target choice for any of these strategies."""
    if side == "LONG":
        return entry_price - 2 * atr_val, entry_price + 3 * atr_val
    return entry_price + 2 * atr_val, entry_price - 3 * atr_val


def _precompute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["_ema_fast"] = ema(df["close"], 9)
    df["_ema_slow"] = ema(df["close"], 21)
    df["_rsi_14"] = rsi(df["close"], 14)
    df["_atr_14"] = atr(df["high"], df["low"], df["close"], 14)
    df["_adx_14"] = adx(df["high"], df["low"], df["close"], 14)
    df["_donchian_high_20"] = df["high"].rolling(20).max()
    df["_donchian_low_20"] = df["low"].rolling(20).min()
    return df


def buy_and_hold_signal_func(position_pct: float = 0.95):
    """Enter once with a fixed fraction of equity at 1x leverage and never
    exit before end-of-data -- buy-and-hold has no stop by definition, so it
    uses the engine's fixed-notional sizing path instead of risk-to-stop
    sizing (see Backtester._open_trade), but still shares every cost/timing
    rule with the other baselines."""
    fired = {"done": False}

    def _signal(df: pd.DataFrame) -> dict | None:
        if fired["done"] or len(df) < 2:
            return None
        fired["done"] = True
        return {
            "signal_type": "LONG", "stop_loss": None, "take_profit_1": None,
            "leverage": 1, "position_pct": position_pct,
        }

    return _signal


def ema_crossover_signal_func(fast_period: int = 9, slow_period: int = 21):
    def _signal(df: pd.DataFrame) -> dict | None:
        if len(df) < slow_period + 2:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        if pd.isna(prev["_ema_fast"]) or pd.isna(prev["_ema_slow"]) or pd.isna(last["_atr_14"]) or last["_atr_14"] <= 0:
            return None

        entry = last["close"]
        if prev["_ema_fast"] <= prev["_ema_slow"] and last["_ema_fast"] > last["_ema_slow"]:
            sl, tp = _default_sl_tp(entry, last["_atr_14"], "LONG")
            return {"signal_type": "LONG", "stop_loss": sl, "take_profit_1": tp, "leverage": 1}
        if prev["_ema_fast"] >= prev["_ema_slow"] and last["_ema_fast"] < last["_ema_slow"]:
            sl, tp = _default_sl_tp(entry, last["_atr_14"], "SHORT")
            return {"signal_type": "SHORT", "stop_loss": sl, "take_profit_1": tp, "leverage": 1}
        return None

    return _signal


def rsi_signal_func(period: int = 14, oversold: float = 30, overbought: float = 70):
    def _signal(df: pd.DataFrame) -> dict | None:
        if len(df) < period + 12:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        if pd.isna(prev["_rsi_14"]) or pd.isna(last["_rsi_14"]) or pd.isna(last["_atr_14"]) or last["_atr_14"] <= 0:
            return None

        entry = last["close"]
        if prev["_rsi_14"] < oversold <= last["_rsi_14"]:
            sl, tp = _default_sl_tp(entry, last["_atr_14"], "LONG")
            return {"signal_type": "LONG", "stop_loss": sl, "take_profit_1": tp, "leverage": 1}
        if prev["_rsi_14"] > overbought >= last["_rsi_14"]:
            sl, tp = _default_sl_tp(entry, last["_atr_14"], "SHORT")
            return {"signal_type": "SHORT", "stop_loss": sl, "take_profit_1": tp, "leverage": 1}
        return None

    return _signal


def trend_following_signal_func(breakout_period: int = 20, adx_threshold: float = 25):
    """Donchian-channel breakout, gated by ADX to require an established
    trend -- a standard, unglamorous trend-following baseline."""

    def _signal(df: pd.DataFrame) -> dict | None:
        if len(df) < breakout_period + 20:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        if pd.isna(last["_adx_14"]) or pd.isna(last["_atr_14"]) or last["_atr_14"] <= 0:
            return None
        if last["_adx_14"] < adx_threshold:
            return None
        if pd.isna(prev["_donchian_high_20"]) or pd.isna(prev["_donchian_low_20"]):
            return None

        entry = last["close"]
        if entry > prev["_donchian_high_20"]:
            sl, tp = _default_sl_tp(entry, last["_atr_14"], "LONG")
            return {"signal_type": "LONG", "stop_loss": sl, "take_profit_1": tp, "leverage": 1}
        if entry < prev["_donchian_low_20"]:
            sl, tp = _default_sl_tp(entry, last["_atr_14"], "SHORT")
            return {"signal_type": "SHORT", "stop_loss": sl, "take_profit_1": tp, "leverage": 1}
        return None

    return _signal


BASELINE_STRATEGIES: dict[str, tuple[str, "callable"]] = {
    "buy_and_hold": ("Buy and Hold", buy_and_hold_signal_func),
    "ema_crossover": ("EMA Crossover (9/21)", lambda: ema_crossover_signal_func(9, 21)),
    "rsi_strategy": ("RSI Strategy (14)", lambda: rsi_signal_func(14, 30, 70)),
    "trend_following": ("Trend Following (Donchian 20 + ADX)", lambda: trend_following_signal_func(20, 25)),
}


def run_baseline(
    name: str, df: pd.DataFrame, config: BacktestConfig | None = None,
    funding_rates: pd.DataFrame | None = None,
) -> tuple[str, BacktestResult]:
    if name not in BASELINE_STRATEGIES:
        raise ValueError(f"Unknown baseline: {name}. Options: {list(BASELINE_STRATEGIES)}")
    display_name, factory = BASELINE_STRATEGIES[name]
    signal_func = factory()
    prepared_df = df if name == "buy_and_hold" else _precompute_indicators(df)
    bt = Backtester(config or BacktestConfig())
    result = bt.run(prepared_df, signal_func, funding_rates=funding_rates)
    return display_name, result


def run_all_baselines(
    df: pd.DataFrame, config: BacktestConfig | None = None, funding_rates: pd.DataFrame | None = None,
) -> dict[str, BacktestResult]:
    return {name: run_baseline(name, df, config, funding_rates)[1] for name in BASELINE_STRATEGIES}
