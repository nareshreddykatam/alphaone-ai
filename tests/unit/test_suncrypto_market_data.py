"""Phase 4F: SunCryptoMarketDataProvider hits the 3 real documented public
endpoints (docs.suncrypto.in) -- verified here against a mocked transport
so no test depends on network access or SunCrypto's real uptime.
"""
import httpx
import pytest

from services.exchange.suncrypto import SunCryptoMarketDataProvider


def _make_provider(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://api.suncrypto.in", transport=transport)
    return SunCryptoMarketDataProvider(client=client), client


@pytest.mark.asyncio
async def test_get_pairs_hits_the_documented_public_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=[{"ticker_id": "BTC_INR"}])

    provider, client = _make_provider(handler)
    pairs = await provider.get_pairs()
    assert seen["path"] == "/public/pairs"
    assert pairs == [{"ticker_id": "BTC_INR"}]
    await client.aclose()


@pytest.mark.asyncio
async def test_get_ticker_filters_from_the_tickers_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"ticker_id": "BTC_INR", "last_price": "100"},
            {"ticker_id": "ETH_INR", "last_price": "50"},
        ])

    provider, client = _make_provider(handler)
    ticker = await provider.get_ticker("ETH_INR")
    assert ticker["last_price"] == "50"
    await client.aclose()


@pytest.mark.asyncio
async def test_get_historical_trades_passes_ticker_id_as_query_param():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json=[{"price": "100", "qty": "1"}])

    provider, client = _make_provider(handler)
    trades = await provider.get_historical_trades("BTC_INR")
    assert seen["query"] == {"ticker_id": "BTC_INR"}
    assert trades == [{"price": "100", "qty": "1"}]
    await client.aclose()


@pytest.mark.asyncio
async def test_raises_on_http_error_rather_than_returning_fabricated_data():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    provider, client = _make_provider(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await provider.get_pairs()
    await client.aclose()
