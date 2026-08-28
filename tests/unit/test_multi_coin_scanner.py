"""Multi-Coin AI Futures System, Phase 4: the scanner must verify real
CoinDCX instrument availability (never assume a symbol is tradeable),
apply real liquidity/freshness eligibility checks, and must never
fabricate a score for a symbol that has no historical data or validated
strategy -- only BTC/USDT is SCORED today; every other symbol in the
whitelist must come back NOT_VALIDATED/INELIGIBLE_*/INSTRUMENT_UNAVAILABLE,
never a made-up direction/confidence."""
import time

import httpx
import pytest

from services.scanner.multi_coin import (
    _to_coindcx_instrument, check_instrument_availability, scan_symbol, scan_whitelist,
    InstrumentAvailability, DEFAULT_WHITELIST, RESEARCHED_SYMBOLS,
    MIN_24H_VOLUME_USDT, MAX_TICK_AGE_SECONDS,
)


def test_instrument_naming_uses_usdt_margin_not_the_account_inr_default():
    assert _to_coindcx_instrument("BTC/USDT") == "B-BTC_USDT"
    assert _to_coindcx_instrument("ETH/USDT") == "B-ETH_USDT"
    assert _to_coindcx_instrument("sol/usdt") == "B-SOL_USDT"


async def test_check_instrument_availability_real_call_shape(monkeypatch):
    now_ms = time.time() * 1000

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"prices": {
                "B-BTC_USDT": {"ls": 80000.0, "fr": 0.0001, "mp": 80001.0, "v": 15_000_000_000.0, "ctRT": now_ms},
                # ETH deliberately absent -> unavailable
            }}

    class _FakeClient:
        async def get(self, url):
            return _FakeResponse()

        async def aclose(self):
            pass

    results = await check_instrument_availability(["BTC/USDT", "ETH/USDT"], client=_FakeClient())
    by_symbol = {r.symbol: r for r in results}
    assert by_symbol["BTC/USDT"].available is True
    assert by_symbol["BTC/USDT"].last_price == pytest.approx(80000.0)
    assert by_symbol["BTC/USDT"].is_liquid is True
    assert by_symbol["BTC/USDT"].is_fresh is True
    assert by_symbol["ETH/USDT"].available is False


async def test_check_instrument_availability_handles_real_network_failure(monkeypatch):
    class _FailingClient:
        async def get(self, url):
            raise httpx.ConnectError("boom")

        async def aclose(self):
            pass

    results = await check_instrument_availability(["BTC/USDT"], client=_FailingClient())
    assert results[0].available is False


def _liquid_fresh(symbol, instrument, last_price):
    return InstrumentAvailability(
        symbol=symbol, instrument=instrument, available=True, last_price=last_price,
        volume_24h=MIN_24H_VOLUME_USDT * 2, tick_age_seconds=1.0,
    )


async def test_only_btc_usdt_is_scored_everything_else_is_not_validated():
    assert RESEARCHED_SYMBOLS == {"BTC/USDT"}

    btc_avail = _liquid_fresh("BTC/USDT", "B-BTC_USDT", 80000.0)
    eth_avail = _liquid_fresh("ETH/USDT", "B-ETH_USDT", 2500.0)

    btc_result = await scan_symbol("BTC/USDT", btc_avail)
    eth_result = await scan_symbol("ETH/USDT", eth_avail)

    assert btc_result.status == "SCORED"
    assert eth_result.status == "NOT_VALIDATED"
    # Never a fabricated direction/confidence for an unvalidated symbol.
    assert eth_result.direction is None
    assert eth_result.confidence is None


async def test_unavailable_instrument_never_scored():
    unavailable = InstrumentAvailability(symbol="DOGE/USDT", instrument="B-DOGE_USDT", available=False)
    result = await scan_symbol("DOGE/USDT", unavailable)
    assert result.status == "INSTRUMENT_UNAVAILABLE"
    assert result.direction is None


async def test_stale_data_is_never_scored_even_if_liquid():
    stale = InstrumentAvailability(
        symbol="BTC/USDT", instrument="B-BTC_USDT", available=True, last_price=80000.0,
        volume_24h=MIN_24H_VOLUME_USDT * 2, tick_age_seconds=MAX_TICK_AGE_SECONDS + 1,
    )
    result = await scan_symbol("BTC/USDT", stale)
    assert result.status == "INELIGIBLE_STALE_DATA"
    assert result.direction is None


async def test_illiquid_instrument_is_never_scored_even_if_fresh():
    illiquid = InstrumentAvailability(
        symbol="BTC/USDT", instrument="B-BTC_USDT", available=True, last_price=80000.0,
        volume_24h=MIN_24H_VOLUME_USDT / 2, tick_age_seconds=1.0,
    )
    result = await scan_symbol("BTC/USDT", illiquid)
    assert result.status == "INELIGIBLE_LIQUIDITY"
    assert result.direction is None


async def test_missing_volume_or_freshness_data_is_treated_as_ineligible_not_assumed_ok():
    missing_data = InstrumentAvailability(symbol="BTC/USDT", instrument="B-BTC_USDT", available=True, last_price=80000.0)
    result = await scan_symbol("BTC/USDT", missing_data)
    assert result.status in ("INELIGIBLE_STALE_DATA", "INELIGIBLE_LIQUIDITY")


async def test_scan_whitelist_covers_every_default_symbol(monkeypatch):
    now_ms = time.time() * 1000

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"prices": {
                f"B-{s.split('/')[0]}_USDT": {"ls": 1.0, "v": MIN_24H_VOLUME_USDT * 2, "ctRT": now_ms}
                for s in DEFAULT_WHITELIST
            }}

    class _FakeClient:
        async def get(self, url):
            return _FakeResponse()

        async def aclose(self):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient())

    results = await scan_whitelist()
    assert len(results) == len(DEFAULT_WHITELIST)
    scored = [r for r in results if r.status == "SCORED"]
    assert [r.symbol for r in scored] == ["BTC/USDT"]
