"""V3 broad candidate discovery for BTC/USDT futures strategies.

Two stages:

  STAGE 1 -- cheap preliminary screen. Every candidate below (23 real,
  backtestable mechanisms, deliberately diverse across trend/breakout/
  momentum/pullback/mean-reversion/market-structure/multi-timeframe/
  funding families) is backtested ONCE over the full historical dataset
  with one reasonable, non-tuned parameter set. This is a coarse filter,
  not a profitability claim -- see Phase 6 of the task this was written
  for. Obvious failures (PF far below 1, catastrophic drawdown, near-zero
  trades) are cut here.

  STAGE 2 -- only candidates that survive Stage 1 go through the full
  scripts/research_v2_rigorous.py-style rigor: chronological train/val/
  OOS split, parameters frozen on VALIDATION only, OOS walk-forward,
  long/short breakdown, regime attribution, parameter sensitivity, cost-
  robustness stress test, and a trade-order bootstrap.

Real data only (alphaone_research.db). No synthetic price series, no
fabricated derivatives data. Funding-rate strategies use the REAL
funding_rates table (3,285 rows, full ~3-year coverage, confirmed this
session). Open-interest and liquidation strategies are NOT implemented
here -- open_interest has only ~1 month of real history (a documented
Binance API limitation, see docs/known_limitations.md) and liquidations
has zero historical rows -- both genuinely insufficient for a
train/val/OOS backtest, so they are reported DATA_UNAVAILABLE rather
than backtested on a fabricated or absurdly short sample.
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.backtester.engine import Backtester, BacktestConfig
from services.feature_engine.indicators import ema, sma, wma, hma, kama, rsi, atr, adx, macd, bollinger_bands, roc, momentum

DB_PATH = str(Path(__file__).resolve().parent.parent / "alphaone_research.db")


def load_candles(conn, timeframe: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT timestamp, open, high, low, close, volume FROM candles "
        "WHERE symbol = 'BTC/USDT' AND timeframe = ? ORDER BY timestamp ASC",
        conn, params=(timeframe,), parse_dates=["timestamp"],
    )


def load_funding(conn) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT timestamp, rate FROM funding_rates WHERE symbol = 'BTC/USDT' ORDER BY timestamp ASC",
        conn, parse_dates=["timestamp"],
    )


def _atr_sl_tp(entry, atr_val, side, sl_mult=2.0, tp_mults=(1.5, 2.5, 3.5)):
    if side == "LONG":
        return {"stop_loss": entry - sl_mult * atr_val, "take_profit_1": entry + tp_mults[0] * atr_val,
                "take_profit_2": entry + tp_mults[1] * atr_val, "take_profit_3": entry + tp_mults[2] * atr_val}
    return {"stop_loss": entry + sl_mult * atr_val, "take_profit_1": entry - tp_mults[0] * atr_val,
            "take_profit_2": entry - tp_mults[1] * atr_val, "take_profit_3": entry - tp_mults[2] * atr_val}


def attach_funding(df: pd.DataFrame, funding: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("timestamp").reset_index(drop=True).copy()
    f = funding.sort_values("timestamp").reset_index(drop=True)
    merged = pd.merge_asof(df, f, on="timestamp", direction="backward", allow_exact_matches=True)
    df["_funding_rate"] = merged["rate"]
    return df


# ============================================================
# CANDIDATE DEFINITIONS -- (precompute_fn, signal_func_factory, mechanism, needs_funding)
# ============================================================

def precompute_common(df):
    df = df.copy()
    df["_atr_14"] = atr(df["high"], df["low"], df["close"], 14)
    return df


# ---- 1/14: KAMA Trend ----
def precompute_kama(df, period=10):
    df = precompute_common(df)
    df["_kama"] = kama(df["close"], period, 2, 30)
    return df

def kama_signal(flat_bars=3):
    def _s(df):
        if len(df) < 40:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        if pd.isna(last["_kama"]) or pd.isna(prev["_kama"]) or pd.isna(last["_atr_14"]) or last["_atr_14"] <= 0:
            return None
        entry = last["close"]
        kama_slope = last["_kama"] - df["_kama"].iloc[-1 - flat_bars]
        crossed_up = prev["close"] <= prev["_kama"] and entry > last["_kama"] and kama_slope > 0
        crossed_down = prev["close"] >= prev["_kama"] and entry < last["_kama"] and kama_slope < 0
        if crossed_up:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG")}
        if crossed_down:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT")}
        return None
    return _s


# ---- 2/15: HMA Trend ----
def precompute_hma(df, period=20):
    df = precompute_common(df)
    df["_hma"] = hma(df["close"], period)
    return df

def hma_signal():
    def _s(df):
        if len(df) < 40:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        if pd.isna(last["_hma"]) or pd.isna(prev["_hma"]) or pd.isna(last["_atr_14"]) or last["_atr_14"] <= 0:
            return None
        entry = last["close"]
        if prev["_hma"] <= prev.get("_hma_prev2", prev["_hma"]):
            pass
        hma_rising = last["_hma"] > prev["_hma"]
        crossed_up = prev["close"] <= prev["_hma"] and entry > last["_hma"] and hma_rising
        crossed_down = prev["close"] >= prev["_hma"] and entry < last["_hma"] and not hma_rising
        if crossed_up:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG")}
        if crossed_down:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT")}
        return None
    return _s


# ---- 3/16: Chandelier Exit trend flip ----
def precompute_chandelier(df, period=22, mult=3.0):
    df = precompute_common(df)
    df["_ch_long"] = df["high"].rolling(period).max() - mult * df["_atr_14"]
    df["_ch_short"] = df["low"].rolling(period).min() + mult * df["_atr_14"]
    return df

def chandelier_signal():
    def _s(df):
        if len(df) < 30:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        cols = ("_ch_long", "_ch_short", "_atr_14")
        if any(pd.isna(last[c]) or pd.isna(prev[c]) for c in cols) or last["_atr_14"] <= 0:
            return None
        entry = last["close"]
        # Flip long: price crosses above the short-exit line (was in a downtrend, now breaking out up)
        if prev["close"] <= prev["_ch_short"] and entry > last["_ch_short"]:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG")}
        if prev["close"] >= prev["_ch_long"] and entry < last["_ch_long"]:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT")}
        return None
    return _s


# ---- 4/18: Momentum Acceleration (2nd derivative of ROC) ----
def precompute_momentum_accel(df, roc_period=10):
    df = precompute_common(df)
    df["_roc"] = roc(df["close"], roc_period)
    df["_roc_accel"] = df["_roc"].diff(3)
    return df

def momentum_accel_signal(accel_threshold=0.5):
    def _s(df):
        if len(df) < 30:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        if pd.isna(last["_roc_accel"]) or pd.isna(last["_roc"]) or pd.isna(last["_atr_14"]) or last["_atr_14"] <= 0:
            return None
        entry = last["close"]
        if last["_roc"] > 0 and last["_roc_accel"] > accel_threshold and prev["_roc_accel"] <= accel_threshold:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG")}
        if last["_roc"] < 0 and last["_roc_accel"] < -accel_threshold and prev["_roc_accel"] >= -accel_threshold:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT")}
        return None
    return _s


# ---- 5/19: Multi-period momentum consensus ----
def precompute_multi_momentum(df):
    df = precompute_common(df)
    df["_roc5"] = roc(df["close"], 5)
    df["_roc10"] = roc(df["close"], 10)
    df["_roc20"] = roc(df["close"], 20)
    return df

def multi_momentum_signal():
    def _s(df):
        if len(df) < 30:
            return None
        last = df.iloc[-1]
        cols = ("_roc5", "_roc10", "_roc20", "_atr_14")
        if any(pd.isna(last[c]) for c in cols) or last["_atr_14"] <= 0:
            return None
        entry = last["close"]
        if last["_roc5"] > 0 and last["_roc10"] > 0 and last["_roc20"] > 0:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG")}
        if last["_roc5"] < 0 and last["_roc10"] < 0 and last["_roc20"] < 0:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT")}
        return None
    return _s


# ---- 6: Volatility-adjusted momentum ----
def precompute_vol_adj_momentum(df, period=10):
    df = precompute_common(df)
    df["_mom"] = momentum(df["close"], period)
    df["_vol_adj_mom"] = df["_mom"] / df["_atr_14"].replace(0, np.nan)
    return df

def vol_adj_momentum_signal(threshold=1.5):
    def _s(df):
        if len(df) < 30:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        if pd.isna(last["_vol_adj_mom"]) or pd.isna(prev["_vol_adj_mom"]) or pd.isna(last["_atr_14"]) or last["_atr_14"] <= 0:
            return None
        entry = last["close"]
        if prev["_vol_adj_mom"] <= threshold and last["_vol_adj_mom"] > threshold:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG")}
        if prev["_vol_adj_mom"] >= -threshold and last["_vol_adj_mom"] < -threshold:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT")}
        return None
    return _s


# ---- 7: Compression -> Expansion breakout (BB width + true range, distinct from S09) ----
def precompute_compression_expansion(df, bb_period=20):
    df = precompute_common(df)
    upper, mid, lower = bollinger_bands(df["close"], bb_period, 2.0)
    df["_bb_upper"] = upper
    df["_bb_lower"] = lower
    bb_width = (upper - lower) / mid.replace(0, np.nan)
    df["_bb_width_pctile"] = bb_width.rolling(60).rank(pct=True)
    prev_close = df["close"].shift(1)
    df["_true_range"] = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    df["_tr_ratio"] = df["_true_range"] / df["_true_range"].rolling(20).mean().replace(0, np.nan)
    return df

def compression_expansion_signal(squeeze_pctile=0.2, tr_ratio_mult=1.5):
    def _s(df):
        if len(df) < 65:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        cols = ("_bb_width_pctile", "_tr_ratio", "_bb_upper", "_bb_lower", "_atr_14")
        if any(pd.isna(last[c]) or (c == "_bb_width_pctile" and pd.isna(prev[c])) for c in cols) or last["_atr_14"] <= 0:
            return None
        entry = last["close"]
        was_squeezed = prev["_bb_width_pctile"] <= squeeze_pctile
        expanding = last["_tr_ratio"] >= tr_ratio_mult
        if not (was_squeezed and expanding):
            return None
        if entry > last["_bb_upper"]:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG")}
        if entry < last["_bb_lower"]:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT")}
        return None
    return _s


# ---- 8: Range expansion breakout (True range vs MA, breaks N-bar range) ----
def precompute_range_expansion(df, range_period=20):
    df = precompute_common(df)
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    df["_tr_ratio"] = tr / tr.rolling(20).mean().replace(0, np.nan)
    df["_range_high"] = df["high"].rolling(range_period).max()
    df["_range_low"] = df["low"].rolling(range_period).min()
    return df

def range_expansion_signal(tr_ratio_mult=1.75):
    def _s(df):
        if len(df) < 30:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        cols = ("_tr_ratio", "_range_high", "_range_low", "_atr_14")
        if any(pd.isna(last[c]) or (c in ("_range_high", "_range_low") and pd.isna(prev[c])) for c in cols) or last["_atr_14"] <= 0:
            return None
        entry = last["close"]
        if last["_tr_ratio"] < tr_ratio_mult:
            return None
        if entry > prev["_range_high"]:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG")}
        if entry < prev["_range_low"]:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT")}
        return None
    return _s


# ---- 9: Breakout-Pullback Continuation ----
def precompute_breakout_pullback(df, breakout_period=20):
    df = precompute_common(df)
    df["_range_high"] = df["high"].rolling(breakout_period).max()
    df["_range_low"] = df["low"].rolling(breakout_period).min()
    df["_ema_9"] = ema(df["close"], 9)
    return df

def breakout_pullback_signal(lookback=6):
    def _s(df):
        if len(df) < 40:
            return None
        last = df.iloc[-1]
        if pd.isna(last["_atr_14"]) or last["_atr_14"] <= 0 or pd.isna(last["_ema_9"]):
            return None
        level_idx = -(lookback + 2)
        if len(df) < abs(level_idx) + 1:
            return None
        prior_high = df["_range_high"].iloc[level_idx]
        prior_low = df["_range_low"].iloc[level_idx]
        if pd.isna(prior_high) or pd.isna(prior_low):
            return None
        window = df.iloc[-(lookback + 1):-1]
        entry = last["close"]
        broke_up = (window["close"] > prior_high).any()
        pulled_back_to_ema_up = window["low"].min() <= window["_ema_9"].min() if not window.empty else False
        if broke_up and entry > last["_ema_9"] and entry > prior_high:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG")}
        broke_down = (window["close"] < prior_low).any()
        if broke_down and entry < last["_ema_9"] and entry < prior_low:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT")}
        return None
    return _s


# ---- 10: Pure Bollinger reversion (no RSI filter, distinct from S04) ----
def precompute_pure_bb(df, period=20):
    df = precompute_common(df)
    upper, mid, lower = bollinger_bands(df["close"], period, 2.0)
    df["_bb_upper"] = upper
    df["_bb_mid"] = mid
    df["_bb_lower"] = lower
    return df

def pure_bb_signal():
    def _s(df):
        if len(df) < 25:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        cols = ("_bb_upper", "_bb_lower", "_bb_mid", "_atr_14")
        if any(pd.isna(last[c]) or pd.isna(prev[c]) for c in cols) or last["_atr_14"] <= 0:
            return None
        entry = last["close"]
        if prev["close"] < prev["_bb_lower"] and entry >= last["_bb_lower"]:
            return {"signal_type": "LONG", "leverage": 1, "stop_loss": entry - 1.5 * last["_atr_14"],
                    "take_profit_1": last["_bb_mid"], "take_profit_2": last["_bb_upper"], "take_profit_3": None}
        if prev["close"] > prev["_bb_upper"] and entry <= last["_bb_upper"]:
            return {"signal_type": "SHORT", "leverage": 1, "stop_loss": entry + 1.5 * last["_atr_14"],
                    "take_profit_1": last["_bb_mid"], "take_profit_2": last["_bb_lower"], "take_profit_3": None}
        return None
    return _s


# ---- 11: Volatility-band reversion (std-dev of returns, distinct from Bollinger) ----
def precompute_vol_band_reversion(df, period=30):
    df = precompute_common(df)
    returns = df["close"].pct_change()
    roll_std = returns.rolling(period).std()
    df["_ret_z"] = returns / roll_std.replace(0, np.nan)
    return df

def vol_band_reversion_signal(z_entry=2.5):
    def _s(df):
        if len(df) < 35:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        if pd.isna(last["_ret_z"]) or pd.isna(last["_atr_14"]) or last["_atr_14"] <= 0:
            return None
        entry = last["close"]
        if last["_ret_z"] < -z_entry:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG", sl_mult=1.5, tp_mults=(1.0, 2.0, 3.0))}
        if last["_ret_z"] > z_entry:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT", sl_mult=1.5, tp_mults=(1.0, 2.0, 3.0))}
        return None
    return _s


# ---- 12: Swing structure (fractal) breakout ----
def precompute_swing_structure(df, fractal_width=2):
    df = precompute_common(df)
    n = fractal_width
    is_swing_high = pd.Series(True, index=df.index)
    is_swing_low = pd.Series(True, index=df.index)
    for k in range(1, n + 1):
        is_swing_high &= df["high"] > df["high"].shift(k)
        is_swing_high &= df["high"] > df["high"].shift(-k)
        is_swing_low &= df["low"] < df["low"].shift(k)
        is_swing_low &= df["low"] < df["low"].shift(-k)
    df["_swing_high_val"] = df["high"].where(is_swing_high).ffill()
    df["_swing_low_val"] = df["low"].where(is_swing_low).ffill()
    return df

def swing_structure_signal():
    def _s(df):
        if len(df) < 30:
            return None
        # fractal detection needs `fractal_width` FUTURE bars relative to the
        # fractal point itself, but never relative to "now" -- the ffill()
        # only ever propagates a fractal confirmed at some earlier index j
        # (which needed bars up to j+n, all <= current index i as long as
        # i > j+n, guaranteed since we exclude the last few bars below).
        last = df.iloc[-3]  # never treat the most recent bars as confirmed fractals
        current = df.iloc[-1]
        if pd.isna(last["_swing_high_val"]) or pd.isna(last["_swing_low_val"]) or pd.isna(current["_atr_14"]) or current["_atr_14"] <= 0:
            return None
        entry = current["close"]
        if entry > last["_swing_high_val"]:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, current["_atr_14"], "LONG")}
        if entry < last["_swing_low_val"]:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, current["_atr_14"], "SHORT")}
        return None
    return _s


# ---- 13: HTF trend + LTF pullback (15m entries filtered by 1h trend) ----
def attach_htf_trend(df_ltf: pd.DataFrame, df_htf: pd.DataFrame, ema_period=50) -> pd.DataFrame:
    htf = df_htf[["timestamp", "close"]].copy().sort_values("timestamp").reset_index(drop=True)
    htf["_htf_ema"] = ema(htf["close"], ema_period)
    htf["_htf_trend_up"] = (htf["close"] > htf["_htf_ema"]).shift(1)  # only a CLOSED htf bar's trend is known
    lookup = htf[["timestamp", "_htf_trend_up"]].dropna().sort_values("timestamp")
    df = df_ltf.sort_values("timestamp").reset_index(drop=True).copy()
    merged = pd.merge_asof(df, lookup, on="timestamp", direction="backward", allow_exact_matches=False)
    df["_htf_trend_up"] = merged["_htf_trend_up"]
    return df

def precompute_htf_pullback(df_ltf, df_htf):
    df = attach_htf_trend(df_ltf, df_htf, 50)
    df = precompute_common(df)
    df["_ema_20"] = ema(df["close"], 20)
    df["_rsi_14"] = rsi(df["close"], 14)
    return df

def htf_pullback_signal(rsi_floor=40):
    def _s(df):
        if len(df) < 55:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        cols = ("_htf_trend_up", "_ema_20", "_atr_14", "_rsi_14")
        if any(pd.isna(last[c]) or pd.isna(prev[c]) for c in cols if c != "_htf_trend_up") or pd.isna(last["_htf_trend_up"]) or last["_atr_14"] <= 0:
            return None
        entry = last["close"]
        touched_up = prev["low"] <= prev["_ema_20"] and entry > last["_ema_20"]
        if bool(last["_htf_trend_up"]) and touched_up and last["_rsi_14"] > rsi_floor:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG")}
        touched_down = prev["high"] >= prev["_ema_20"] and entry < last["_ema_20"]
        if not bool(last["_htf_trend_up"]) and touched_down and last["_rsi_14"] < (100 - rsi_floor):
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT")}
        return None
    return _s


# ---- 14: HTF volatility regime + momentum entry (4h entries filtered by 1d vol regime) ----
def attach_htf_vol_regime(df_ltf, df_htf, atr_period=14):
    htf = df_htf[["timestamp", "high", "low", "close"]].copy().sort_values("timestamp").reset_index(drop=True)
    htf_atr = atr(htf["high"], htf["low"], htf["close"], atr_period)
    htf_atr_norm = htf_atr / htf["close"]
    htf["_htf_high_vol"] = (htf_atr_norm > htf_atr_norm.rolling(60, min_periods=20).median()).shift(1)
    lookup = htf[["timestamp", "_htf_high_vol"]].dropna().sort_values("timestamp")
    df = df_ltf.sort_values("timestamp").reset_index(drop=True).copy()
    merged = pd.merge_asof(df, lookup, on="timestamp", direction="backward", allow_exact_matches=False)
    df["_htf_high_vol"] = merged["_htf_high_vol"]
    return df

def precompute_htf_vol_momentum(df_4h, df_1d):
    df = attach_htf_vol_regime(df_4h, df_1d, 14)
    df = precompute_common(df)
    df["_roc10"] = roc(df["close"], 10)
    return df

def htf_vol_momentum_signal(roc_threshold=2.0):
    def _s(df):
        if len(df) < 30:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        if pd.isna(last["_htf_high_vol"]) or pd.isna(last["_roc10"]) or pd.isna(last["_atr_14"]) or last["_atr_14"] <= 0:
            return None
        if not bool(last["_htf_high_vol"]):
            return None  # only trade when the DAILY regime is genuinely high-volatility
        entry = last["close"]
        if prev["_roc10"] <= roc_threshold and last["_roc10"] > roc_threshold:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG")}
        if prev["_roc10"] >= -roc_threshold and last["_roc10"] < -roc_threshold:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT")}
        return None
    return _s


# ---- 15: Funding-extreme mean reversion ----
def precompute_funding_reversion(df, funding):
    df = precompute_common(df)
    df = attach_funding(df, funding)
    df["_funding_pctile"] = df["_funding_rate"].rolling(200, min_periods=50).rank(pct=True)
    return df

def funding_reversion_signal(high_pctile=0.9, low_pctile=0.1):
    def _s(df):
        if len(df) < 60:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        if pd.isna(last["_funding_pctile"]) or pd.isna(last["_atr_14"]) or last["_atr_14"] <= 0:
            return None
        entry = last["close"]
        # Funding at a historical extreme HIGH (longs paying a lot) -> bet
        # on a reversion DOWN (crowded long unwind). Extreme LOW -> bet UP.
        if prev["_funding_pctile"] >= high_pctile and last["_funding_pctile"] < high_pctile:
            return {"signal_type": "SHORT", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "SHORT", sl_mult=1.5, tp_mults=(1.0, 2.0, 3.0))}
        if prev["_funding_pctile"] <= low_pctile and last["_funding_pctile"] > low_pctile:
            return {"signal_type": "LONG", "leverage": 1, **_atr_sl_tp(entry, last["_atr_14"], "LONG", sl_mult=1.5, tp_mults=(1.0, 2.0, 3.0))}
        return None
    return _s


# ============================================================
# Registry of candidates for Stage 1 screening
# ============================================================

CANDIDATES_15M = {
    "V3_KAMA_TREND_15M": ("KAMA adaptive trend", precompute_kama, kama_signal),
    "V3_HMA_TREND_15M": ("Hull MA trend", precompute_hma, hma_signal),
    "V3_CHANDELIER_15M": ("Chandelier Exit trend flip", precompute_chandelier, chandelier_signal),
    "V3_MOMENTUM_ACCEL_15M": ("Momentum acceleration", precompute_momentum_accel, momentum_accel_signal),
    "V3_MULTI_MOMENTUM_15M": ("Multi-period momentum consensus", precompute_multi_momentum, multi_momentum_signal),
    "V3_VOL_ADJ_MOMENTUM_15M": ("Volatility-adjusted momentum", precompute_vol_adj_momentum, vol_adj_momentum_signal),
    "V3_COMPRESSION_EXPANSION_15M": ("Compression->expansion breakout", precompute_compression_expansion, compression_expansion_signal),
    "V3_RANGE_EXPANSION_15M": ("Range expansion breakout", precompute_range_expansion, range_expansion_signal),
    "V3_BREAKOUT_PULLBACK_15M": ("Breakout-pullback continuation", precompute_breakout_pullback, breakout_pullback_signal),
    "V3_PURE_BB_REVERSION_15M": ("Pure Bollinger reversion", precompute_pure_bb, pure_bb_signal),
    "V3_VOL_BAND_REVERSION_15M": ("Volatility-band (return z-score) reversion", precompute_vol_band_reversion, vol_band_reversion_signal),
    "V3_SWING_STRUCTURE_15M": ("Fractal swing structure breakout", precompute_swing_structure, swing_structure_signal),
    "V3_FUNDING_REVERSION_15M": ("Funding-rate extreme reversion", precompute_funding_reversion, funding_reversion_signal),
}

CANDIDATES_4H = {
    "V3_KAMA_TREND_4H": ("KAMA adaptive trend", precompute_kama, kama_signal),
    "V3_HMA_TREND_4H": ("Hull MA trend", precompute_hma, hma_signal),
    "V3_CHANDELIER_4H": ("Chandelier Exit trend flip", precompute_chandelier, chandelier_signal),
    "V3_MOMENTUM_ACCEL_4H": ("Momentum acceleration", precompute_momentum_accel, momentum_accel_signal),
    "V3_RANGE_EXPANSION_4H": ("Range expansion breakout", precompute_range_expansion, range_expansion_signal),
    "V3_SWING_STRUCTURE_4H": ("Fractal swing structure breakout", precompute_swing_structure, swing_structure_signal),
    "V3_VOL_BAND_REVERSION_4H": ("Volatility-band (return z-score) reversion", precompute_vol_band_reversion, vol_band_reversion_signal),
    "V3_FUNDING_REVERSION_4H": ("Funding-rate extreme reversion", precompute_funding_reversion, funding_reversion_signal),
}

# Candidates needing a second dataframe (HTF filter / funding) -- handled specially in main()
SPECIAL_15M = ["V3_HTF_PULLBACK_15M"]
SPECIAL_4H = ["V3_HTF_VOL_MOMENTUM_4H"]

DATA_UNAVAILABLE = {
    "OPEN_INTEREST_EXPANSION_BREAKOUT": "open_interest table has only ~1 month of real history (2026-07-27 -> 2026-08-26, 567 rows) -- a documented Binance API limitation (see docs/known_limitations.md). Genuinely insufficient for a train/val/OOS backtest spanning years; not fabricated.",
    "LIQUIDATION_DRIVEN_REVERSAL": "liquidations table has ZERO historical rows (Binance has no public historical-liquidation backfill endpoint, only a best-effort recent snapshot -- see docs/known_limitations.md). Cannot be backtested at all without fabricating data.",
    "PRICE_OI_DIVERGENCE": "depends on the same insufficient open_interest history as above.",
    "FUTURES_SPOT_BASIS_DIVERGENCE": "no separate historical SPOT price series is ingested in this project (services/market_data/binance.py is configured defaultType='future' throughout) -- only the futures series exists historically, so a real futures-vs-spot basis cannot be reconstructed.",
}


def run_screen(name, mechanism, prepared_df, signal_func, config):
    bt = Backtester(config)
    result = bt.run(prepared_df.reset_index(drop=True), signal_func)
    return {
        "name": name, "mechanism": mechanism, "trades": result.total_trades,
        "win_rate": result.win_rate, "pf": result.profit_factor, "return_pct": result.total_pnl_pct,
        "max_dd_pct": result.max_drawdown_pct, "expectancy": result.expectancy,
    }


def main():
    conn = sqlite3.connect(DB_PATH)
    df_15m = load_candles(conn, "15m")
    df_4h = load_candles(conn, "4h")
    df_1h = load_candles(conn, "1h")
    df_1d = load_candles(conn, "1d")
    funding = load_funding(conn)
    conn.close()

    print(f"15m: {len(df_15m)}, 4h: {len(df_4h)}, 1h: {len(df_1h)}, 1d: {len(df_1d)}, funding: {len(funding)} rows")

    config = BacktestConfig()
    results = []

    for name, (mechanism, precompute_fn, factory) in CANDIDATES_15M.items():
        try:
            if name == "V3_FUNDING_REVERSION_15M":
                prepared = precompute_fn(df_15m, funding)
            else:
                prepared = precompute_fn(df_15m)
            r = run_screen(name, mechanism, prepared, factory(), config)
        except Exception as e:
            r = {"name": name, "mechanism": mechanism, "error": str(e)}
        results.append(r)
        print(r)

    # HTF pullback (15m entries + 1h trend filter)
    try:
        prepared = precompute_htf_pullback(df_15m, df_1h)
        r = run_screen("V3_HTF_PULLBACK_15M", "1h trend + 15m pullback entry", prepared, htf_pullback_signal(), config)
    except Exception as e:
        r = {"name": "V3_HTF_PULLBACK_15M", "mechanism": "1h trend + 15m pullback entry", "error": str(e)}
    results.append(r)
    print(r)

    for name, (mechanism, precompute_fn, factory) in CANDIDATES_4H.items():
        try:
            if name == "V3_FUNDING_REVERSION_4H":
                prepared = precompute_fn(df_4h, funding)
            else:
                prepared = precompute_fn(df_4h)
            r = run_screen(name, mechanism, prepared, factory(), config)
        except Exception as e:
            r = {"name": name, "mechanism": mechanism, "error": str(e)}
        results.append(r)
        print(r)

    # HTF vol regime + 4h momentum (4h entries + 1d vol regime filter)
    try:
        prepared = precompute_htf_vol_momentum(df_4h, df_1d)
        r = run_screen("V3_HTF_VOL_MOMENTUM_4H", "1d vol regime + 4h momentum entry", prepared, htf_vol_momentum_signal(), config)
    except Exception as e:
        r = {"name": "V3_HTF_VOL_MOMENTUM_4H", "mechanism": "1d vol regime + 4h momentum entry", "error": str(e)}
    results.append(r)
    print(r)

    print("\n=== DATA_UNAVAILABLE (not backtested, real reason) ===")
    for name, reason in DATA_UNAVAILABLE.items():
        print(f"{name}: {reason}")

    print("\n=== SCREEN SUMMARY (sorted by PF) ===")
    valid = [r for r in results if "error" not in r and r.get("trades", 0) >= 10]
    valid.sort(key=lambda r: r["pf"], reverse=True)
    for r in valid:
        print(f"{r['name']}: trades={r['trades']} pf={r['pf']:.2f} return={r['return_pct']:.2f}% dd={r['max_dd_pct']:.2f}% win_rate={r['win_rate']:.1f}%")

    too_few = [r for r in results if "error" not in r and r.get("trades", 0) < 10]
    print(f"\n=== TOO FEW TRADES (<10) TO SCREEN ({len(too_few)}) ===")
    for r in too_few:
        print(r)

    errored = [r for r in results if "error" in r]
    print(f"\n=== ERRORS ({len(errored)}) ===")
    for r in errored:
        print(r)


if __name__ == "__main__":
    main()
