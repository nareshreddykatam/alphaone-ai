from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema import get_db
from database.schema.models import Candle, Signal
from services.exchange.fx import get_usdt_inr_rate, convert_usdt_to_inr, conversion_meta

router = APIRouter()


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
    rate = await get_usdt_inr_rate() if rows else None

    return {
        "candles": [
            {
                "time": int(r.timestamp.timestamp()),
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
                "time": int(s.timestamp.timestamp()),
                "signal_type": s.signal_type,
                "quality": s.quality,
                "entry_price": s.entry_price,
                "entry_price_inr": convert_usdt_to_inr(s.entry_price, rate),
            }
            for s in signals
        ],
        **conversion_meta(rate),
    }
