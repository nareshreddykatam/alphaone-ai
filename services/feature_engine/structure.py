import numpy as np
import pandas as pd


def _detect_swings(df: pd.DataFrame, lookback: int = 5) -> tuple[pd.Series, pd.Series]:
    """Detect swing highs/lows using a centered comparison window, but stamp
    each confirmed swing onto the bar `lookback` periods AFTER it occurred.

    A swing point at bar `i` can only be confirmed once the `lookback` bars
    following it are known -- classifying bar `i` itself at the time bar `i`
    closes would require future data. Posting the confirmed value at
    `i + lookback` instead means nothing at time T ever depends on data
    after T; consumers simply see the swing appear `lookback` bars later
    than it actually happened, which is the correct point-in-time behavior.
    """
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    swing_highs = np.zeros(n)
    swing_lows = np.zeros(n)

    for i in range(lookback, n - lookback):
        confirm_idx = i + lookback
        window_high = highs[i - lookback:i + lookback + 1]
        window_low = lows[i - lookback:i + lookback + 1]
        if highs[i] == max(window_high):
            swing_highs[confirm_idx] = highs[i]
        if lows[i] == min(window_low):
            swing_lows[confirm_idx] = lows[i]

    return (
        pd.Series(swing_highs, index=df.index),
        pd.Series(swing_lows, index=df.index),
    )


def detect_market_structure(df: pd.DataFrame, lookback: int = 5) -> pd.DataFrame:
    df = df.copy()
    swing_highs, swing_lows = _detect_swings(df, lookback)

    df["swing_high"] = swing_highs
    df["swing_low"] = swing_lows

    df["higher_high"] = 0
    df["higher_low"] = 0
    df["lower_high"] = 0
    df["lower_low"] = 0

    last_swing_high = None
    last_swing_low = None
    prev_swing_high = None
    prev_swing_low = None

    for i in range(len(df)):
        if swing_highs.iloc[i] > 0:
            prev_swing_high = last_swing_high
            last_swing_high = swing_highs.iloc[i]

        if swing_lows.iloc[i] > 0:
            prev_swing_low = last_swing_low
            last_swing_low = swing_lows.iloc[i]

        if last_swing_high is not None and prev_swing_high is not None:
            df.iloc[i, df.columns.get_loc("higher_high")] = int(last_swing_high > prev_swing_high)
            df.iloc[i, df.columns.get_loc("lower_high")] = int(last_swing_high < prev_swing_high)

        if last_swing_low is not None and prev_swing_low is not None:
            df.iloc[i, df.columns.get_loc("higher_low")] = int(last_swing_low > prev_swing_low)
            df.iloc[i, df.columns.get_loc("lower_low")] = int(last_swing_low < prev_swing_low)

    df["uptrend"] = ((df["higher_high"] == 1) & (df["higher_low"] == 1)).astype(int)
    df["downtrend"] = ((df["lower_high"] == 1) & (df["lower_low"] == 1)).astype(int)

    df["ema_50_prev"] = df["ema_50"].shift(1) if "ema_50" in df.columns else 0
    df["ema_200_prev"] = df["ema_200"].shift(1) if "ema_200" in df.columns else 0

    df["break_of_structure"] = 0
    if "ema_50" in df.columns and "ema_200" in df.columns:
        cross_up = (df["ema_50"] > df["ema_200"]) & (df["ema_50_prev"] <= df["ema_200_prev"])
        cross_down = (df["ema_50"] < df["ema_200"]) & (df["ema_50_prev"] >= df["ema_200_prev"])
        df["break_of_structure"] = cross_up.astype(int) - cross_down.astype(int)

    recent_high = df["high"].rolling(window=20).max()
    recent_low = df["low"].rolling(window=20).min()

    df["near_resistance"] = ((df["close"] >= recent_high * 0.995) & (df["close"] <= recent_high)).astype(int)
    df["near_support"] = ((df["close"] <= recent_low * 1.005) & (df["close"] >= recent_low)).astype(int)

    df["consolidation"] = (
        (df["high"].rolling(20).max() - df["low"].rolling(20).min()) / df["close"] < 0.03
    ).astype(int)

    # Donchian / range position (Phase 3, ML feature group E): where the
    # current close sits within the trailing 20-bar high/low channel, 0 =
    # at the channel low, 1 = at the channel high. Purely a function of
    # this bar and the 19 before it -- no future data.
    donchian_width = (recent_high - recent_low).replace(0, np.nan)
    df["donchian_position_20"] = (df["close"] - recent_low) / donchian_width

    df = df.drop(columns=["ema_50_prev", "ema_200_prev"], errors="ignore")

    return df
