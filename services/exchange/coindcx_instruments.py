"""CoinDCX futures instrument metadata (Live Futures Auto-Trading V1,
Contract Audit V2 -- Phase 2). Public, unauthenticated endpoints, verified
against the OFFICIAL docs at https://docs.coindcx.com/ (fetched directly
2026-08-28, cross-checked against a real live response for B-BTC_USDT,
B-ETH_USDT, B-SOL_USDT, B-XRP_USDT on the same date -- see
docs/coindcx_futures_contract_audit_v2.md):

  GET /exchange/v1/derivatives/futures/data/active_instruments
      ?margin_currency_short_name[]={INR|USDT}
      -> plain list[str] of instrument pairs, no metadata.

  GET /exchange/v1/derivatives/futures/data/instrument
      ?pair={instrument}&margin_currency_short_name={INR|USDT}
      -> {"instrument": {...}} -- THE authoritative source for every
      constraint Section 8 of the live-execution spec needs:
      quantity_increment (step size), min_quantity, max_quantity,
      min_notional, price_increment (tick size), min_price, max_price,
      max_leverage_long, max_leverage_short, exit_only, status.

Neither endpoint sends X-AUTH-APIKEY/X-AUTH-SIGNATURE in CoinDCX's own
code samples -- confirmed genuinely public, no credentials needed or used
here. This module makes GET requests only; it has no capability to place,
modify, or cancel anything (enforced by
tests/unit/test_no_order_placement_capability.py).
"""
import time
from dataclasses import dataclass
from typing import Optional

import httpx
import structlog

logger = structlog.get_logger()

COINDCX_API_BASE = "https://api.coindcx.com"

# CoinDCX's own docs give no explicit cache-freshness guidance for this
# endpoint; instrument constraints (precision, leverage caps, min/max
# quantity) change far less often than price, so a conservative 5-minute
# TTL avoids hammering the endpoint on every candidate while still
# re-fetching well within any single trading session.
CACHE_TTL_SECONDS = 300


@dataclass
class InstrumentMetadata:
    """Every field is copied verbatim from CoinDCX's own documented
    response -- this module never invents or infers a constraint CoinDCX
    itself did not report."""
    pair: str
    status: str
    kind: str
    settle_currency_short_name: str
    quote_currency_short_name: str
    position_currency_short_name: str
    underlying_currency_short_name: str
    margin_currency_short_name: str
    max_leverage_long: float
    max_leverage_short: float
    price_increment: float
    quantity_increment: float
    min_trade_size: float
    min_price: float
    max_price: float
    min_quantity: float
    max_quantity: float
    min_notional: float
    max_notional: float
    exit_only: bool
    order_types: list
    time_in_force_options: list
    fetched_at: float

    def supports_leverage(self, leverage: int) -> bool:
        return leverage <= self.max_leverage_long and leverage <= self.max_leverage_short

    def is_stale(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return (now - self.fetched_at) > CACHE_TTL_SECONDS


_cache: dict[str, InstrumentMetadata] = {}


def _reset_cache_for_tests() -> None:
    global _cache
    _cache = {}


def _parse_instrument(raw: dict, now: float) -> InstrumentMetadata:
    """Every field is required -- a response missing a field CoinDCX's own
    docs document as always-present is treated as a malformed response
    (KeyError propagates), never silently defaulted to a guessed value."""
    return InstrumentMetadata(
        pair=raw["pair"], status=raw["status"], kind=raw["kind"],
        settle_currency_short_name=raw["settle_currency_short_name"],
        quote_currency_short_name=raw["quote_currency_short_name"],
        position_currency_short_name=raw["position_currency_short_name"],
        underlying_currency_short_name=raw["underlying_currency_short_name"],
        margin_currency_short_name=raw["margin_currency_short_name"],
        max_leverage_long=float(raw["max_leverage_long"]), max_leverage_short=float(raw["max_leverage_short"]),
        price_increment=float(raw["price_increment"]), quantity_increment=float(raw["quantity_increment"]),
        min_trade_size=float(raw["min_trade_size"]), min_price=float(raw["min_price"]), max_price=float(raw["max_price"]),
        min_quantity=float(raw["min_quantity"]), max_quantity=float(raw["max_quantity"]),
        min_notional=float(raw["min_notional"]), max_notional=float(raw.get("max_notional") or 0.0),
        exit_only=bool(raw["exit_only"]), order_types=raw.get("order_types") or [], time_in_force_options=raw.get("time_in_force_options") or [],
        fetched_at=now,
    )


async def get_instrument_metadata(
    pair: str, margin_currency: str = "USDT", client: Optional[httpx.AsyncClient] = None, force_refresh: bool = False,
) -> Optional[InstrumentMetadata]:
    """Returns the cached metadata if fresh, otherwise fetches from the
    real public endpoint. Returns None -- never fabricated data -- if the
    instrument doesn't exist or the endpoint is unreachable."""
    cache_key = f"{pair}:{margin_currency}"
    cached = _cache.get(cache_key)
    if cached is not None and not force_refresh and not cached.is_stale():
        return cached

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        resp = await client.get(
            f"{COINDCX_API_BASE}/exchange/v1/derivatives/futures/data/instrument",
            params={"pair": pair, "margin_currency_short_name": margin_currency},
        )
        resp.raise_for_status()
        body = resp.json()
        raw = body.get("instrument")
        if raw is None:
            logger.warning("CoinDCX instrument response missing 'instrument' key", pair=pair)
            return None
        metadata = _parse_instrument(raw, time.time())
        _cache[cache_key] = metadata
        return metadata
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as e:
        logger.warning("Failed to fetch CoinDCX instrument metadata", pair=pair, error=str(e))
        return None
    finally:
        if owns_client:
            await client.aclose()


async def get_active_instruments(margin_currency: str = "USDT", client: Optional[httpx.AsyncClient] = None) -> list[str]:
    """Returns [] -- never a fabricated list -- if the endpoint is
    unreachable."""
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        resp = await client.get(
            f"{COINDCX_API_BASE}/exchange/v1/derivatives/futures/data/active_instruments",
            params={"margin_currency_short_name[]": margin_currency},
        )
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Failed to fetch CoinDCX active instruments", error=str(e))
        return []
    finally:
        if owns_client:
            await client.aclose()
