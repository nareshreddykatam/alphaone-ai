import numpy as np
import pandas as pd


def compute_volatility_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["returns"] = df["close"].pct_change()
    df["log_returns"] = np.log(df["close"] / df["close"].shift(1))

    df["realized_vol_10"] = df["returns"].rolling(10).std() * np.sqrt(365 * 24)
    df["realized_vol_20"] = df["returns"].rolling(20).std() * np.sqrt(365 * 24)
    df["realized_vol_50"] = df["returns"].rolling(50).std() * np.sqrt(365 * 24)

    df["vol_ratio_short_long"] = df["realized_vol_10"] / df["realized_vol_50"].replace(0, np.nan)

    df["high_low_range"] = (df["high"] - df["low"]) / df["close"].replace(0, np.nan)
    df["range_sma_20"] = df["high_low_range"].rolling(20).mean()
    df["range_expanding"] = (df["high_low_range"] > df["range_sma_20"] * 1.5).astype(int)
    df["range_contracting"] = (df["high_low_range"] < df["range_sma_20"] * 0.5).astype(int)

    df["upper_shadow"] = (df["high"] - df[["open", "close"]].max(axis=1)) / df["close"].replace(0, np.nan)
    df["lower_shadow"] = (df[["open", "close"]].min(axis=1) - df["low"]) / df["close"].replace(0, np.nan)

    df["return_1"] = df["close"].pct_change(1)
    df["return_3"] = df["close"].pct_change(3)
    df["return_5"] = df["close"].pct_change(5)
    df["return_10"] = df["close"].pct_change(10)
    df["return_20"] = df["close"].pct_change(20)

    df["max_drawdown_20"] = (
        df["close"].rolling(20).max() - df["close"]
    ) / df["close"].rolling(20).max().replace(0, np.nan)

    df["garman_klass_vol"] = np.sqrt(
        0.5 * np.log(df["high"] / df["low"].replace(0, np.nan)) ** 2
        - (2 * np.log(2) - 1) * np.log(df["close"] / df["open"].replace(0, np.nan)) ** 2
    )

    return df
