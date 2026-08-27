import numpy as np
import pandas as pd
import structlog

logger = structlog.get_logger()


class MarketRegimeDetector:
    REGIMES = {
        "TRENDING_BULLISH": "TRENDING_BULLISH",
        "TRENDING_BEARISH": "TRENDING_BEARISH",
        "RANGING": "RANGING",
        "HIGH_VOLATILITY": "HIGH_VOLATILITY",
        "LOW_VOLATILITY": "LOW_VOLATILITY",
        "BREAKOUT": "BREAKOUT",
        "POST_LIQUIDATION": "POST_LIQUIDATION",
        "UNCERTAIN": "UNCERTAIN",
    }

    def detect(self, df: pd.DataFrame) -> str:
        if df.empty or len(df) < 50:
            return self.REGIMES["UNCERTAIN"]

        latest = df.iloc[-1]
        recent = df.tail(20)

        vol_regime = self._detect_volatility_regime(latest, recent)
        trend_regime = self._detect_trend_regime(latest, recent)

        if vol_regime == "HIGH_VOLATILITY":
            if self._is_post_liquidation(df):
                return self.REGIMES["POST_LIQUIDATION"]
            return self.REGIMES["HIGH_VOLATILITY"]

        if vol_regime == "LOW_VOLATILITY":
            if self._is_breakout_imminent(recent):
                return self.REGIMES["BREAKOUT"]
            return self.REGIMES["LOW_VOLATILITY"]

        if trend_regime == "BULLISH":
            return self.REGIMES["TRENDING_BULLISH"]
        elif trend_regime == "BEARISH":
            return self.REGIMES["TRENDING_BEARISH"]

        if self._is_ranging(recent):
            return self.REGIMES["RANGING"]

        return self.REGIMES["UNCERTAIN"]

    def _detect_volatility_regime(self, latest: pd.Series, recent: pd.DataFrame) -> str:
        if "realized_vol_10" in latest.index and "realized_vol_50" in latest.index:
            if pd.notna(latest["realized_vol_10"]) and pd.notna(latest["realized_vol_50"]):
                ratio = latest["realized_vol_10"] / max(latest["realized_vol_50"], 0.01)
                if ratio > 1.5:
                    return "HIGH_VOLATILITY"
                elif ratio < 0.6:
                    return "LOW_VOLATILITY"

        if "atr_14" in recent.columns:
            atr_vals = recent["atr_14"].dropna()
            if len(atr_vals) > 0:
                current_atr = atr_vals.iloc[-1]
                mean_atr = atr_vals.mean()
                if current_atr > mean_atr * 2:
                    return "HIGH_VOLATILITY"
                elif current_atr < mean_atr * 0.5:
                    return "LOW_VOLATILITY"

        return "NORMAL"

    def _detect_trend_regime(self, latest: pd.Series, recent: pd.DataFrame) -> str:
        if "ema_50" in latest.index and "ema_200" in latest.index:
            if pd.notna(latest.get("ema_50")) and pd.notna(latest.get("ema_200")):
                if latest["ema_50"] > latest["ema_200"]:
                    if "adx_14" in latest.index and pd.notna(latest.get("adx_14")) and latest["adx_14"] > 25:
                        return "BULLISH"
                elif latest["ema_50"] < latest["ema_200"]:
                    if "adx_14" in latest.index and pd.notna(latest.get("adx_14")) and latest["adx_14"] > 25:
                        return "BEARISH"

        if "uptrend" in latest.index and "downtrend" in latest.index:
            if latest.get("uptrend", 0) == 1:
                return "BULLISH"
            elif latest.get("downtrend", 0) == 1:
                return "BEARISH"

        return "NONE"

    def _is_ranging(self, recent: pd.DataFrame) -> bool:
        if "adx_14" in recent.columns:
            adx_vals = recent["adx_14"].dropna()
            if len(adx_vals) > 0 and adx_vals.iloc[-1] < 20:
                return True
        if "consolidation" in recent.columns:
            if recent["consolidation"].iloc[-1] == 1:
                return True
        return False

    def _is_post_liquidation(self, df: pd.DataFrame) -> bool:
        if "liq_spike" in df.columns:
            recent_liqs = df["liq_spike"].tail(5)
            return recent_liqs.sum() > 0
        return False

    def _is_breakout_imminent(self, recent: pd.DataFrame) -> bool:
        if "bb_width" in recent.columns:
            bb_widths = recent["bb_width"].dropna()
            if len(bb_widths) > 0:
                return bb_widths.iloc[-1] < bb_widths.quantile(0.2)
        return False


