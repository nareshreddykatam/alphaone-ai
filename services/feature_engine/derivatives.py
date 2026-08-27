import numpy as np
import pandas as pd


def _asof_merge_value(df: pd.DataFrame, source: pd.DataFrame, value_col: str, out_col: str) -> pd.Series:
    """Timestamp-correct backward as-of join: each candle gets the most recent
    `value_col` reading known at or before its own timestamp. Using
    `direction="backward"` guarantees a candle can never be assigned a value
    that was observed after it closed, regardless of the source's sampling
    frequency relative to `df`'s.
    """
    left = df[["timestamp"]].sort_values("timestamp")
    right = source[["timestamp", value_col]].dropna(subset=["timestamp"]).sort_values("timestamp")
    merged = pd.merge_asof(left, right, on="timestamp", direction="backward")
    merged = merged.set_index(left.index)
    return merged[value_col].rename(out_col)


def compute_derivatives_features(
    df: pd.DataFrame,
    funding_rates: pd.DataFrame | None = None,
    open_interest: pd.DataFrame | None = None,
    liquidations: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    funding_rates: DataFrame with columns [timestamp, rate]
    open_interest: DataFrame with columns [timestamp, value]
    liquidations:  DataFrame with columns [timestamp, side, quantity]

    All three are aligned onto `df` by timestamp (as-of backward join), never
    by row position -- a source series sampled at a different frequency than
    `df` (e.g. 8h funding onto 1h candles) is handled correctly and can never
    leak a future-dated reading backward onto an earlier candle.
    """
    df = df.copy()

    if funding_rates is not None and len(funding_rates) > 0:
        df["funding_rate"] = _asof_merge_value(df, funding_rates, "rate", "funding_rate").values
        df["funding_rate_sma_8"] = df["funding_rate"].rolling(8).mean()
        df["funding_extreme_high"] = (df["funding_rate"] > 0.001).astype(int)
        df["funding_extreme_low"] = (df["funding_rate"] < -0.001).astype(int)
        df["funding_trend"] = df["funding_rate"].diff(3)
    else:
        for col in ["funding_rate", "funding_rate_sma_8", "funding_extreme_high", "funding_extreme_low", "funding_trend"]:
            df[col] = 0.0

    if open_interest is not None and len(open_interest) > 0:
        df["open_interest"] = _asof_merge_value(df, open_interest, "value", "open_interest").values
        df["oi_change"] = df["open_interest"].pct_change()
        df["oi_change_sma"] = df["oi_change"].rolling(10).mean()
        df["oi_acceleration"] = df["oi_change"].diff()
        df["price_oi_corr"] = df["close"].rolling(20).corr(df["open_interest"])
    else:
        for col in ["open_interest", "oi_change", "oi_change_sma", "oi_acceleration", "price_oi_corr"]:
            df[col] = 0.0

    if liquidations is not None and len(liquidations) > 0:
        liqs = liquidations.copy().sort_values("timestamp")
        long_liqs = liqs[liqs["side"] == "long"][["timestamp", "quantity"]].rename(columns={"quantity": "long_qty"})
        short_liqs = liqs[liqs["side"] == "short"][["timestamp", "quantity"]].rename(columns={"quantity": "short_qty"})

        # Sum liquidation volume into each candle's own [prev_close, this_close] bucket
        # using merge_asof to find which candle each liquidation belongs to, then
        # grouping -- this is timestamp-correct regardless of df's timeframe,
        # unlike a fixed "1h" resample.
        df_sorted = df[["timestamp"]].sort_values("timestamp").reset_index()

        def _bucket_sum(events: pd.DataFrame, qty_col: str) -> pd.Series:
            if events.empty:
                return pd.Series(0.0, index=df.index)
            events = events.sort_values("timestamp")
            assigned = pd.merge_asof(events, df_sorted, on="timestamp", direction="forward")
            grouped = assigned.groupby("index")[qty_col].sum()
            out = pd.Series(0.0, index=df.index)
            out.loc[grouped.index] = grouped.values
            return out

        df["long_liquidations"] = _bucket_sum(long_liqs, "long_qty")
        df["short_liquidations"] = _bucket_sum(short_liqs, "short_qty")
        df["liq_spike"] = (
            (df["long_liquidations"] > df["long_liquidations"].rolling(20).mean() * 3) |
            (df["short_liquidations"] > df["short_liquidations"].rolling(20).mean() * 3)
        ).astype(int)
    else:
        for col in ["long_liquidations", "short_liquidations", "liq_spike"]:
            df[col] = 0.0

    return df
