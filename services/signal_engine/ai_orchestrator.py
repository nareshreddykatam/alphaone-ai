"""AI signal orchestrator (AI Trading V1, Phase 8).

Combines each PRODUCTION_ELIGIBLE strategy's own independent LONG/SHORT
signal (produced by services/signal_engine/multi_strategy_engine.py --
reused, never reimplemented, so strategy independence and no-cross-
suppression are inherited automatically, not re-promised here) with real,
currently-computable market evidence: regime (services/signal_engine/
regime.py), expected volatility (ATR%), and -- ONLY if a model has
actually cleared research validation and been deployed (BotState key
"deployed_model_metadata", the same one apps/api/routers/model.py already
reports) -- a calibrated probability estimate from that model.

Per the task's explicit instruction: "The AI layer should be capable of
saying 'Only S05 has a signal' and still evaluate that opportunity" --
there is NO consensus requirement here. Each strategy signal is enriched
and evaluated independently, one AIDecision per signal; this module never
suppresses one strategy's signal because another disagrees or is silent.

NEVER FABRICATES a probability or confidence number. When no model is
deployed (see reports/AI_TRADING_RESEARCH_V1.txt for why),
probability_long/short/no_trade, expected_return, and model_version are
all None -- not zero, not guessed -- and `confidence` falls back to the
strategy's own already-real, already-documented quality tier (LOW/MEDIUM/
HIGH, computed from R:R -- see services/signal_engine/multi_strategy.py:
_rr_quality), never an invented number standing in for a probability that
was never actually computed.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import BotState, Prediction, Signal
from services.feature_engine.indicators import atr
from services.signal_engine.regime import MarketRegimeDetector

logger = structlog.get_logger()

DEPLOYED_MODEL_KEY = "deployed_model_metadata"


@dataclass
class AIDecision:
    symbol: str
    market: str
    timeframe: str
    strategy_sources: list[str]
    direction: str  # LONG / SHORT (never called for a NO_TRADE signal)
    probability_long: Optional[float]
    probability_short: Optional[float]
    probability_no_trade: Optional[float]
    expected_return: Optional[float]  # R-multiples, only when a real deployed model backs it
    expected_volatility: Optional[float]  # ATR / close -- real and computable whenever ATR is available
    regime: str
    confidence: str  # a rounded calibrated probability string if model-backed, else the strategy's own real quality tier
    entry: float
    stop_loss: Optional[float]
    take_profit_1: Optional[float]
    take_profit_2: Optional[float]
    take_profit_3: Optional[float]
    risk_reward: Optional[float]
    timestamp: datetime
    model_version: Optional[str]
    model_status: str  # "NO_MODEL_DEPLOYED" or "MODEL_BACKED"


# Cached once per process (mirrors services/market_data/live_state.py's
# singleton pattern) -- re-querying BotState and re-loading the joblib file
# on every single signal would be wasteful; a newly-deployed model only
# takes effect on the next process restart, which is an acceptable
# trade-off for research-grade infrastructure that changes rarely.
_cache = {"loaded": False, "model": None, "feature_cols": None, "metadata": None, "barrier_config": None}


async def _load_deployed_model(session: AsyncSession):
    if _cache["loaded"]:
        return _cache["model"], _cache["feature_cols"], _cache["metadata"], _cache["barrier_config"]
    _cache["loaded"] = True

    row = (await session.execute(select(BotState).where(BotState.key == DEPLOYED_MODEL_KEY))).scalar_one_or_none()
    if row is None:
        return None, None, None, None

    from ml.training.trainer import ModelTrainer
    from ml.labeling import TripleBarrierConfig

    meta = row.value
    try:
        trainer = ModelTrainer(model_path="./ml/models")
        model = trainer.load_model(meta["model_name"], meta["model_version"])
        if model is None:
            return None, None, None, None
        barrier_config = TripleBarrierConfig(**meta.get("barrier_params", {}))
        _cache.update(model=model, feature_cols=meta["feature_names"], metadata=meta, barrier_config=barrier_config)
        return model, meta["feature_names"], meta, barrier_config
    except Exception as e:
        logger.warning("Failed to load deployed AI model -- falling back to strategy-only evidence", error=str(e))
        return None, None, None, None


async def enrich_signal_with_ai_evidence(
    session: AsyncSession, df: pd.DataFrame, signal: Signal, symbol: str = "BTC/USDT",
) -> AIDecision:
    """`df` must be the same closed-candle dataframe the strategy that
    produced `signal` was just evaluated against (real closed candles, no
    lookahead -- see multi_strategy_engine.evaluate_all_strategies_for_timeframe,
    the intended caller)."""
    last = df.iloc[-1]
    regime = MarketRegimeDetector().detect(df)

    atr_series = atr(df["high"], df["low"], df["close"], 14)
    atr_val = float(atr_series.iloc[-1]) if pd.notna(atr_series.iloc[-1]) else None
    expected_volatility = (atr_val / float(last["close"])) if atr_val and last["close"] else None

    entry = signal.entry_price if signal.entry_price is not None else float(last["close"])
    risk_reward = None
    if signal.entry_price and signal.stop_loss and signal.take_profit_1:
        risk = abs(signal.entry_price - signal.stop_loss)
        if risk > 0:
            risk_reward = round(abs(signal.take_profit_1 - signal.entry_price) / risk, 2)

    model, feature_cols, metadata, barrier_config = await _load_deployed_model(session)

    probability_long = probability_short = probability_no_trade = None
    expected_return = None
    model_version = None
    model_status = "NO_MODEL_DEPLOYED"

    if model is not None and feature_cols is not None:
        try:
            from ml.features.feature_groups import assemble_features
            from ml.signal import expected_value_r

            assembled, _ = assemble_features(df, include_strategy_signals=True)
            row = assembled.iloc[-1]
            if not row[feature_cols].isna().any():
                X = row[feature_cols].to_numpy(dtype=float).reshape(1, -1)
                proba = model.predict_proba(X)[0]
                probability_short, probability_no_trade, probability_long = (
                    float(proba[0]), float(proba[1]), float(proba[2])
                )
                model_version = metadata.get("model_version")
                model_status = "MODEL_BACKED"
                reward_r = barrier_config.tp_atr_multiple / barrier_config.sl_atr_multiple
                p_favorable = probability_long if signal.signal_type == "LONG" else probability_short
                expected_return = round(expected_value_r(p_favorable, reward_r, 1.0, 0.05), 3)

                # Persisted so services/model_monitor/monitor.py has a real
                # prediction-distribution history to detect drift against --
                # every model-backed evaluation is recorded, not only the
                # ones that end up qualifying as a trade signal.
                predicted_class = max(
                    (("SHORT", probability_short), ("NO_TRADE", probability_no_trade), ("LONG", probability_long)),
                    key=lambda kv: kv[1],
                )[0]
                session.add(Prediction(
                    signal_id=signal.signal_id, timestamp=signal.timestamp, symbol=symbol,
                    signal_type=predicted_class, long_probability=probability_long,
                    short_probability=probability_short, no_trade_probability=probability_no_trade,
                    confidence=max(probability_long, probability_short, probability_no_trade),
                    market_regime=regime, model_version=model_version,
                    feature_version=metadata.get("feature_version"),
                ))
                await session.commit()
        except Exception as e:
            logger.warning("AI model evaluation failed for this bar -- falling back to strategy-only evidence", error=str(e))

    if model_status == "MODEL_BACKED":
        p = probability_long if signal.signal_type == "LONG" else probability_short
        confidence = f"{p:.0%} calibrated probability"
    else:
        confidence = signal.quality or "LOW"

    return AIDecision(
        symbol=symbol, market="CoinDCX BTC/USDT Perpetual", timeframe=signal.timeframe or "4h",
        strategy_sources=[signal.strategy_name], direction=signal.signal_type,
        probability_long=probability_long, probability_short=probability_short,
        probability_no_trade=probability_no_trade, expected_return=expected_return,
        expected_volatility=round(expected_volatility, 5) if expected_volatility is not None else None,
        regime=regime, confidence=confidence, entry=entry, stop_loss=signal.stop_loss,
        take_profit_1=signal.take_profit_1, take_profit_2=signal.take_profit_2, take_profit_3=signal.take_profit_3,
        risk_reward=risk_reward, timestamp=signal.timestamp, model_version=model_version, model_status=model_status,
    )
