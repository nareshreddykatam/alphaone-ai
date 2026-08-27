"""Phase 5, section 5: CoinDCX API keys/secrets must never appear in logs,
even on failure paths (auth errors, network errors, malformed responses).
Uses structlog's capture_logs() to inspect every log event emitted while
exercising failure paths with a realistic-looking secret value.
"""
import httpx
import pytest
import structlog

from services.exchange.coindcx import CoinDCXReadOnlyAccountProvider

FAKE_SECRET = "sk_live_super_secret_dO_NOT_LOG_this_9f8e7d6c5b4a"
FAKE_KEY = "ak_live_dO_NOT_LOG_this_either_1a2b3c4d"


def _provider_with(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return CoinDCXReadOnlyAccountProvider(api_key=FAKE_KEY, api_secret=FAKE_SECRET, client=client), client


def _assert_no_secret_leaked(events):
    for event in events:
        rendered = str(event)
        assert FAKE_SECRET not in rendered, f"secret leaked into log event: {event}"
        assert FAKE_KEY not in rendered, f"api key leaked into log event: {event}"


@pytest.mark.asyncio
async def test_auth_failure_does_not_log_credentials():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Invalid API key"})

    provider, client = _provider_with(handler)
    with structlog.testing.capture_logs() as logs:
        await provider.get_balance()
        await provider.get_open_positions()
        await provider.get_trade_history()

    _assert_no_secret_leaked(logs)
    await client.aclose()


@pytest.mark.asyncio
async def test_network_failure_does_not_log_credentials():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated DNS failure")

    provider, client = _provider_with(handler)
    with structlog.testing.capture_logs() as logs:
        await provider.get_balance()
        await provider.get_transactions()
        await provider.get_orders()

    _assert_no_secret_leaked(logs)
    await client.aclose()


@pytest.mark.asyncio
async def test_exception_message_from_missing_credentials_does_not_echo_the_secret():
    """Even the "not configured" path must never accidentally echo back
    whatever partial value was passed in."""
    provider = CoinDCXReadOnlyAccountProvider(api_key="", api_secret=FAKE_SECRET)
    from services.exchange.coindcx import CoinDCXAuthError

    with pytest.raises(CoinDCXAuthError) as exc_info:
        await provider._post("/exchange/v1/derivatives/futures/positions", {})
    assert FAKE_SECRET not in str(exc_info.value)
