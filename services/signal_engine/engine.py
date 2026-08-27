from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import pandas as pd
import structlog

from services.signal_engine.regime import MarketRegimeDetector
from services.signal_engine.quality import bucket_signal_quality

logger = structlog.get_logger()


@dataclass
class SignalOutput:
    signal_id: str
    signal_type: str
    confidence: float
    long_probability: float
    short_probability: float
    no_trade_probability: float
    entry_price: Optional[float]
    stop_loss: Optional[float]
    take_profit_1: Optional[float]
    take_profit_2: Optional[float]
    take_profit_3: Optional[float]
    risk_reward: Optional[float]
    market_regime: str
    reasoning: str
    timestamp: datetime
    symbol: str
    quality: str = "LOW"
    strategy_name: str = "ml_model"
    model_version: Optional[str] = None
    expiry: Optional[datetime] = None


class SignalEngine:
    def __init__(self, prediction_threshold: float = 0.55):
        self.prediction_threshold = prediction_threshold
        self.regime_detector = MarketRegimeDetector()

    def generate_signal(
        self,
        ml_prediction: dict,
        df,
        entry_price: float,
        symbol: str = "BTC/USDT",
        signal_id: str = "",
        strategy_name: str = "ml_model",
        model_version: Optional[str] = None,
    ) -> SignalOutput:
        regime = self.regime_detector.detect(df)
        long_prob = ml_prediction.get("long_probability", 0)
        short_prob = ml_prediction.get("short_probability", 0)
        no_trade_prob = ml_prediction.get("no_trade_probability", 0)

        confidence = max(long_prob, short_prob)
        signal_type = self._determine_signal_type(long_prob, short_prob, no_trade_prob, regime)

        reasoning = self._generate_reasoning(signal_type, long_prob, short_prob, regime, df)

        entry, sl, tp1, tp2, tp3, rr = None, None, None, None, None, None

        if signal_type in ("LONG", "SHORT"):
            entry, sl, tp1, tp2, tp3, rr = self._calculate_levels(
                entry_price, signal_type, df
            )

        return SignalOutput(
            signal_id=signal_id,
            signal_type=signal_type,
            confidence=round(confidence, 4),
            long_probability=round(long_prob, 4),
            short_probability=round(short_prob, 4),
            no_trade_probability=round(no_trade_prob, 4),
            entry_price=entry,
            stop_loss=sl,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            risk_reward=rr,
            market_regime=regime,
            reasoning=reasoning,
            timestamp=datetime.utcnow(),
            symbol=symbol,
            quality=bucket_signal_quality(confidence),
            strategy_name=strategy_name,
            model_version=model_version,
        )

    def _determine_signal_type(
        self, long_prob: float, short_prob: float, no_trade_prob: float, regime: str
    ) -> str:
        if no_trade_prob > 0.5:
            return "NO_TRADE"

        if long_prob < self.prediction_threshold and short_prob < self.prediction_threshold:
            return "NO_TRADE"

        if abs(long_prob - short_prob) < 0.15:
            return "NO_TRADE"

        if regime in ("HIGH_VOLATILITY", "UNCERTAIN", "POST_LIQUIDATION"):
            return "NO_TRADE"

        if long_prob > short_prob and long_prob >= self.prediction_threshold:
            if regime == "TRENDING_BEARISH":
                return "NO_TRADE"
            return "LONG"

        if short_prob > long_prob and short_prob >= self.prediction_threshold:
            if regime == "TRENDING_BULLISH":
                return "NO_TRADE"
            return "SHORT"

        return "NO_TRADE"

    def _calculate_levels(
        self, entry_price: float, signal_type: str, df
    ) -> tuple[float, float, float, float, float, float]:
        atr_val = 100.0
        if "atr_14" in df.columns:
            recent_atr = df["atr_14"].dropna().tail(5)
            if len(recent_atr) > 0:
                atr_val = recent_atr.iloc[-1]

        if atr_val <= 0:
            atr_val = entry_price * 0.01

        if signal_type == "LONG":
            entry = entry_price
            sl = entry - 2 * atr_val
            tp1 = entry + 2 * atr_val
            tp2 = entry + 3 * atr_val
            tp3 = entry + 5 * atr_val
        else:
            entry = entry_price
            sl = entry + 2 * atr_val
            tp1 = entry - 2 * atr_val
            tp2 = entry - 3 * atr_val
            tp3 = entry - 5 * atr_val

        risk = abs(entry - sl)
        reward = abs(tp1 - entry)
        rr = round(reward / risk, 2) if risk > 0 else 0

        return round(entry, 2), round(sl, 2), round(tp1, 2), round(tp2, 2), round(tp3, 2), rr

    def _generate_reasoning(
        self, signal_type: str, long_prob: float, short_prob: float, regime: str, df
    ) -> str:
        if signal_type == "NO_TRADE":
            reasons = []
            if long_prob < 0.4 and short_prob < 0.4:
                reasons.append("Low probability for both directions")
            if regime in ("HIGH_VOLATILITY", "UNCERTAIN"):
                reasons.append(f"Market regime is {regime}")
            if not reasons:
                reasons.append("Signal confidence below threshold")
            return "NO TRADE: " + "; ".join(reasons)

        direction = "LONG" if signal_type == "LONG" else "SHORT"
        reasons = []

        latest = df.iloc[-1]

        if regime.startswith("TRENDING"):
            reasons.append(f"Market in {regime} trend")

        if "rsi_14" in latest.index and pd.notna(latest.get("rsi_14")):
            if signal_type == "LONG" and latest["rsi_14"] < 65:
                reasons.append("RSI not overbought")
            elif signal_type == "SHORT" and latest["rsi_14"] > 35:
                reasons.append("RSI not oversold")

        if "macd_bullish" in latest.index:
            if signal_type == "LONG" and latest.get("macd_bullish") == 1:
                reasons.append("MACD bullish")
            elif signal_type == "SHORT" and latest.get("macd_bullish") == 0:
                reasons.append("MACD bearish")

        reasons.append(f"Model {direction} probability: {long_prob if signal_type == 'LONG' else short_prob:.1%}")

        return f"{direction} because: " + "; ".join(reasons)
