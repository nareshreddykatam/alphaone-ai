"""services/exchange/coindcx_instruments.py -- CoinDCX futures instrument
metadata (Contract Audit V2, Phase 2). Tests use response shapes taken
verbatim from a REAL live GET against api.coindcx.com performed during
this task's own audit (2026-08-28, B-BTC_USDT/USDT), never a guessed
shape. Both endpoints are genuinely public -- no auth header is asserted
or required, matching CoinDCX's own unauthenticated code samples.
"""
import httpx
import pytest

from services.exchange import coindcx_instruments as mod
from services.exchange.coindcx_instruments import get_active_instruments, get_instrument_metadata

# Real response captured live for B-BTC_USDT/USDT during this audit.
REAL_BTC_INSTRUMENT_RESPONSE = {
    "instrument": {
        "settle_currency_short_name": "USDT", "quote_currency_short_name": "USDT",
        "position_currency_short_name": "BTC", "underlying_currency_short_name": "BTC",
        "status": "active", "pair": "B-BTC_USDT", "kind": "perpetual", "settlement": "never",
        "max_leverage_long": 20.0, "max_leverage_short": 20.0, "unit_contract_value": 1.0,
        "price_increment": 0.1, "quantity_increment": 0.001, "min_trade_size": 0.001,
        "min_price": 584.64, "max_price": 791341.0, "min_quantity": 0.001, "max_quantity": 950.0,
        "min_notional": 60.0, "maker_fee": 0.0236, "taker_fee": 0.059, "safety_percentage": 1.5,
        "quanto_to_settle_multiplier": 1.0, "is_inverse": False, "is_quanto": False,
        "allow_post_only": False, "allow_hidden": False, "max_market_order_quantity": 120.0,
        "funding_frequency": 8, "max_notional": 0.0, "expiry_time": 2548143000000, "exit_only": False,
        "multiplier_up": 4.0, "multiplier_down": 4.0, "liquidation_fee": 1.0,
        "time_in_force_options": ["good_till_cancel", "immediate_or_cancel", "fill_or_kill"],
        "order_types": ["limit_order", "market_order", "stop_limit", "take_profit_limit", "stop_market", "take_profit_market"],
        "margin_currency_short_name": "USDT",
    },
}


@pytest.fixture(autouse=True)
def _reset_cache():
    mod._reset_cache_for_tests()
    yield
    mod._reset_cache_for_tests()


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_get_instrument_metadata_parses_the_real_response_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "pair=B-BTC_USDT" in str(request.url)
        assert "X-AUTH-APIKEY" not in request.headers  # genuinely public -- no auth sent
        return httpx.Response(200, json=REAL_BTC_INSTRUMENT_RESPONSE)

    client = _client(handler)
    result = await get_instrument_metadata("B-BTC_USDT", margin_currency="USDT", client=client)
    await client.aclose()
    assert result is not None
    assert result.pair == "B-BTC_USDT"
    assert result.status == "active"
    assert result.max_leverage_long == 20.0
    assert result.quantity_increment == 0.001
    assert result.min_quantity == 0.001
    assert result.min_notional == 60.0
    assert result.price_increment == 0.1
    assert result.exit_only is False


async def test_supports_leverage_true_when_within_both_long_and_short_caps():
    def handler(request):
        return httpx.Response(200, json=REAL_BTC_INSTRUMENT_RESPONSE)
    client = _client(handler)
    result = await get_instrument_metadata("B-BTC_USDT", client=client)
    await client.aclose()
    assert result.supports_leverage(10) is True
    assert result.supports_leverage(20) is True
    assert result.supports_leverage(21) is False


