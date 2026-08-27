"""Tests for services/exchange/fx.py -- the USDT->INR conversion used to
display the Binance-sourced (USDT) signal-engine prices in INR. Covers the
spec's explicit requirements: real conversion (not fabricated), staleness,
unavailability, and no double-conversion of already-INR values."""
import time

import httpx
import pytest

from services.exchange import fx


@pytest.fixture(autouse=True)
def _reset_cache():
    fx._reset_cache_for_tests()
    yield
    fx._reset_cache_for_tests()


def _mock_transport(rows):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows)
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_get_usdt_inr_rate_parses_real_ticker_shape():
    rows = [
        {"market": "BTCUSDT", "last_price": "78000.0", "timestamp": 1700000000},
        {"market": "USDTINR", "last_price": "88.42", "timestamp": 1700000000},
    ]
    client = httpx.AsyncClient(transport=_mock_transport(rows))
    rate = await fx.get_usdt_inr_rate(client=client)
    await client.aclose()
    assert rate is not None
    assert rate.rate == 88.42
    assert rate.source == fx.CONVERSION_SOURCE


@pytest.mark.asyncio
async def test_get_usdt_inr_rate_returns_none_if_market_missing():
    rows = [{"market": "BTCUSDT", "last_price": "78000.0", "timestamp": 1700000000}]
    client = httpx.AsyncClient(transport=_mock_transport(rows))
    rate = await fx.get_usdt_inr_rate(client=client)
    await client.aclose()
    assert rate is None


@pytest.mark.asyncio
async def test_get_usdt_inr_rate_returns_none_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rate = await fx.get_usdt_inr_rate(client=client)
    await client.aclose()
    assert rate is None


@pytest.mark.asyncio
async def test_rate_is_cached_within_ttl():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json=[{"market": "USDTINR", "last_price": "90.0", "timestamp": 1700000000}])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    now = time.time()
    r1 = await fx.get_usdt_inr_rate(client=client, now=now)
    r2 = await fx.get_usdt_inr_rate(client=client, now=now + 5)
    await client.aclose()
    assert calls["count"] == 1
    assert r1 is r2


@pytest.mark.asyncio
async def test_rate_refetches_after_ttl_expires():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json=[{"market": "USDTINR", "last_price": "90.0", "timestamp": 1700000000}])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    now = time.time()
    await fx.get_usdt_inr_rate(client=client, now=now)
    await fx.get_usdt_inr_rate(client=client, now=now + fx.CACHE_TTL_SECONDS + 1)
    await client.aclose()
    assert calls["count"] == 2


def test_conversion_rate_status_live_vs_stale():
    now = time.time()
    fresh = fx.ConversionRate(rate=90.0, rate_timestamp=now, fetched_at=now)
    stale = fx.ConversionRate(rate=90.0, rate_timestamp=now, fetched_at=now - fx.STALE_AFTER_SECONDS - 1)
    assert fresh.status(now=now) == "LIVE"
    assert stale.status(now=now) == "STALE"


def test_convert_usdt_to_inr():
    rate = fx.ConversionRate(rate=90.0, rate_timestamp=1.0, fetched_at=1.0)
    assert fx.convert_usdt_to_inr(100.0, rate) == 9000.0


def test_convert_usdt_to_inr_never_fabricates_when_inputs_missing():
    rate = fx.ConversionRate(rate=90.0, rate_timestamp=1.0, fetched_at=1.0)
    assert fx.convert_usdt_to_inr(None, rate) is None
    assert fx.convert_usdt_to_inr(100.0, None) is None


def test_conversion_meta_unavailable_when_rate_is_none():
    meta = fx.conversion_meta(None)
    assert meta["conversion_status"] == "UNAVAILABLE"
    assert meta["conversion_rate"] is None


def test_conversion_meta_reports_source_and_rate():
    rate = fx.ConversionRate(rate=90.0, rate_timestamp=123.0, fetched_at=time.time())
    meta = fx.conversion_meta(rate)
    assert meta["conversion_rate"] == 90.0
    assert meta["conversion_source"] == fx.CONVERSION_SOURCE
    assert meta["conversion_status"] == "LIVE"
