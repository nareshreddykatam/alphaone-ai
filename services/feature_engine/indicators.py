import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period).mean()


def directional_indicators(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (plus_di, minus_di, adx) -- the full directional-movement
    system, not just the final ADX line, so +DI/-DI are available as their
    own features (Phase 3 feature group B: "directional indicators")."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)

    plus_dm = np.where((high - prev_high) > (prev_low - low), np.maximum(high - prev_high, 0), 0)
    minus_dm = np.where((prev_low - low) > (high - prev_high), np.maximum(prev_low - low, 0), 0)

    plus_dm = pd.Series(plus_dm, index=high.index)
    minus_dm = pd.Series(minus_dm, index=high.index)

    atr_val = atr(high, low, close, period)

    plus_di = 100 * ema(plus_dm, period) / atr_val.replace(0, np.nan)
    minus_di = 100 * ema(minus_dm, period) / atr_val.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = ema(dx, period)
    return plus_di, minus_di, adx_val


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    _, _, adx_val = directional_indicators(high, low, close, period)
    return adx_val


def roc(series: pd.Series, period: int = 10) -> pd.Series:
    """Rate of change, percent: causal (uses only the current and
    `period`-bars-ago value)."""
    return (series / series.shift(period) - 1) * 100


def momentum(series: pd.Series, period: int = 10) -> pd.Series:
    """Raw price momentum: current value minus the value `period` bars ago."""
    return series - series.shift(period)


def bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0):
    mid = sma(series, period)
    std = series.rolling(window=period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return upper, mid, lower


def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """Running VWAP over the entire passed-in series (no session reset).

    Causal (never uses future data), but note this keeps compounding across
    the whole history rather than resetting daily -- use `vwap_session` for
    the standard daily-anchored VWAP perp-futures strategies usually expect.
    Kept for backward compatibility with existing callers.
    """
    typical_price = (high + low + close) / 3
    cumulative_tp_vol = (typical_price * volume).cumsum()
    cumulative_vol = volume.cumsum()
    return cumulative_tp_vol / cumulative_vol.replace(0, np.nan)


def vwap_session(
    timestamp: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series
) -> pd.Series:
    """VWAP that resets at each UTC day boundary (candle timestamps are
    stored as naive UTC -- see services/market_data/binance.py). Still
    strictly causal: each bar's value only uses bars from the same UTC day
    up to and including itself.
    """
    typical_price = (high + low + close) / 3
    session = timestamp.dt.floor("D")
    tp_vol = typical_price * volume
    cum_tp_vol = tp_vol.groupby(session).cumsum()
    cum_vol = volume.groupby(session).cumsum()
    return cum_tp_vol / cum_vol.replace(0, np.nan)


def compute_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for period in [9, 20, 50, 100, 200]:
        df[f"ema_{period}"] = ema(df["close"], period)

    df["sma_20"] = sma(df["close"], 20)
    df["sma_50"] = sma(df["close"], 50)

    df["rsi_14"] = rsi(df["close"], 14)
    df["rsi_7"] = rsi(df["close"], 7)

    macd_line, signal_line, histogram = macd(df["close"])
    df["macd"] = macd_line
    df["macd_signal"] = signal_line
    df["macd_histogram"] = histogram

    df["atr_14"] = atr(df["high"], df["low"], df["close"], 14)
    df["atr_7"] = atr(df["high"], df["low"], df["close"], 7)

    plus_di_14, minus_di_14, adx_14 = directional_indicators(df["high"], df["low"], df["close"], 14)
    df["plus_di_14"] = plus_di_14
    df["minus_di_14"] = minus_di_14
    df["adx_14"] = adx_14

    bb_upper, bb_mid, bb_lower = bollinger_bands(df["close"])
    df["bb_upper"] = bb_upper
    df["bb_mid"] = bb_mid
    df["bb_lower"] = bb_lower
    df["bb_width"] = (bb_upper - bb_lower) / bb_mid.replace(0, np.nan)
    df["bb_pct"] = (df["close"] - bb_lower) / (bb_upper - bb_lower).replace(0, np.nan)

    df["vwap"] = vwap(df["high"], df["low"], df["close"], df["volume"])
    if "timestamp" in df.columns:
        df["vwap_session"] = vwap_session(df["timestamp"], df["high"], df["low"], df["close"], df["volume"])
    else:
        df["vwap_session"] = df["vwap"]

    df["price_vs_ema9"] = (df["close"] - df["ema_9"]) / df["ema_9"].replace(0, np.nan)
    df["price_vs_ema20"] = (df["close"] - df["ema_20"]) / df["ema_20"].replace(0, np.nan)
    df["price_vs_ema50"] = (df["close"] - df["ema_50"]) / df["ema_50"].replace(0, np.nan)
    df["price_vs_ema200"] = (df["close"] - df["ema_200"]) / df["ema_200"].replace(0, np.nan)
    df["price_vs_vwap"] = (df["close"] - df["vwap"]) / df["vwap"].replace(0, np.nan)

    df["ema9_above_ema20"] = (df["ema_9"] > df["ema_20"]).astype(int)
    df["ema20_above_ema50"] = (df["ema_20"] > df["ema_50"]).astype(int)
    df["ema50_above_ema200"] = (df["ema_50"] > df["ema_200"]).astype(int)

    df["rsi_overbought"] = (df["rsi_14"] > 70).astype(int)
    df["rsi_oversold"] = (df["rsi_14"] < 30).astype(int)

    df["macd_bullish"] = (df["macd"] > df["macd_signal"]).astype(int)

    df["roc_10"] = roc(df["close"], 10)
    df["momentum_10"] = momentum(df["close"], 10)

    return df
