"""Live orchestrator for the 10-strategy independent signal system
(services/signal_engine/multi_strategy.py). Evaluates every
PRODUCTION_ELIGIBLE strategy registered for a given timeframe against that
timeframe's real closed candles -- independently: no consensus, no
cross-strategy suppression. Strategy 1 firing LONG has zero effect on
whether Strategy 2 can independently fire SHORT on the exact same candle;
both are separately deduped (services/signal_engine/live_signal.py:
signal_already_exists_for_candle, keyed by strategy_name+timeframe) and
persisted as separate Signal rows.

RESEARCH_ONLY strategies are deliberately never evaluated here -- they
exist for backtesting/research and the frontend's filter list only,
never touching the live pipeline. See multi_strategy.py's module
docstring for how PRODUCTION_ELIGIBLE was decided.
"""
from datetime import datetime
from typing import Optional

import pandas as pd
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import Signal
from services.signal_engine.live_signal import (
    DEFAULT_LOOKBACK_BARS,
    MIN_BARS_REQUIRED,
    _load_recent_candles,
    _persist_new_signal,
    is_signal_generation_paused,
    signal_already_exists_for_candle,
)
from services.signal_engine.multi_strategy import MULTI_STRATEGY_REGISTRY, MTFTrendStrategy
from services.signal_engine.regime import MarketRegimeDetector

logger = structlog.get_logger()


async def evaluate_all_strategies_for_timeframe(
    session: AsyncSession, symbol: str, timeframe: str, df_1d: Optional[pd.DataFrame] = None,
) -> list[Signal]:
    """Evaluates every PRODUCTION_ELIGIBLE strategy registered for
    `timeframe` against symbol's real closed candles for that timeframe,
    independently. Returns the list of newly-persisted Signal rows (never
    includes NO_TRADE, never includes an already-alerted strategy/candle
    combination). `df_1d` is only used by S10 (the MTF strategy) and is
    fetched lazily on demand if not supplied, so every OTHER strategy's
    evaluation never pays for a daily-candle query it doesn't need.
    """
    if await is_signal_generation_paused(session):
        return []

    df = await _load_recent_candles(session, symbol, timeframe, DEFAULT_LOOKBACK_BARS)
    if len(df) < MIN_BARS_REQUIRED:
        return []  # not enough real data yet -- never fabricate a signal

    candle_timestamp = df.iloc[-1]["timestamp"]
    regime = MarketRegimeDetector().detect(df)

    persisted: list[Signal] = []
    for definition in MULTI_STRATEGY_REGISTRY:
        if definition.timeframe != timeframe or definition.production_status != "PRODUCTION_ELIGIBLE":
            continue

        strategy = definition.make_strategy()
        if isinstance(strategy, MTFTrendStrategy):
            if df_1d is None:
                df_1d = await _load_recent_candles(session, symbol, "1d", DEFAULT_LOOKBACK_BARS)
            if len(df_1d) < MIN_BARS_REQUIRED:
                continue  # never fabricate the daily trend filter from insufficient data
            result = strategy.generate_with_daily(df, df_1d)
        else:
            result = strategy.generate(df)

        if result.signal_type == "NO_TRADE":
            continue

        # Dedup on result.strategy_name (what will ACTUALLY be persisted to
        # Signal.strategy_name), not definition.strategy_id -- for S05 these
        # deliberately differ: BaselineStrategy.name is the untouched,
        # pre-existing "trend_following_donchian_adx" (never renamed to the
        # new "S05_..." registry id, to avoid changing the identity string
        # every existing persisted row / the live_breakout_job intrabar path
        # already uses). Keying on the id instead of the real value would
        # silently defeat the dedup check for S05.
        if await signal_already_exists_for_candle(session, symbol, timeframe, result.strategy_name, candle_timestamp):
            continue  # this exact strategy already alerted this exact candle -- edge-triggered, not repeated

        signal = await _persist_new_signal(session, result, symbol, timeframe, candle_timestamp, regime, result.reasoning)
        persisted.append(signal)
        logger.info(
            "Multi-strategy signal generated", strategy_id=definition.strategy_id,
            timeframe=timeframe, signal_type=result.signal_type, signal_id=signal.signal_id,
        )

    return persisted