async def test_supports_leverage_false_for_a_low_leverage_cap_instrument():
    """Mirrors the REAL SOL/USDT response captured live during this audit
    (max_leverage_long=5.0) -- a genuinely important finding: SOL/USDT
    does not support the required 10x leverage on CoinDCX today."""
    sol_response = {**REAL_BTC_INSTRUMENT_RESPONSE, "instrument": {**REAL_BTC_INSTRUMENT_RESPONSE["instrument"], "pair": "B-SOL_USDT", "max_leverage_long": 5.0, "max_leverage_short": 5.0}}
    def handler(request):
        return httpx.Response(200, json=sol_response)
    client = _client(handler)
    result = await get_instrument_metadata("B-SOL_USDT", client=client)
    await client.aclose()
    assert result.supports_leverage(10) is False


async def test_missing_instrument_key_returns_none_not_a_crash():
    def handler(request):
        return httpx.Response(200, json={"error": "not found"})
    client = _client(handler)
    result = await get_instrument_metadata("B-NOTREAL_USDT", client=client)
    await client.aclose()
    assert result is None


async def test_http_failure_returns_none_not_a_fabricated_metadata_object():
    def handler(request):
        return httpx.Response(500, json={"error": "server error"})
    client = _client(handler)
    result = await get_instrument_metadata("B-BTC_USDT", client=client)
    await client.aclose()
    assert result is None


async def test_malformed_response_missing_a_required_field_returns_none():
    broken = {"instrument": {k: v for k, v in REAL_BTC_INSTRUMENT_RESPONSE["instrument"].items() if k != "quantity_increment"}}
    def handler(request):
        return httpx.Response(200, json=broken)
    client = _client(handler)
    result = await get_instrument_metadata("B-BTC_USDT", client=client)
    await client.aclose()
    assert result is None


async def test_result_is_cached_and_not_refetched_within_the_ttl():
    call_count = {"n": 0}
    def handler(request):
        call_count["n"] += 1
        return httpx.Response(200, json=REAL_BTC_INSTRUMENT_RESPONSE)
    client = _client(handler)
    first = await get_instrument_metadata("B-BTC_USDT", client=client)
    second = await get_instrument_metadata("B-BTC_USDT", client=client)
    await client.aclose()
    assert call_count["n"] == 1
    assert first is second or first == second


async def test_force_refresh_bypasses_the_cache():
    call_count = {"n": 0}
    def handler(request):
        call_count["n"] += 1
        return httpx.Response(200, json=REAL_BTC_INSTRUMENT_RESPONSE)
    client = _client(handler)
    await get_instrument_metadata("B-BTC_USDT", client=client)
    await get_instrument_metadata("B-BTC_USDT", client=client, force_refresh=True)
    await client.aclose()
    assert call_count["n"] == 2


async def test_get_active_instruments_returns_the_real_plain_list_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "active_instruments" in str(request.url)
        assert "X-AUTH-APIKEY" not in request.headers
        return httpx.Response(200, json=["B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT", "B-XRP_USDT"])
    client = _client(handler)
    result = await get_active_instruments(margin_currency="USDT", client=client)
    await client.aclose()
    assert result == ["B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT", "B-XRP_USDT"]


async def test_get_active_instruments_returns_empty_list_on_failure_not_a_fabricated_list():
    def handler(request):
        return httpx.Response(500, json={"error": "server error"})
    client = _client(handler)
    result = await get_active_instruments(client=client)
    await client.aclose()
    assert result == []


def test_is_stale_reflects_the_cache_ttl():
    metadata = mod.InstrumentMetadata(
        pair="B-BTC_USDT", status="active", kind="perpetual",
        settle_currency_short_name="USDT", quote_currency_short_name="USDT",
        position_currency_short_name="BTC", underlying_currency_short_name="BTC", margin_currency_short_name="USDT",
        max_leverage_long=20.0, max_leverage_short=20.0, price_increment=0.1, quantity_increment=0.001,
        min_trade_size=0.001, min_price=1.0, max_price=1_000_000.0, min_quantity=0.001, max_quantity=950.0,
        min_notional=60.0, max_notional=0.0, exit_only=False, order_types=[], time_in_force_options=[], fetched_at=1000.0,
    )
    assert metadata.is_stale(now=1000.0 + mod.CACHE_TTL_SECONDS + 1) is True
    assert metadata.is_stale(now=1000.0 + 1) is False
