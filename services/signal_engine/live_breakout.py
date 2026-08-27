"""Live/intrabar breakout detection for the EXISTING, unchanged 4h
Donchian(20)+ADX(25) strategy (BaselineStrategy). This does NOT change the
strategy, its parameters, or its timeframe -- real walk-forward research
(9 out-of-sample folds each, real fees/slippage/spread via the existing
backtester) found no credible edge at 15m, 1h, or either lower-timeframe
+4h-filter multi-timeframe variant (all strictly worse than 4h, with a
clean monotonic degradation as trade frequency increased -- PF 1.02 at 4h
down to PF 0.69 at 15m). Per the explicit instruction to retain 4h rather
than invent an unvalidated strategy, this module's only job is to detect
the SAME validated 4h breakout condition intrabar -- while the current 4h
candle is still forming -- instead of waiting up to 15 minutes after it
closes (the existing signal_generation_job's cadence). "More frequent"
here means more TIMELY, not a different, faster, unvalidated strategy.

Cross-venue disclosure: the historical closed candles are Binance-sourced
(services/market_data/binance.py); the live tick spliced onto them for the
forming candle comes from CoinDCX's public B-BTC_USDT WebSocket
(services/market_data/live_state.py's `market_ws`, built in the Live
Market Data phase) -- reused here rather than building a second live feed,
per the explicit instruction not to duplicate market-data infrastructure.
Both are liquid BTC/USDT perpetual markets that track closely, but this is
a real, disclosed simplification (see docs/known_limitations.md), not a
hidden one.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import ConnectionState, Signal, SignalOutcome, SignalOutcomeType
from services.market_data.coindcx_ws import CoinDCXMarketDataWebSocket
from services.signal_engine.live_signal import (
    DEFAULT_LOOKBACK_BARS,
    MIN_BARS_REQUIRED,
    _load_recent_candles,
    _persist_new_signal,
    is_signal_generation_paused,
    signal_already_exists_for_candle,
)
from services.signal_engine.regime import MarketRegimeDetector
from services.signal_engine.strategy import BaselineStrategy

TIMEFRAME_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400,
}


@dataclass
class FormingCandle:
    """The currently-forming (not yet closed) OHLC bar, built purely from
    live ticks -- never persisted to the Candle table (per the explicit
    instruction not to grow the database with per-tick rows; only a
    confirmed, CLOSED candle from the existing ingestion job ever becomes
    a real Candle row)."""
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_count: int = 1


class LiveCandleAggregator:
    """Aggregates live price ticks into the currently-forming bar for a
    fixed timeframe. Pure and stateful (deliberately -- it must remember
    the running high/low across calls), fully unit-testable without a
    real connection. A new tick landing in a later bucket than the
    current one implicitly means the previous bucket's forming candle is
    now closed (its data was already ingested for real by the existing
    candle_ingestion_job well before this matters -- this class never
    tries to be the source of truth for a closed candle)."""

    def __init__(self, timeframe: str = "4h"):
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"Unsupported timeframe for live aggregation: {timeframe}")
        self._interval_seconds = TIMEFRAME_SECONDS[timeframe]
        self.current: Optional[FormingCandle] = None

    def _bucket_start(self, ts: datetime) -> datetime:
        epoch = datetime(1970, 1, 1)
        elapsed = (ts - epoch).total_seconds()
        bucket_seconds = int(elapsed // self._interval_seconds) * self._interval_seconds
        return epoch + timedelta(seconds=bucket_seconds)

    def on_tick(self, price: float, ts: Optional[datetime] = None) -> FormingCandle:
        ts = ts or datetime.utcnow()
        bucket_start = self._bucket_start(ts)
        if self.current is None or self.current.open_time != bucket_start:
            self.current = FormingCandle(open_time=bucket_start, open=price, high=price, low=price, close=price)
        else:
            self.current.high = max(self.current.high, price)
            self.current.low = min(self.current.low, price)
            self.current.close = price
            self.current.tick_count += 1
        return self.current


async def evaluate_live_breakout(
    session: AsyncSession,
    market_ws: CoinDCXMarketDataWebSocket,
    aggregator: LiveCandleAggregator,
    symbol: str = "BTC/USDT",
    timeframe: str = "4h",
) -> Optional[Signal]:
    """Evaluates the SAME unmodified BaselineStrategy against [closed
    candles + the live forming candle]. Returns a newly-persisted Signal
    only for a genuinely NEW breakout that hasn't already been alerted for
    this exact candle -- never fires repeatedly while price sits above/
    below the level, and a restart is safe: the dedup check is a real DB
    query (signal_already_exists_for_candle), not in-memory state, so a
    freshly-restarted process re-checks the database before ever alerting
    again for a candle it (or a previous process) already caught."""
    if await is_signal_generation_paused(session):
        return None

    # Never evaluate on data that isn't genuinely fresh -- STALE/
    # DISCONNECTED/UNAVAILABLE market data must never drive a live alert.
    if market_ws.connection_status() != ConnectionState.LIVE:
        return None
    price = market_ws.state.last_price_usdt
    if price is None:
        return None

    closed_df = await _load_recent_candles(session, symbol, timeframe, DEFAULT_LOOKBACK_BARS)
    if len(closed_df) < MIN_BARS_REQUIRED:
        return None

    forming = aggregator.on_tick(price, market_ws.state.received_at)

    # The forming candle must be the bar that comes immediately after the
    # latest CLOSED one -- if it's older (candle_ingestion_job hasn't
    # caught up to a candle that already closed) or implausibly far ahead
    # (a gap), don't guess; skip this tick rather than evaluate against a
    # forming candle that doesn't actually follow the closed history.
    latest_closed_time = closed_df.iloc[-1]["timestamp"]
    expected_forming_open = latest_closed_time + timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    if forming.open_time != expected_forming_open:
        return None

    if await signal_already_exists_for_candle(session, symbol, forming.open_time):
        return None  # already alerted (or confirmed) this exact candle -- edge-triggered, not repeated

    combined = pd.concat(
        [
            closed_df,
            pd.DataFrame([{
                "timestamp": forming.open_time, "open": forming.open, "high": forming.high,
                "low": forming.low, "close": forming.close, "volume": 0.0,
            }]),
        ],
        ignore_index=True,
    )

    strategy = BaselineStrategy()  # unmodified -- same parameters as the closed-candle path
    result = strategy.generate(combined)
    if result.signal_type == "NO_TRADE":
        return None  # never persist/alert a live NO_TRADE -- nothing to say yet

    regime = MarketRegimeDetector().detect(combined)
    expiry = forming.open_time + timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    reasoning = (
        f"{result.reasoning} [LIVE/INTRABAR: detected while the {timeframe} candle was still "
        f"forming, via CoinDCX's public live price feed -- {forming.tick_count} tick(s) seen this bar. "
        f"Will be confirmed (or superseded) when the candle closes and the scheduled evaluation runs; "
        f"no duplicate alert will fire for the same candle either way.]"
    )
    return await _persist_new_signal(session, result, symbol, forming.open_time, regime, reasoning)
