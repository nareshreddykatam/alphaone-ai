"""Signal functions for the 9 NEW independent strategies (S01-S04 at 15m,
S06-S10 at 4h) requested alongside the existing validated S05 (Donchian+ADX,
services/signal_engine/strategy.py's BaselineStrategy -- untouched).

Same contract as ml/evaluation/baselines.py: each `_signal(df) -> dict|None`
closure is fed straight into services.backtester.engine.Backtester.run(),
so every strategy here automatically shares the exact same fee/slippage/
funding/position-sizing/next-bar-execution model as the validated baseline
-- no strategy gets its own private, more-favorable cost assumption.

Every strategy is a genuinely different mechanism (see each docstring for
its market hypothesis) -- not the same setup with different parameter
values. None is assumed profitable; ml/evaluation/multi_strategy_runner.py
backtests and walk-forward validates all of them against real data before
any classification is made.
"""
import numpy as np
import pandas as pd

from services.feature_engine.indicators import (
    ema, rsi, atr, adx, macd, bollinger_bands, vwap_session, sma, supertrend,
)


def _atr_sl_tp(entry: float, atr_val: float, side: str, sl_mult: float = 2.0, tp_mults=(1.5, 2.5, 3.5)) -> dict:
    """Shared ATR-scaled SL/TP1/TP2/TP3 -- a documented, fixed risk-management
    convention (not claimed optimal for any one strategy), consistent with
    the existing baselines' `_default_sl_tp`."""
    if side == "LONG":
        sl = entry - sl_mult * atr_val
        tps = [entry + m * atr_val for m in tp_mults]
    else:
        sl = entry + sl_mult * atr_val
        tps = [entry - m * atr_val for m in tp_mults]
    return {"stop_loss": sl, "take_profit_1": tps[0], "take_profit_2": tps[1], "take_profit_3": tps[2]}


# ---------------------------------------------------------------------------
# S01 -- Momentum / Volatility Breakout (15m)
#
# Hypothesis: a genuine volatility EXPANSION (rising ATR) breaking a recent
# trading range, confirmed by above-average volume, is more likely to
# continue than a quiet-range breakout with no participation behind it.
# ---------------------------------------------------------------------------

def precompute_momentum_breakout(df: pd.DataFrame, lookback: int = 20) -> pd.DataFrame:
    df = df.copy()
    df["_atr_14"] = atr(df["high"], df["low"], df["close"], 14)
    df["_range_high"] = df["high"].rolling(lookback).max()
    df["_range_low"] = df["low"].rolling(lookback).min()
    df["_vol_sma_20"] = sma(df["volume"], 20)
    return df


def momentum_breakout_signal_func(lookback: int = 20, volume_mult: float = 1.5, atr_lookback: int = 5):
    def _signal(df: pd.DataFrame) -> dict | None:
        if len(df) < lookback + atr_lookback + 5:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        if pd.isna(prev["_range_high"]) or pd.isna(prev["_range_low"]) or pd.isna(last["_atr_14"]) or last["_atr_14"] <= 0:
            return None
        if pd.isna(last["_vol_sma_20"]) or last["_vol_sma_20"] <= 0:
            return None
        atr_series = df["_atr_14"]
        if len(atr_series) < atr_lookback + 1 or pd.isna(atr_series.iloc[-1 - atr_lookback]):
            return None
        expanding = last["_atr_14"] > atr_series.iloc[-1 - atr_lookback]
        volume_confirmed = last["volume"] > volume_mult * last["_vol_sma_20"]
        if not (expanding and volume_confirmed):
            return None

        entry = last["close"]
        if entry > prev["_range_high"]:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG")}
        if entry < prev["_range_low"]:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT")}
        return None

    return _signal


# ---------------------------------------------------------------------------
# S02 -- EMA Pullback / Trend Continuation (15m)
#
# Hypothesis: in an established short-term trend (EMA20 vs EMA50), a shallow
# pullback that TOUCHES the faster EMA and is immediately reclaimed (not
# broken through) is a lower-risk continuation entry than chasing the
# original breakout.
# ---------------------------------------------------------------------------

