"""Phase 5: CoinDCX provider tests, against mocked HTTP responses shaped
exactly like the real documented payloads (docs/coindcx_api_findings.md)
-- never a live connection. Per the Phase 5 brief, the test suite must
work with zero real credentials configured.
"""
import hashlib
import hmac
import json

import httpx
import pytest

from services.exchange.coindcx import (
    CoinDCXMarketDataProvider,
    CoinDCXReadOnlyAccountProvider,
    CoinDCXAuthError,
    normalize_symbol,
)


def test_normalize_symbol_uses_coindcx_futures_format():
    # Default margin currency reflects the real connected account (INR,
    # confirmed 2026-08-26) -- normalize_symbol always uses the configured/
    # explicit margin currency, never the suffix of the input symbol string
    # (AlphaOne's "BTC/USDT" naming is an internal convention, not a claim
    # about which CoinDCX wallet is in use).
    assert normalize_symbol("BTC/USDT") == "B-BTC_INR"
    assert normalize_symbol("eth/usdt") == "B-ETH_INR"
    assert normalize_symbol("BTC/USDT", margin_currency="USDT") == "B-BTC_USDT"


def _market_provider(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return CoinDCXMarketDataProvider(client=client), client


def _account_provider(handler, api_key="key", api_secret="secret"):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return CoinDCXReadOnlyAccountProvider(api_key=api_key, api_secret=api_secret, client=client), client


@pytest.mark.asyncio
async def test_get_pairs_returns_real_shaped_instrument_list():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "active_instruments" in str(request.url)
        return httpx.Response(200, json=["B-BTC_USDT", "B-ETH_USDT"])

    provider, client = _market_provider(handler)
    pairs = await provider.get_pairs()
    assert pairs == ["B-BTC_USDT", "B-ETH_USDT"]
    await client.aclose()


@pytest.mark.asyncio
async def test_get_ticker_extracts_by_normalized_symbol():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ts": 1720429586580, "vs": 1,
            "prices": {"B-BTC_USDT": {"mp": 65000.5, "ls": 64990.0, "h": 66000, "l": 64000}},
        })

    provider, client = _market_provider(handler)
    ticker = await provider.get_ticker("BTC/USDT")
    assert ticker["mp"] == 65000.5
    await client.aclose()


@pytest.mark.asyncio
async def test_get_candles_hits_rest_endpoint_with_pcode_f():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"s": "ok", "data": [{"open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10, "time": 123}]})

    provider, client = _market_provider(handler)
    candles = await provider.get_candles("BTC/USDT", resolution="60", from_ts=100, to_ts=200)
    assert seen["params"]["pcode"] == "f"
    assert seen["params"]["pair"] == "B-BTC_INR"
    assert len(candles) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_account_provider_without_credentials_reports_not_configured():
    provider = CoinDCXReadOnlyAccountProvider()  # no key/secret
    status = await provider.get_connection_status()
    assert status["status"] == "NOT_CONFIGURED"

    balance = await provider.get_balance()
    assert balance["status"] == "NOT_CONFIGURED"
    assert balance["total_equity"] is None

    assert await provider.get_open_positions() == []
    assert await provider.get_trade_history() == []


@pytest.mark.asyncio
async def test_signature_matches_documented_hmac_scheme():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["signature"] = request.headers["X-AUTH-SIGNATURE"]
        captured["apikey"] = request.headers["X-AUTH-APIKEY"]
        captured["body"] = request.content
        return httpx.Response(200, json=[])

    provider, client = _account_provider(handler, api_key="mykey", api_secret="mysecret")
    await provider.get_balance()

    expected_signature = hmac.new(b"mysecret", captured["body"], hashlib.sha256).hexdigest()
    assert captured["signature"] == expected_signature
    assert captured["apikey"] == "mykey"
    body = json.loads(captured["body"])
    assert "timestamp" in body
    await client.aclose()


@pytest.mark.asyncio
async def test_get_balance_uses_balance_field_as_total_equity(monkeypatch):
    """CoinDCX's own docs say to ignore the wallet's `balance` field, but a
    real connectivity test (2026-08-26, real account) showed `balance`
    holding the actual deposited amount while the margin fields were all
    zero (no open positions/orders) -- reporting $0 equity in that case
    would be misleading. Per the account owner's explicit choice, `balance`
    is treated as total_equity; used_margin is the sum of the three margin
    fields, and available_balance is the difference."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "id": "w1", "currency_short_name": "INR",
            "balance": "1600.0", "locked_balance": "10.0", "cross_order_margin": "2.0", "cross_user_margin": "3.0",
        }])

    provider, client = _account_provider(handler)
    balance = await provider.get_balance()
    assert balance["status"] == "OK"
    assert balance["total_equity"] == 1600.0
    assert balance["used_margin"] == 15.0  # 10 + 2 + 3
    assert balance["available_balance"] == pytest.approx(1585.0)  # 1600 - 15
    await client.aclose()


@pytest.mark.asyncio
async def test_get_balance_only_matches_the_configured_margin_currency_wallet():
    """A wallet entry for a currency other than DEFAULT_MARGIN_CURRENCY
    must never be picked up silently -- confirms the account's real
    INR-only wallet setup wouldn't be masked by a stray USDT entry."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"id": "w1", "currency_short_name": "USDT", "balance": "999999.0",
             "locked_balance": "0", "cross_order_margin": "0", "cross_user_margin": "0"},
        ])

    provider, client = _account_provider(handler)
    balance = await provider.get_balance()
    assert balance["status"] == "UNAVAILABLE"  # no INR wallet entry present
    assert balance["total_equity"] is None
    await client.aclose()


