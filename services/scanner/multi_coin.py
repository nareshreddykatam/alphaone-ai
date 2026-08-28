"""Multi-coin futures scanner architecture (AI Trading V1, Phase 10).

Prepares the architecture to scan multiple CoinDCX futures markets --
deliberately NOT scanning hundreds of coins. A small, configurable
whitelist, and instrument availability is verified against CoinDCX's real
public API (never assumed) before a symbol is scanned at all.

Honest scope limit, not hidden: AlphaOne's historical Candle data,
research database, and every validated strategy (S05/S06/V3_KAMA_TREND_4H/
V3_RANGE_EXPANSION_4H) exist ONLY for BTC/USDT -- built and validated over
the multi-year research passes in reports/STRATEGY_RESEARCH_V2_RIGOROUS_REPORT.txt
and reports/STRATEGY_RESEARCH_V3_RIGOROUS_REPORT.txt. Scanning a second
symbol with real signal quality would require repeating that entire
research process for it -- out of scope for this pass. Every non-BTC
symbol below reports `NOT_VALIDATED` / `NO_HISTORICAL_DATA` rather than a
fabricated score; this module's job here is to prove the architecture
generalizes (real instrument check, real ticker data, a uniform
ScanResult shape), not to pretend a second coin has been researched.
"""
from dataclasses import dataclass
from typing import Optional

import httpx
import structlog

logger = structlog.get_logger()

COINDCX_TICKER_URL = "https://public.coindcx.com/market_data/v3/current_prices/futures/rt"

# Small, explicit whitelist -- liquid majors only. Adding a symbol here
# does NOT make it tradeable; it only makes it eligible to be scanned, and
# a scan result for anything but BTC/USDT will currently report
# NOT_VALIDATED (see module docstring).
DEFAULT_WHITELIST = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"]

# Symbols with real, validated production strategies (see
# services/signal_engine/multi_strategy.py). Everything else in the
# whitelist is scanned for instrument availability + live price only.
RESEARCHED_SYMBOLS = {"BTC/USDT"}


def _to_coindcx_instrument(symbol: str) -> str:
    """AlphaOne's canonical symbol format is "BASE/USDT" (Binance-style).
    CoinDCX's USDT-margined futures instruments are named "B-BASE_USDT" --
    confirmed against the real public API during this task's own
    production-verification pass (B-BTC_USDT, B-ETH_USDT, B-SOL_USDT,
    B-XRP_USDT all returned real live data). Deliberately NOT reusing
    services/exchange/coindcx.py: normalize_symbol()'s default margin
    currency (DEFAULT_MARGIN_CURRENCY="INR", correct for the real account's
    own INR-margined sync calls, but not what a USDT-quoted symbol here
    means) -- that mismatch is a pre-existing, currently-dead-code gap
    (get_ticker() has no real caller anywhere in this codebase today), not
    something this scanner should inherit."""
    base = symbol.split("/")[0].upper()
    return f"B-{base}_USDT"


@dataclass
class InstrumentAvailability:
    symbol: str
    instrument: str
    available: bool
    last_price: Optional[float] = None
    funding_rate: Optional[float] = None
    mark_price: Optional[float] = None


async def check_instrument_availability(symbols: list[str], client: Optional[httpx.AsyncClient] = None) -> list[InstrumentAvailability]:
    """One real call to CoinDCX's public futures ticker endpoint, checked
    against every requested symbol -- never assumes a symbol is tradeable
    without this."""
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    results = []
    try:
        resp = await client.get(COINDCX_TICKER_URL)
        resp.raise_for_status()
        prices = resp.json().get("prices", {})
        for symbol in symbols:
            instrument = _to_coindcx_instrument(symbol)
            data = prices.get(instrument)
            if data is None:
                results.append(InstrumentAvailability(symbol=symbol, instrument=instrument, available=False))
            else:
                results.append(InstrumentAvailability(
                    symbol=symbol, instrument=instrument, available=True,
                    last_price=data.get("ls"), funding_rate=data.get("fr"), mark_price=data.get("mp"),
                ))
    except httpx.HTTPError as e:
        logger.warning("CoinDCX instrument availability check failed", error=str(e))
        for symbol in symbols:
            results.append(InstrumentAvailability(symbol=symbol, instrument=_to_coindcx_instrument(symbol), available=False))
    finally:
        if owns_client:
            await client.aclose()
    return results


@dataclass
class ScanResult:
    symbol: str
    status: str  # "SCORED" (BTC/USDT today) or "NOT_VALIDATED" / "INSTRUMENT_UNAVAILABLE"
    reason: str
    instrument_available: bool
    last_price: Optional[float] = None
    direction: Optional[str] = None
    confidence: Optional[str] = None
    strategy_sources: Optional[list] = None
    regime: Optional[str] = None


async def scan_symbol(symbol: str, availability: InstrumentAvailability) -> ScanResult:
    if not availability.available:
        return ScanResult(
            symbol=symbol, status="INSTRUMENT_UNAVAILABLE",
            reason=f"{availability.instrument} did not return live data from CoinDCX's public ticker.",
            instrument_available=False,
        )

    if symbol not in RESEARCHED_SYMBOLS:
        return ScanResult(
            symbol=symbol, status="NOT_VALIDATED",
            reason=(
                f"{symbol} has a real, live CoinDCX instrument ({availability.instrument}) but no historical "
                f"candle data and no validated strategy in this system -- see this module's docstring. "
                f"Never scored without real backtested evidence."
            ),
            instrument_available=True, last_price=availability.last_price,
        )

    # BTC/USDT: the only symbol with real historical data + validated
    # strategies today. A genuine "SCORED" result reuses the SAME
    # multi_strategy_engine evaluation the live scheduler already runs --
    # never a separate, unvalidated re-implementation -- so this function
    # deliberately does not compute a score itself; callers that want a
    # live BTC/USDT decision should read the most recent Signal rows
    # (GET /api/v1/signals/latest) rather than have this scanner duplicate
    # that evaluation a second time on its own clock.
    return ScanResult(
        symbol=symbol, status="SCORED",
        reason="BTC/USDT has validated strategies -- see GET /api/v1/signals/latest for the current live decision.",
        instrument_available=True, last_price=availability.last_price,
    )


async def scan_whitelist(symbols: Optional[list[str]] = None) -> list[ScanResult]:
    symbols = symbols or DEFAULT_WHITELIST
    availabilities = await check_instrument_availability(symbols)
    return [await scan_symbol(s, a) for s, a in zip(symbols, availabilities)]
