"""USDT -> INR conversion for display purposes only (never for account
math -- the real CoinDCX account is INR-margined and its balance/position
values are already native INR, see services/exchange/coindcx.py).

AlphaOne's signal engine (Phases 1-3) was built entirely on Binance
BTC/USDT candles and still reports prices in USDT. The user wants every
UI/Telegram price shown in INR. Rather than inventing a synthetic
USDT->USD->INR chain (which would need a USD reference with no real data
source behind it), this module converts directly using CoinDCX's own
public USDT/INR spot ticker -- a real, directly-tradeable CoinDCX market,
and therefore a verifiable, documented conversion source in its own
right (see docs/coindcx_api_findings.md).

If the rate can't be fetched, callers must show "INR conversion
unavailable" -- this module never fabricates a rate or a converted value.
"""
import time
from dataclasses import dataclass
from typing import Optional

import httpx
import structlog

logger = structlog.get_logger()

COINDCX_PUBLIC_TICKER_URL = "https://public.coindcx.com/exchange/ticker"
USDT_INR_MARKET = "USDTINR"
CONVERSION_SOURCE = "CoinDCX USDT/INR spot ticker"

STALE_AFTER_SECONDS = 300
CACHE_TTL_SECONDS = 30


@dataclass
class ConversionRate:
    rate: float  # 1 USDT = `rate` INR
    rate_timestamp: float  # epoch seconds CoinDCX reported for this price
    fetched_at: float  # epoch seconds AlphaOne fetched it
    source: str = CONVERSION_SOURCE

    def status(self, now: Optional[float] = None) -> str:
        now = time.time() if now is None else now
        return "STALE" if (now - self.fetched_at) > STALE_AFTER_SECONDS else "LIVE"


_cache: Optional[ConversionRate] = None


def _reset_cache_for_tests() -> None:
    global _cache
    _cache = None


async def get_usdt_inr_rate(
    client: Optional[httpx.AsyncClient] = None, now: Optional[float] = None
) -> Optional[ConversionRate]:
    """Returns the cached rate if fetched within CACHE_TTL_SECONDS, else
    fetches fresh from CoinDCX's public ticker. Returns None -- never a
    fabricated rate -- if the ticker is unreachable or has no USDTINR row."""
    global _cache
    now = time.time() if now is None else now
    if _cache is not None and (now - _cache.fetched_at) < CACHE_TTL_SECONDS:
        return _cache

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        resp = await client.get(COINDCX_PUBLIC_TICKER_URL)
        resp.raise_for_status()
        rows = resp.json()
        for row in rows:
            if row.get("market") == USDT_INR_MARKET:
                rate = float(row["last_price"])
                rate_ts = float(row.get("timestamp", now))
                _cache = ConversionRate(rate=rate, rate_timestamp=rate_ts, fetched_at=now)
                return _cache
        logger.warning("CoinDCX ticker response missing USDTINR market")
        return None
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        logger.warning("Failed to fetch USDT/INR conversion rate", error=str(e))
        return None
    finally:
        if owns_client:
            await client.aclose()


def convert_usdt_to_inr(amount_usdt: Optional[float], rate: Optional[ConversionRate]) -> Optional[float]:
    """Never guesses: returns None if either input is missing."""
    if amount_usdt is None or rate is None:
        return None
    return amount_usdt * rate.rate


def conversion_meta(rate: Optional[ConversionRate]) -> dict:
    """The freshness/timestamp/source envelope required alongside every
    converted value."""
    if rate is None:
        return {
            "conversion_rate": None,
            "conversion_timestamp": None,
            "conversion_source": None,
            "conversion_status": "UNAVAILABLE",
        }
    return {
        "conversion_rate": rate.rate,
        "conversion_timestamp": rate.rate_timestamp,
        "conversion_source": rate.source,
        "conversion_status": rate.status(),
    }
