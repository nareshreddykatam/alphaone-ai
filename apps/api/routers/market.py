import calendar
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema import get_db
from database.schema.models import Candle, ConnectionState, Signal
from services.exchange.fx import get_usdt_inr_rate, convert_usdt_to_inr, conversion_meta
from services.market_data.live_state import live_candle_aggregators, market_ws

router = APIRouter()


def _epoch_seconds(dt: datetime) -> int:
    """Every DateTime column in this codebase is stored as a naive UTC
    value (Column(DateTime), no timezone=True; see database/schema/models.py
    -- all defaults use datetime.utcnow). datetime.timestamp() on a naive
    value silently assumes the LOCAL system timezone, not UTC -- on any
    machine whose OS timezone isn't UTC (confirmed during this audit: this
    dev machine is IST, UTC+5:30) that shifts every chart bar's reported
    time by the local UTC offset (verified: candle.open_time.timestamp()
    for a true 08:00:00 UTC bucket returned the epoch for 02:30:00 UTC on
    this machine). calendar.timegm() reads the naive value's wall-clock
    fields as UTC directly, with no local-timezone conversion, which is
    what a Candle/Signal's naive UTC timestamp actually requires."""
    return calendar.timegm(dt.timetuple())


@router.get("/candles")
async def get_candles(
    symbol: str = "BTC/USDT", timeframe: str = "4h", limit: int = 300, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Candle)
        .where(Candle.symbol == symbol, Candle.timeframe == timeframe, Candle.quality_status == "valid")
        .order_by(Candle.timestamp.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    rows.reverse()

    signals_result = await db.execute(
        select(Signal)
        .where(Signal.symbol == symbol, Signal.signal_type.in_(["LONG", "SHORT"]))
        .order_by(Signal.timestamp.desc())
        .limit(50)
    )
    signals = signals_result.scalars().all()

    # Candles are Binance-sourced BTC/USDT (USDT-denominated). Converted
    # to INR here using a single current CoinDCX USDT/INR rate applied
    # uniformly across the whole series -- there is no documented historical
    # USDT/INR rate history to apply per-candle, so this intentionally does
    # not attempt to reconstruct one. conversion_status/timestamp/source
    # tell the frontend how fresh (and how approximate for older candles)
    # this is; frontend shows "INR conversion unavailable" if rate is None.
    #
    # Every timeframe the aggregator registry supports (1m/5m/15m/1h/4h/1d --
    # see services/market_data/live_state.py) gets a correct live forming
    # candle here, not just 4h: only the SIGNAL engine stays 4h-only
    # (services/signal_engine/live_breakout.py is never pointed at any other
    # entry in the registry), this display endpoint is timeframe-agnostic.
    aggregator = live_candle_aggregators.get(timeframe)
    live_feed_is_live = market_ws.connection_status() == ConnectionState.LIVE and market_ws.state.last_price_usdt is not None
    needs_rate = bool(rows) or (symbol == "BTC/USDT" and aggregator is not None and live_feed_is_live)
    rate = await get_usdt_inr_rate() if needs_rate else None

    # Live forming (not-yet-closed) candle -- fixes a real, measured gap:
    # the chart's "latest" bar was previously always the last COMPLETED
    # candle, which can sit up to a full timeframe period stale (measured
    # up to 5h40m for 4h during audit). Only computed for a timeframe the
    # registry actually tracks and only while the live feed is genuinely
    # LIVE -- never guessed, never shown as live when it isn't. Feeding the
    # tick here (not just reading `.current`) is what keeps the forming
    # candle up to date on every chart poll even if the scheduler's own 30s
    # live_breakout_job tick (which only ever touches the "4h" entry)
    # hasn't landed yet.
    forming_candle = None
    if symbol == "BTC/USDT" and aggregator is not None and live_feed_is_live:
        candle = aggregator.on_tick(market_ws.state.last_price_usdt, market_ws.state.received_at)
        forming_candle = {
            "time": _epoch_seconds(candle.open_time),
            "open": candle.open, "high": candle.high, "low": candle.low, "close": candle.close,
            "open_inr": convert_usdt_to_inr(candle.open, rate),
            "high_inr": convert_usdt_to_inr(candle.high, rate),
            "low_inr": convert_usdt_to_inr(candle.low, rate),
            "close_inr": convert_usdt_to_inr(candle.close, rate),
            "tick_count": candle.tick_count,
        }

    return {
        "candles": [
            {
                "time": _epoch_seconds(r.timestamp),
                "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume,
                "open_inr": convert_usdt_to_inr(r.open, rate),
                "high_inr": convert_usdt_to_inr(r.high, rate),
                "low_inr": convert_usdt_to_inr(r.low, rate),
                "close_inr": convert_usdt_to_inr(r.close, rate),
            }
            for r in rows
        ],
        "markers": [
            {
                "time": _epoch_seconds(s.timestamp),
                "signal_type": s.signal_type,
                "quality": s.quality,
                "entry_price": s.entry_price,
                "entry_price_inr": convert_usdt_to_inr(s.entry_price, rate),
            }
            for s in signals
        ],
        "forming_candle": forming_candle,
        "market_data_status": market_ws.connection_status().value,
        **conversion_meta(rate),
    }