@pytest.mark.asyncio
async def test_get_open_positions_filters_flat_and_computes_unrealized_pnl():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"id": "p1", "pair": "B-BTC_USDT", "active_pos": 0.0, "avg_price": 0.0, "mark_price": 0.0,
             "liquidation_price": 0.0, "leverage": 10.0, "locked_margin": 0.0, "margin_type": "crossed"},
            {"id": "p2", "pair": "B-ETH_USDT", "active_pos": 2.0, "avg_price": 100.0, "mark_price": 110.0,
             "liquidation_price": 50.0, "leverage": 5.0, "locked_margin": 40.0, "margin_type": "isolated"},
            {"id": "p3", "pair": "B-SOL_USDT", "active_pos": -3.0, "avg_price": 20.0, "mark_price": 18.0,
             "liquidation_price": 30.0, "leverage": 5.0, "locked_margin": 12.0, "margin_type": "isolated"},
        ])

    provider, client = _account_provider(handler)
    positions = await provider.get_open_positions()

    assert len(positions) == 2  # the flat B-BTC_USDT position is excluded
    eth = next(p for p in positions if p["symbol"] == "B-ETH_USDT")
    assert eth["side"] == "LONG"
    assert eth["quantity"] == 2.0
    assert eth["unrealized_pnl"] == pytest.approx((110.0 - 100.0) * 2.0)

    sol = next(p for p in positions if p["symbol"] == "B-SOL_USDT")
    assert sol["side"] == "SHORT"
    assert sol["quantity"] == 3.0
    assert sol["unrealized_pnl"] == pytest.approx((18.0 - 20.0) * -3.0)  # positive PnL on a profitable short
    await client.aclose()


@pytest.mark.asyncio
async def test_get_trade_history_defaults_a_date_range_since_it_is_mandatory_upstream():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=[])

    provider, client = _account_provider(handler)
    await provider.get_trade_history(symbol="BTC/USDT")
    assert "from_date" in seen["body"]
    assert "to_date" in seen["body"]
    assert seen["body"]["pair"] == "B-BTC_INR"
    await client.aclose()


@pytest.mark.asyncio
async def test_get_orders_queries_both_sides_since_side_is_mandatory_single_valued():
    seen_sides = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_sides.append(body["side"])
        return httpx.Response(200, json=[{"id": f"order-{body['side']}"}])

    provider, client = _account_provider(handler)
    orders = await provider.get_orders()
    assert sorted(seen_sides) == ["buy", "sell"]
    assert len(orders) == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_auth_failure_is_reported_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Invalid API key"})

    provider, client = _account_provider(handler)
    status = await provider.get_connection_status()
    assert status["status"] == "AUTH_FAILURE"
    await client.aclose()


@pytest.mark.asyncio
async def test_rate_limit_and_server_errors_are_reported_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    provider, client = _account_provider(handler)
    status = await provider.get_connection_status()
    assert status["status"] == "API_FAILURE"
    await client.aclose()


@pytest.mark.asyncio
async def test_network_failure_is_reported_as_connection_lost():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated DNS failure")

    provider, client = _account_provider(handler)
    status = await provider.get_connection_status()
    assert status["status"] == "CONNECTION_LOST"
    await client.aclose()


@pytest.mark.asyncio
async def test_direct_calls_without_credentials_raise_a_clear_auth_error():
    provider = CoinDCXReadOnlyAccountProvider()
    with pytest.raises(CoinDCXAuthError):
        await provider._post("/exchange/v1/derivatives/futures/positions", {})
