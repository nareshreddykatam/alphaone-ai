import asyncio
from datetime import datetime, timezone
from typing import Optional
import ccxt.async_support as ccxt
import structlog

from services.market_data import (
    ExchangeBase, OHLCV, FundingRate, OpenInterest, Liquidation,
    OrderBook, OrderBookLevel, ExchangeDataUnavailable, ExchangeCapabilityUnsupported,
)

logger = structlog.get_logger()

TIMEFRAME_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h",
    "4h": "4h", "1d": "1d", "1w": "1w",
}

RETRYABLE_MAX_ATTEMPTS = 3
RETRYABLE_BASE_DELAY_SECONDS = 1.0


def _ms_to_utc_naive(ms: float) -> datetime:
    """Convert an exchange epoch-ms timestamp to a naive UTC datetime.

    Using datetime.fromtimestamp() without tz=utc would interpret the
    epoch using the local system timezone, silently corrupting every
    stored timestamp on any machine not running in UTC.
    """
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


async def _with_retries(fn, *, op_name: str, **log_context):
    last_error: Optional[Exception] = None
    for attempt in range(1, RETRYABLE_MAX_ATTEMPTS + 1):
        try:
            return await fn()
        except ccxt.NotSupported as e:
            logger.warning("Exchange does not support this call, not retrying", op=op_name, error=str(e), **log_context)
            raise ExchangeCapabilityUnsupported(f"{op_name} is not supported by this exchange: {e}") from e
        except Exception as e:  # noqa: BLE001 - ccxt raises many exception types
            last_error = e
            logger.warning(
                "Exchange call failed, retrying" if attempt < RETRYABLE_MAX_ATTEMPTS else "Exchange call failed, giving up",
                op=op_name, attempt=attempt, max_attempts=RETRYABLE_MAX_ATTEMPTS,
                error=str(e), **log_context,
            )
            if attempt < RETRYABLE_MAX_ATTEMPTS:
                await asyncio.sleep(RETRYABLE_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
    raise ExchangeDataUnavailable(f"{op_name} failed after {RETRYABLE_MAX_ATTEMPTS} attempts: {last_error}") from last_error


class BinanceExchange(ExchangeBase):
    def __init__(self, api_key: str = "", api_secret: str = "", testnet: bool = True):
        config = {
            "apiKey": api_key,
            "secret": api_secret,
            "options": {
                "defaultType": "future",
                "adjustForTimeDifference": True,
            },
            "enableRateLimit": True,
        }
        if testnet:
            config["options"]["sandboxMode"] = True

        self.exchange = ccxt.binance(config)
        self._testnet = testnet
        logger.info("Binance exchange initialized", testnet=testnet)

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: Optional[datetime] = None, limit: int = 1000
    ) -> list[OHLCV]:
        tf = TIMEFRAME_MAP.get(timeframe, timeframe)
        since_ms = int(since.timestamp() * 1000) if since else None

        raw = await _with_retries(
            lambda: self.exchange.fetch_ohlcv(symbol, tf, since=since_ms, limit=limit),
            op_name="fetch_ohlcv", symbol=symbol, timeframe=timeframe,
        )
        return [
            OHLCV(
                timestamp=_ms_to_utc_naive(row[0]),
                open=row[1],
                high=row[2],
                low=row[3],
                close=row[4],
                volume=row[5],
                timeframe=timeframe,
                symbol=symbol,
            )
            for row in raw
        ]

    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        funding = await _with_retries(
            lambda: self.exchange.fetch_funding_rate(symbol),
            op_name="fetch_funding_rate", symbol=symbol,
        )
        return FundingRate(
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
            rate=funding.get("fundingRate", 0),
            symbol=symbol,
        )

    async def fetch_funding_rate_history(
        self, symbol: str, since: Optional[datetime] = None, limit: int = 1000
    ) -> list[FundingRate]:
        since_ms = int(since.timestamp() * 1000) if since else None
        history = await _with_retries(
            lambda: self.exchange.fetch_funding_rate_history(symbol, since=since_ms, limit=limit),
            op_name="fetch_funding_rate_history", symbol=symbol,
        )
        return [
            FundingRate(
                timestamp=_ms_to_utc_naive(h["timestamp"]),
                rate=h.get("fundingRate", 0),
                symbol=symbol,
            )
            for h in history
        ]

    async def fetch_open_interest(self, symbol: str) -> OpenInterest:
        oi = await _with_retries(
            lambda: self.exchange.fetch_open_interest(symbol),
            op_name="fetch_open_interest", symbol=symbol,
        )
        return OpenInterest(
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
            value=oi.get("openInterestAmount", 0),
            symbol=symbol,
        )

    async def fetch_open_interest_history(
        self, symbol: str, timeframe: str = "1h", since: Optional[datetime] = None, limit: int = 500
    ) -> list[OpenInterest]:
        since_ms = int(since.timestamp() * 1000) if since else None
        history = await _with_retries(
            lambda: self.exchange.fetch_open_interest_history(symbol, timeframe, since=since_ms, limit=limit),
            op_name="fetch_open_interest_history", symbol=symbol,
        )
        return [
            OpenInterest(
                timestamp=_ms_to_utc_naive(h["timestamp"]),
                value=h.get("openInterestAmount", h.get("openInterest", 0)),
                symbol=symbol,
            )
            for h in history
        ]

    async def fetch_liquidations(self, symbol: str, limit: int = 100) -> list[Liquidation]:
        liqs = await _with_retries(
            lambda: self.exchange.fetch_liquidations(symbol, limit=limit),
            op_name="fetch_liquidations", symbol=symbol,
        )
        return [
            Liquidation(
                timestamp=_ms_to_utc_naive(l["timestamp"]),
                side=l.get("side", ""),
                price=l.get("price", 0),
                quantity=l.get("amount", 0),
                symbol=symbol,
            )
            for l in liqs
        ]

    async def fetch_order_book(self, symbol: str, limit: int = 20) -> OrderBook:
        ob = await _with_retries(
            lambda: self.exchange.fetch_order_book(symbol, limit),
            op_name="fetch_order_book", symbol=symbol,
        )
        return OrderBook(
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
            bids=[OrderBookLevel(price=b[0], quantity=b[1]) for b in ob.get("bids", [])],
            asks=[OrderBookLevel(price=a[0], quantity=a[1]) for a in ob.get("asks", [])],
            symbol=symbol,
        )

    async def fetch_ticker(self, symbol: str) -> dict:
        return await _with_retries(
            lambda: self.exchange.fetch_ticker(symbol),
            op_name="fetch_ticker", symbol=symbol,
        )

    async def close(self):
        await self.exchange.close()