def precompute_ema_pullback(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["_ema_20"] = ema(df["close"], 20)
    df["_ema_50"] = ema(df["close"], 50)
    df["_atr_14"] = atr(df["high"], df["low"], df["close"], 14)
    df["_rsi_14"] = rsi(df["close"], 14)
    return df


def ema_pullback_signal_func(rsi_floor: float = 40, rsi_ceiling: float = 60):
    def _signal(df: pd.DataFrame) -> dict | None:
        if len(df) < 55:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        cols = ("_ema_20", "_ema_50", "_atr_14", "_rsi_14")
        if any(pd.isna(last[c]) or pd.isna(prev[c]) for c in cols) or last["_atr_14"] <= 0:
            return None

        entry = last["close"]
        uptrend = last["_ema_20"] > last["_ema_50"]
        downtrend = last["_ema_20"] < last["_ema_50"]

        touched_from_above = prev["low"] <= prev["_ema_20"] and prev["close"] >= prev["_ema_20"] * 0.999
        reclaimed_up = entry > last["_ema_20"]
        if uptrend and touched_from_above and reclaimed_up and last["_rsi_14"] > rsi_floor:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG")}

        touched_from_below = prev["high"] >= prev["_ema_20"] and prev["close"] <= prev["_ema_20"] * 1.001
        rejected_down = entry < last["_ema_20"]
        if downtrend and touched_from_below and rejected_down and last["_rsi_14"] < rsi_ceiling:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT")}

        return None

    return _signal


# ---------------------------------------------------------------------------
# S03 -- VWAP Mean Reversion (15m)
#
# Hypothesis: an intraday session-VWAP deviation of unusual size (measured
# in ATR units, so it adapts to current volatility rather than a fixed
# percentage) tends to partially revert, especially once price stops
# extending further away from VWAP.
# ---------------------------------------------------------------------------

def precompute_vwap_reversion(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["_vwap"] = vwap_session(df["timestamp"], df["high"], df["low"], df["close"], df["volume"])
    df["_atr_14"] = atr(df["high"], df["low"], df["close"], 14)
    return df


def vwap_reversion_signal_func(deviation_atr_mult: float = 2.5):
    def _signal(df: pd.DataFrame) -> dict | None:
        if len(df) < 30:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        if pd.isna(last["_vwap"]) or pd.isna(last["_atr_14"]) or last["_atr_14"] <= 0:
            return None
        if pd.isna(prev["_vwap"]):
            return None

        entry = last["close"]
        deviation = entry - last["_vwap"]
        prev_deviation = prev["close"] - prev["_vwap"]
        threshold = deviation_atr_mult * last["_atr_14"]

        # Oversold vs VWAP AND starting to turn back up (deviation shrinking).
        if deviation < -threshold and deviation > prev_deviation:
            return {
                "signal_type": "LONG", "leverage": 1,
                "stop_loss": entry - 1.5 * last["_atr_14"],
                "take_profit_1": last["_vwap"], "take_profit_2": last["_vwap"] + 0.5 * last["_atr_14"], "take_profit_3": None,
            }
        # Overbought vs VWAP AND starting to turn back down.
        if deviation > threshold and deviation < prev_deviation:
            return {
                "signal_type": "SHORT", "leverage": 1,
                "stop_loss": entry + 1.5 * last["_atr_14"],
                "take_profit_1": last["_vwap"], "take_profit_2": last["_vwap"] - 0.5 * last["_atr_14"], "take_profit_3": None,
            }
        return None

    return _signal


# ---------------------------------------------------------------------------
# S04 -- RSI + Bollinger Momentum/Reversal (15m)
#
# Hypothesis: a close piercing a Bollinger band (2 std dev, 20-period)
# while RSI is simultaneously at a momentum extreme, followed by a close
# back INSIDE the band, marks short-term exhaustion.
# ---------------------------------------------------------------------------

def precompute_rsi_bollinger(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["_rsi_14"] = rsi(df["close"], 14)
    upper, mid, lower = bollinger_bands(df["close"], 20, 2.0)
    df["_bb_upper"] = upper
    df["_bb_mid"] = mid
    df["_bb_lower"] = lower
    df["_atr_14"] = atr(df["high"], df["low"], df["close"], 14)
    return df


def rsi_bollinger_signal_func(rsi_oversold: float = 30, rsi_overbought: float = 70):
    def _signal(df: pd.DataFrame) -> dict | None:
        if len(df) < 25:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        cols = ("_rsi_14", "_bb_upper", "_bb_mid", "_bb_lower", "_atr_14")
        if any(pd.isna(last[c]) or pd.isna(prev[c]) for c in cols) or last["_atr_14"] <= 0:
            return None

        entry = last["close"]
        if prev["close"] < prev["_bb_lower"] and entry >= last["_bb_lower"] and prev["_rsi_14"] < rsi_oversold:
            return {
                "signal_type": "LONG", "leverage": 1,
                "stop_loss": min(prev["low"], entry - 1.5 * last["_atr_14"]),
                "take_profit_1": last["_bb_mid"], "take_profit_2": last["_bb_upper"], "take_profit_3": None,
            }
        if prev["close"] > prev["_bb_upper"] and entry <= last["_bb_upper"] and prev["_rsi_14"] > rsi_overbought:
            return {
                "signal_type": "SHORT", "leverage": 1,
                "stop_loss": max(prev["high"], entry + 1.5 * last["_atr_14"]),
                "take_profit_1": last["_bb_mid"], "take_profit_2": last["_bb_lower"], "take_profit_3": None,
            }
        return None

    return _signal


# ---------------------------------------------------------------------------
# S06 -- Supertrend + ATR (4h)
#
# Hypothesis: a Supertrend direction FLIP (the standard ATR-band trend-
# following signal) captures the start of a new trend leg; ATR itself sizes
# the stop.
# ---------------------------------------------------------------------------

def precompute_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    df = df.copy()
    line, direction = supertrend(df["high"], df["low"], df["close"], period, multiplier)
    df["_st_line"] = line
    df["_st_dir"] = direction
    df["_atr_14"] = atr(df["high"], df["low"], df["close"], 14)
    return df


def supertrend_signal_func():
    def _signal(df: pd.DataFrame) -> dict | None:
        if len(df) < 15:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        if pd.isna(last["_st_dir"]) or pd.isna(prev["_st_dir"]) or pd.isna(last["_atr_14"]) or last["_atr_14"] <= 0:
            return None

        entry = last["close"]
        if prev["_st_dir"] == -1 and last["_st_dir"] == 1:
            return {
                "signal_type": "LONG", "leverage": 1,
                "stop_loss": last["_st_line"],
                "take_profit_1": entry + 1.5 * last["_atr_14"],
                "take_profit_2": entry + 2.5 * last["_atr_14"],
                "take_profit_3": entry + 4.0 * last["_atr_14"],
            }
        if prev["_st_dir"] == 1 and last["_st_dir"] == -1:
            return {
                "signal_type": "SHORT", "leverage": 1,
                "stop_loss": last["_st_line"],
                "take_profit_1": entry - 1.5 * last["_atr_14"],
                "take_profit_2": entry - 2.5 * last["_atr_14"],
                "take_profit_3": entry - 4.0 * last["_atr_14"],
            }
        return None

    return _signal


# ---------------------------------------------------------------------------
# S07 -- MACD Trend Momentum (4h)
#
# Hypothesis: a MACD line/signal-line crossover, taken only in the
# direction of the prevailing EMA50/EMA200 trend, filters out most
# counter-trend MACD whipsaws.
# ---------------------------------------------------------------------------

def precompute_macd_momentum(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    macd_line, signal_line, hist = macd(df["close"], 12, 26, 9)
    df["_macd"] = macd_line
    df["_macd_signal"] = signal_line
    df["_macd_hist"] = hist
    df["_ema_50"] = ema(df["close"], 50)
    df["_ema_200"] = ema(df["close"], 200)
    df["_atr_14"] = atr(df["high"], df["low"], df["close"], 14)
    return df


def macd_momentum_signal_func():
    def _signal(df: pd.DataFrame) -> dict | None:
        if len(df) < 210:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        cols = ("_macd", "_macd_signal", "_ema_50", "_ema_200", "_atr_14")
        if any(pd.isna(last[c]) or pd.isna(prev[c]) for c in cols) or last["_atr_14"] <= 0:
            return None

        entry = last["close"]
        bullish_trend = last["_ema_50"] > last["_ema_200"]
        bearish_trend = last["_ema_50"] < last["_ema_200"]
        crossed_up = prev["_macd"] <= prev["_macd_signal"] and last["_macd"] > last["_macd_signal"]
        crossed_down = prev["_macd"] >= prev["_macd_signal"] and last["_macd"] < last["_macd_signal"]

        if bullish_trend and crossed_up:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG")}
        if bearish_trend and crossed_down:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT")}
        return None

    return _signal


# ---------------------------------------------------------------------------
# S08 -- EMA Structure + ADX (4h)
#
# Hypothesis: a fully STACKED EMA structure (9 > 20 > 50, or the mirror)
# combined with ADX confirming trend strength describes a healthier trend
# than a single moving-average cross; entry on a pullback to the middle
# EMA avoids chasing. Deliberately NOT a channel breakout (that's S05).
# ---------------------------------------------------------------------------

def precompute_ema_structure(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["_ema_9"] = ema(df["close"], 9)
    df["_ema_20"] = ema(df["close"], 20)
    df["_ema_50"] = ema(df["close"], 50)
    df["_adx_14"] = adx(df["high"], df["low"], df["close"], 14)
    df["_atr_14"] = atr(df["high"], df["low"], df["close"], 14)
    return df


def ema_structure_adx_signal_func(adx_threshold: float = 20):
    def _signal(df: pd.DataFrame) -> dict | None:
        if len(df) < 55:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        cols = ("_ema_9", "_ema_20", "_ema_50", "_adx_14", "_atr_14")
        if any(pd.isna(last[c]) or pd.isna(prev[c]) for c in cols) or last["_atr_14"] <= 0:
            return None
        if last["_adx_14"] < adx_threshold:
            return None

        entry = last["close"]
        bullish_stack = last["_ema_9"] > last["_ema_20"] > last["_ema_50"]
        bearish_stack = last["_ema_9"] < last["_ema_20"] < last["_ema_50"]

        pulled_back_up = prev["low"] <= prev["_ema_20"] and entry > last["_ema_20"]
        if bullish_stack and pulled_back_up:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG")}

        pulled_back_down = prev["high"] >= prev["_ema_20"] and entry < last["_ema_20"]
        if bearish_stack and pulled_back_down:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT")}
        return None

    return _signal


# ---------------------------------------------------------------------------
# S09 -- ATR Volatility Breakout (4h)
#
# Hypothesis: a volatility CONTRACTION (Bollinger-width squeeze relative to
# its own recent history) followed by a directional break of the bands with
# ATR simultaneously expanding marks the start of a new regime -- distinct
# from S01 (15m, breakout of a plain price range + volume) and from S05
# (Donchian channel breakout with no volatility-squeeze precondition).
# ---------------------------------------------------------------------------

def precompute_atr_volatility_breakout(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    upper, mid, lower = bollinger_bands(df["close"], 20, 2.0)
    df["_bb_upper"] = upper
    df["_bb_lower"] = lower
    df["_bb_width"] = (upper - lower) / mid.replace(0, np.nan)
    df["_bb_width_pctile"] = df["_bb_width"].rolling(50).rank(pct=True)
    df["_atr_14"] = atr(df["high"], df["low"], df["close"], 14)
    return df


def atr_volatility_breakout_signal_func(squeeze_pctile: float = 0.25, atr_lookback: int = 3):
    def _signal(df: pd.DataFrame) -> dict | None:
        if len(df) < 55:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        cols = ("_bb_upper", "_bb_lower", "_bb_width_pctile", "_atr_14")
        if any(pd.isna(last[c]) or pd.isna(prev[c]) for c in cols) or last["_atr_14"] <= 0:
            return None
        atr_series = df["_atr_14"]
        if len(atr_series) < atr_lookback + 1 or pd.isna(atr_series.iloc[-1 - atr_lookback]):
            return None

        was_squeezed = prev["_bb_width_pctile"] <= squeeze_pctile
        expanding = last["_atr_14"] > atr_series.iloc[-1 - atr_lookback]
        if not (was_squeezed and expanding):
            return None

        entry = last["close"]
        if entry > last["_bb_upper"]:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG")}
        if entry < last["_bb_lower"]:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT")}
        return None

    return _signal


# ---------------------------------------------------------------------------
# S10 -- Multi-Timeframe Trend Confirmation (4h setup + 1d filter)
#
# Hypothesis: a 4h Donchian breakout taken ONLY in the direction of the
# already-established daily trend (price above/below a 1d EMA) should see
# fewer false breakouts than the same 4h setup with no higher-timeframe
# filter -- this is what actually distinguishes it from S05.
#
# Genuine multi-timeframe construction: the 1d trend flag is computed once
# on the real 1d series, then merge_asof'd onto the 4h index using ONLY 1d
# bars that closed strictly before each 4h bar's timestamp (direction=
# "backward", exclusive) -- never a same-day or future daily bar. See
# `attach_daily_trend_filter` for the no-lookahead mechanics.
# ---------------------------------------------------------------------------

def attach_daily_trend_filter(df_4h: pd.DataFrame, df_1d: pd.DataFrame, ema_period: int = 50) -> pd.DataFrame:
    daily = df_1d[["timestamp", "close"]].copy().sort_values("timestamp").reset_index(drop=True)
    daily["_daily_ema"] = ema(daily["close"], ema_period)
    # A 1d bar's own close/EMA is only "known" once that day has actually
    # closed -- shift by one full day so a 4h bar can never see the daily
    # bar it is itself still inside of.
    daily["_daily_trend_up"] = (daily["close"] > daily["_daily_ema"]).shift(1)
    daily_lookup = daily[["timestamp", "_daily_trend_up"]].dropna().sort_values("timestamp")

    df = df_4h.sort_values("timestamp").reset_index(drop=True).copy()
    merged = pd.merge_asof(
        df, daily_lookup, on="timestamp", direction="backward", allow_exact_matches=False,
    )
    df["_daily_trend_up"] = merged["_daily_trend_up"]
    return df


def precompute_mtf_trend(df_4h: pd.DataFrame, df_1d: pd.DataFrame, breakout_period: int = 20) -> pd.DataFrame:
    df = attach_daily_trend_filter(df_4h, df_1d, 50)
    df["_donchian_high"] = df["high"].rolling(breakout_period).max()
    df["_donchian_low"] = df["low"].rolling(breakout_period).min()
    df["_adx_14"] = adx(df["high"], df["low"], df["close"], 14)
    df["_atr_14"] = atr(df["high"], df["low"], df["close"], 14)
    return df


def mtf_trend_signal_func(breakout_period: int = 20, adx_threshold: float = 20):
    def _signal(df: pd.DataFrame) -> dict | None:
        if len(df) < breakout_period + 20:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        cols = ("_donchian_high", "_donchian_low", "_adx_14", "_atr_14", "_daily_trend_up")
        if any(pd.isna(last[c]) for c in ("_adx_14", "_atr_14")) or last["_atr_14"] <= 0:
            return None
        if pd.isna(prev["_donchian_high"]) or pd.isna(prev["_donchian_low"]) or pd.isna(last["_daily_trend_up"]):
            return None
        if last["_adx_14"] < adx_threshold:
            return None

        entry = last["close"]
        if bool(last["_daily_trend_up"]) and entry > prev["_donchian_high"]:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG")}
        if not bool(last["_daily_trend_up"]) and entry < prev["_donchian_low"]:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT")}
        return None

    return _signal


# ---------------------------------------------------------------------------
# S11 -- Z-Score Mean Reversion (15m)
#
# Hypothesis: price deviating an unusual number of standard deviations from
# its own recent rolling mean (a pure statistical extreme, no session
# anchoring) tends to revert once the deviation itself starts shrinking.
# Deliberately distinct from S03 (VWAP Mean Reversion): S03 measures
# distance from a volume-weighted, UTC-day-anchored price; this measures
# distance from a plain rolling price mean, in standard-deviation units,
# with no session dependency and no volume weighting at all -- a genuinely
# different statistical construction of "how far is price from normal."
# ---------------------------------------------------------------------------

def precompute_zscore_reversion(df: pd.DataFrame, period: int = 50) -> pd.DataFrame:
    df = df.copy()
    roll_mean = df["close"].rolling(period).mean()
    roll_std = df["close"].rolling(period).std()
    df["_zscore"] = (df["close"] - roll_mean) / roll_std.replace(0, np.nan)
    df["_atr_14"] = atr(df["high"], df["low"], df["close"], 14)
    return df


def zscore_reversion_signal_func(z_entry: float = 2.0):
    def _signal(df: pd.DataFrame) -> dict | None:
        if len(df) < 55:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        if pd.isna(last["_zscore"]) or pd.isna(prev["_zscore"]) or pd.isna(last["_atr_14"]) or last["_atr_14"] <= 0:
            return None

        entry = last["close"]
        # Extreme oversold AND the deviation is already shrinking (z moving
        # back toward zero) -- never enters while still extending further.
        if prev["_zscore"] < -z_entry and last["_zscore"] > prev["_zscore"]:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG", sl_mult=1.5, tp_mults=(1.0, 2.0, 3.0))}
        if prev["_zscore"] > z_entry and last["_zscore"] < prev["_zscore"]:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT", sl_mult=1.5, tp_mults=(1.0, 2.0, 3.0))}
        return None

    return _signal


# ---------------------------------------------------------------------------
# S12 -- Structure Breakout + Retest (4h)
#
# Hypothesis: a level that price has already broken through and is now
# retesting from the far side (without invalidating it) is a lower-risk
# continuation entry than the moment of the original breakout -- distinct
# from S05 (enters AT the Donchian breakout itself) and S06 (a trailing
# ATR-band flip, no notion of a specific broken level being retested).
# ---------------------------------------------------------------------------

def precompute_structure_retest(df: pd.DataFrame, swing_period: int = 20) -> pd.DataFrame:
    df = df.copy()
    df["_swing_high"] = df["high"].rolling(swing_period).max()
    df["_swing_low"] = df["low"].rolling(swing_period).min()
    df["_atr_14"] = atr(df["high"], df["low"], df["close"], 14)
    return df


def structure_retest_signal_func(lookback_break: int = 8, retest_atr_mult: float = 0.75):
    def _signal(df: pd.DataFrame) -> dict | None:
        if len(df) < lookback_break + 30:
            return None
        last = df.iloc[-1]
        if pd.isna(last["_atr_14"]) or last["_atr_14"] <= 0:
            return None

        # The resistance/support level being tested is the swing extreme
        # from BEFORE the breakout window -- never the swing value computed
        # using bars inside the window itself (that would just describe the
        # breakout bar's own high/low, not a real prior level).
        level_idx = -(lookback_break + 2)
        if len(df) < abs(level_idx) + 1:
            return None
        prior_swing_high = df["_swing_high"].iloc[level_idx]
        prior_swing_low = df["_swing_low"].iloc[level_idx]
        if pd.isna(prior_swing_high) or pd.isna(prior_swing_low):
            return None

        window = df.iloc[-(lookback_break + 1):-1]
        entry = last["close"]
        atr_val = last["_atr_14"]

        broke_up = (window["close"] > prior_swing_high).any()
        retesting_from_above = 0 <= (entry - prior_swing_high) <= retest_atr_mult * atr_val
        if broke_up and retesting_from_above:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, atr_val, "LONG")}

        broke_down = (window["close"] < prior_swing_low).any()
        retesting_from_below = 0 <= (prior_swing_low - entry) <= retest_atr_mult * atr_val
        if broke_down and retesting_from_below:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, atr_val, "SHORT")}
        return None

    return _signal


# ---------------------------------------------------------------------------
# Registry -- mirrors ml/evaluation/baselines.py's BASELINE_STRATEGIES shape.
# S05 (existing Donchian+ADX) is deliberately NOT here: it is never
# re-implemented, only ever reused via services.signal_engine.strategy.
# BaselineStrategy / ml.evaluation.baselines.trend_following_signal_func.
# ---------------------------------------------------------------------------

MULTI_STRATEGIES: dict[str, dict] = {
    "S01_MOMENTUM_BREAKOUT_15M": {
        "display_name": "Momentum / Volatility Breakout",
        "timeframe": "15m",
        "precompute": precompute_momentum_breakout,
        "factory": lambda: momentum_breakout_signal_func(20, 1.5, 5),
        "data_mode": "CLOSED_CANDLE",
    },
    "S02_EMA_PULLBACK_15M": {
        "display_name": "EMA Pullback / Trend Continuation",
        "timeframe": "15m",
        "precompute": precompute_ema_pullback,
        "factory": lambda: ema_pullback_signal_func(40, 60),
        "data_mode": "CLOSED_CANDLE",
    },
    "S03_VWAP_REVERSION_15M": {
        "display_name": "VWAP Mean Reversion",
        "timeframe": "15m",
        "precompute": precompute_vwap_reversion,
        "factory": lambda: vwap_reversion_signal_func(2.5),
        "data_mode": "CLOSED_CANDLE",
    },
    "S04_RSI_BB_15M": {
        "display_name": "RSI + Bollinger Momentum/Reversal",
        "timeframe": "15m",
        "precompute": precompute_rsi_bollinger,
        "factory": lambda: rsi_bollinger_signal_func(30, 70),
        "data_mode": "CLOSED_CANDLE",
    },
    "S06_SUPERTREND_ATR_4H": {
        "display_name": "Supertrend + ATR",
        "timeframe": "4h",
        "precompute": precompute_supertrend,
        "factory": supertrend_signal_func,
        "data_mode": "CLOSED_CANDLE",
    },
    "S07_MACD_MOMENTUM_4H": {
        "display_name": "MACD Trend Momentum",
        "timeframe": "4h",
        "precompute": precompute_macd_momentum,
        "factory": macd_momentum_signal_func,
        "data_mode": "CLOSED_CANDLE",
    },
    "S08_EMA_ADX_4H": {
        "display_name": "EMA Structure + ADX",
        "timeframe": "4h",
        "precompute": precompute_ema_structure,
        "factory": lambda: ema_structure_adx_signal_func(20),
        "data_mode": "CLOSED_CANDLE",
    },
    "S09_ATR_BREAKOUT_4H": {
        "display_name": "ATR Volatility Breakout",
        "timeframe": "4h",
        "precompute": precompute_atr_volatility_breakout,
        "factory": lambda: atr_volatility_breakout_signal_func(0.25, 3),
        "data_mode": "CLOSED_CANDLE",
    },
    "S10_MTF_TREND_4H": {
        "display_name": "Multi-Timeframe Trend Confirmation",
        "timeframe": "4h",
        "precompute": None,  # needs both df_4h and df_1d -- see precompute_mtf_trend
        "factory": lambda: mtf_trend_signal_func(20, 20),
        "data_mode": "CLOSED_CANDLE",
    },
    "S11_ZSCORE_REVERSION_15M": {
        "display_name": "Z-Score Mean Reversion",
        "timeframe": "15m",
        "precompute": precompute_zscore_reversion,
        "factory": lambda: zscore_reversion_signal_func(2.0),
        "data_mode": "CLOSED_CANDLE",
    },
    "S12_STRUCTURE_RETEST_4H": {
        "display_name": "Structure Breakout + Retest",
        "timeframe": "4h",
        "precompute": precompute_structure_retest,
        "factory": lambda: structure_retest_signal_func(8, 0.75),
        "data_mode": "CLOSED_CANDLE",
    },
}
