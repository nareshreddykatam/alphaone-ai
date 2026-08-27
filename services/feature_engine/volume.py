import numpy as np
import pandas as pd


def compute_volume_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["volume_sma_20"] = df["volume"].rolling(window=20).mean()
    df["volume_sma_50"] = df["volume"].rolling(window=50).mean()

    df["relative_volume"] = df["volume"] / df["volume_sma_20"].replace(0, np.nan)

    df["volume_change"] = df["volume"].pct_change()
    df["volume_spike"] = (df["relative_volume"] > 2.0).astype(int)
    df["volume_dry"] = (df["relative_volume"] < 0.5).astype(int)

    df["price_up"] = (df["close"] > df["close"].shift(1)).astype(int)
    df["volume_on_up"] = df["volume"] * df["price_up"]
    df["volume_on_down"] = df["volume"] * (1 - df["price_up"])

    df["obv"] = (np.sign(df["close"].diff()) * df["volume"]).fillna(0).cumsum()

    df["volume_price_divergence"] = 0
    price_direction = np.sign(df["close"].diff(5))
    volume_direction = np.sign(df["volume"].diff(5))
    df["volume_price_divergence"] = (price_direction != volume_direction).astype(int)

    df["buy_sell_volume_ratio"] = df["volume_on_up"].rolling(20).sum() / df["volume_on_down"].rolling(20).sum().replace(0, np.nan)

    df = df.drop(columns=["price_up", "volume_on_up", "volume_on_down"], errors="ignore")

    return df