def detect_regime_series(df: pd.DataFrame) -> pd.Series:
    """Vectorized equivalent of calling `MarketRegimeDetector().detect()` at
    every row (each call only ever seeing that row and its own trailing
    window, exactly like the scalar version) -- computing it in a loop is
    O(n^2) and impractical for regime-bucketed analysis over a full
    multi-year dataset. See tests/unit/test_regime.py for a parity check
    against the scalar detector on sample rows.

    Only used for read-only research/reporting (e.g. regime-bucketed
    baseline performance) -- the live signal path still uses the scalar
    `MarketRegimeDetector.detect()` on a real trailing dataframe slice.
    """
    n = len(df)
    regimes = pd.Series("UNCERTAIN", index=df.index, dtype=object)
    if n == 0:
        return regimes

    has_vol_cols = "realized_vol_10" in df.columns and "realized_vol_50" in df.columns
    has_atr = "atr_14" in df.columns
    has_trend_cols = "ema_50" in df.columns and "ema_200" in df.columns
    has_adx = "adx_14" in df.columns
    has_updown = "uptrend" in df.columns and "downtrend" in df.columns
    has_liq = "liq_spike" in df.columns
    has_consolidation = "consolidation" in df.columns
    has_bb = "bb_width" in df.columns

    vol_regime = pd.Series("NORMAL", index=df.index, dtype=object)
    if has_vol_cols:
        ratio = df["realized_vol_10"] / df["realized_vol_50"].clip(lower=0.01)
        vol_regime = np.where(ratio > 1.5, "HIGH_VOLATILITY", np.where(ratio < 0.6, "LOW_VOLATILITY", "NORMAL"))
        vol_regime = pd.Series(vol_regime, index=df.index)
        unresolved = df["realized_vol_10"].isna() | df["realized_vol_50"].isna()
        if has_atr:
            atr_mean_20 = df["atr_14"].rolling(20).mean()
            fallback = np.where(
                df["atr_14"] > atr_mean_20 * 2, "HIGH_VOLATILITY",
                np.where(df["atr_14"] < atr_mean_20 * 0.5, "LOW_VOLATILITY", "NORMAL"),
            )
            vol_regime = vol_regime.where(~unresolved, pd.Series(fallback, index=df.index))
    elif has_atr:
        atr_mean_20 = df["atr_14"].rolling(20).mean()
        vol_regime = pd.Series(np.where(
            df["atr_14"] > atr_mean_20 * 2, "HIGH_VOLATILITY",
            np.where(df["atr_14"] < atr_mean_20 * 0.5, "LOW_VOLATILITY", "NORMAL"),
        ), index=df.index)

    trend_regime = pd.Series("NONE", index=df.index, dtype=object)
    if has_trend_cols and has_adx:
        bullish = (df["ema_50"] > df["ema_200"]) & (df["adx_14"] > 25)
        bearish = (df["ema_50"] < df["ema_200"]) & (df["adx_14"] > 25)
        trend_regime = pd.Series(np.where(bullish, "BULLISH", np.where(bearish, "BEARISH", "NONE")), index=df.index)
    if has_updown:
        still_none = trend_regime == "NONE"
        fallback_trend = pd.Series(np.where(
            df["uptrend"] == 1, "BULLISH", np.where(df["downtrend"] == 1, "BEARISH", "NONE"),
        ), index=df.index)
        trend_regime = trend_regime.where(~still_none, fallback_trend)

    post_liq = pd.Series(False, index=df.index)
    if has_liq:
        post_liq = df["liq_spike"].rolling(5).sum() > 0

    is_ranging = pd.Series(False, index=df.index)
    if has_adx:
        is_ranging = is_ranging | (df["adx_14"] < 20)
    if has_consolidation:
        is_ranging = is_ranging | (df["consolidation"] == 1)

    is_breakout = pd.Series(False, index=df.index)
    if has_bb:
        rolling_q20 = df["bb_width"].rolling(20).quantile(0.2)
        is_breakout = df["bb_width"] < rolling_q20

    for i in range(n):
        if i < 49:  # matches `len(df) < 50` in the scalar version (0-indexed: need 50 rows total)
            continue
        vr = vol_regime.iloc[i]
        if vr == "HIGH_VOLATILITY":
            regimes.iloc[i] = "POST_LIQUIDATION" if post_liq.iloc[i] else "HIGH_VOLATILITY"
            continue
        if vr == "LOW_VOLATILITY":
            regimes.iloc[i] = "BREAKOUT" if is_breakout.iloc[i] else "LOW_VOLATILITY"
            continue
        tr = trend_regime.iloc[i]
        if tr == "BULLISH":
            regimes.iloc[i] = "TRENDING_BULLISH"
            continue
        if tr == "BEARISH":
            regimes.iloc[i] = "TRENDING_BEARISH"
            continue
        regimes.iloc[i] = "RANGING" if is_ranging.iloc[i] else "UNCERTAIN"

    return regimes
