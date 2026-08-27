"""Phase 5: CoinDCX WebSocket client message-handling and freshness logic,
tested by calling the parsing methods directly with synthetic payloads --
no real socket connection, per the mocks-first mandate. Payload shapes
match docs/coindcx_api_findings.md exactly.
"""
from datetime import datetime, timedelta

import pytest

from database.schema.models import ConnectionState
from services.exchange.coindcx_ws import CoinDCXWebSocketClient, _extract_payload


def test_handle_price_change_updates_market_state():
    client = CoinDCXWebSocketClient()
    client.handle_price_change({"T": 1705516361108, "p": "65000.5", "pr": "f"})
    assert client.market_state.price == 65000.5
    assert client.market_state.last_updated is not None


def test_handle_price_change_ignores_malformed_payload_without_price():
    client = CoinDCXWebSocketClient()
    client.handle_price_change({"T": 123})
    assert client.market_state.price is None


def test_handle_position_update_indexes_by_position_id():
    client = CoinDCXWebSocketClient()
    client.handle_position_update([
        {"id": "p1", "pair": "B-BTC_USDT", "mark_price": 65000, "active_pos": 1.0},
        {"id": "p2", "pair": "B-ETH_USDT", "mark_price": 3000, "active_pos": -2.0},
    ])
    assert client.account_state.positions["p1"]["mark_price"] == 65000
    assert client.account_state.positions["p2"]["active_pos"] == -2.0
    assert client.account_state.positions_updated_at is not None


def test_handle_position_update_overwrites_existing_entry():
    client = CoinDCXWebSocketClient()
    client.handle_position_update([{"id": "p1", "mark_price": 100}])
    client.handle_position_update([{"id": "p1", "mark_price": 105}])
    assert client.account_state.positions["p1"]["mark_price"] == 105
    assert len(client.account_state.positions) == 1


def test_handle_balance_update():
    client = CoinDCXWebSocketClient()
    client.handle_balance_update([{"id": "1", "balance": "1.02", "locked_balance": "0.5", "currency_short_name": "USDT"}])
    assert client.account_state.balance[0]["currency_short_name"] == "USDT"
    assert client.account_state.balance_updated_at is not None


def test_market_data_state_disconnected_when_never_connected():
    client = CoinDCXWebSocketClient()
    assert client.market_data_state() == ConnectionState.DISCONNECTED


def test_market_data_state_unavailable_when_connected_but_no_data_yet():
    client = CoinDCXWebSocketClient()
    client._connected = True
    assert client.market_data_state() == ConnectionState.UNAVAILABLE


def test_market_data_state_live_when_recently_updated():
    client = CoinDCXWebSocketClient()
    client._connected = True
    client.handle_price_change({"p": "100"})
    assert client.market_data_state() == ConnectionState.LIVE


def test_market_data_state_stale_when_update_is_old():
    client = CoinDCXWebSocketClient()
    client._connected = True
    client.handle_price_change({"p": "100"})
    later = datetime.utcnow() + timedelta(seconds=60)
    assert client.market_data_state(now=later, stale_after=timedelta(seconds=30)) == ConnectionState.STALE


def test_account_data_state_not_configured_without_credentials():
    client = CoinDCXWebSocketClient()  # no api_key/secret
    client._connected = True
    client.handle_position_update([{"id": "p1"}])
    assert client.account_data_state() == ConnectionState.NOT_CONFIGURED


def test_account_data_state_live_when_configured_connected_and_fresh():
    client = CoinDCXWebSocketClient(api_key="k", api_secret="s")
    client._connected = True
    client.handle_position_update([{"id": "p1"}])
    assert client.account_data_state() == ConnectionState.LIVE


# ---- Real wire-format regression (2026-08-26): CoinDCX wraps every event's
# payload as {"event": <name>, "data": <JSON-encoded STRING>}. This crashed
# 142/142 real price-change events against a real authenticated connection
# (scripts/coindcx_account_ws_verification_test.py) with
# AttributeError: 'str' object has no attribute 'get' before
# _extract_payload() was added -- the identical bug/fix pattern already
# found and fixed in services/market_data/coindcx_ws.py. ----

def test_extract_payload_parses_the_real_json_string_wrapper_dict_shape():
    response = {"event": "price-change", "data": '{"T":1787763803239,"p":"77979","pr":"f"}'}
    assert _extract_payload(response) == {"T": 1787763803239, "p": "77979", "pr": "f"}


def test_extract_payload_parses_the_real_json_string_wrapper_list_shape():
    """df-position-update/balance-update's documented inner shape is a
    JSON ARRAY, not a dict -- confirm the array-shaped case parses too."""
    response = {"event": "df-position-update", "data": '[{"id":"p1","pair":"B-BNB_USDT","active_pos":0}]'}
    assert _extract_payload(response) == [{"id": "p1", "pair": "B-BNB_USDT", "active_pos": 0}]


def test_extract_payload_still_accepts_an_already_parsed_value():
    assert _extract_payload({"event": "x", "data": {"a": 1}}) == {"a": 1}
    assert _extract_payload({"event": "x", "data": [{"a": 1}]}) == [{"a": 1}]


def test_extract_payload_accepts_a_bare_payload_with_no_wrapper():
    assert _extract_payload({"p": "1.0"}) == {"p": "1.0"}


def test_extract_payload_returns_none_on_garbage_json_string():
    assert _extract_payload({"data": "{not valid json"}) is None


def test_extract_payload_passes_through_non_dict_response_unchanged():
    assert _extract_payload("just a string") == "just a string"
    assert _extract_payload(None) is None


@pytest.mark.asyncio
async def test_on_price_change_event_adapter_handles_the_real_string_wrapper():
    client = CoinDCXWebSocketClient()
    response = {"event": "price-change", "data": '{"T":1787763803239,"p":"77979","pr":"f"}'}
    await client._on_price_change_event(response)
    assert client.market_state.price == 77979.0


@pytest.mark.asyncio
async def test_on_price_change_event_adapter_never_crashes_on_malformed_data():
    client = CoinDCXWebSocketClient()
    await client._on_price_change_event({"event": "price-change", "data": "{not valid json"})
    assert client.market_state.price is None


@pytest.mark.asyncio
async def test_on_position_update_event_adapter_handles_the_real_string_wrapper():
    client = CoinDCXWebSocketClient(api_key="k", api_secret="s")
    response = {"event": "df-position-update", "data": '[{"id":"p1","pair":"B-BTC_USDT","active_pos":1.0}]'}
    await client._on_position_update_event(response)
    assert client.account_state.positions["p1"]["pair"] == "B-BTC_USDT"


@pytest.mark.asyncio
async def test_on_balance_update_event_adapter_handles_the_real_string_wrapper():
    client = CoinDCXWebSocketClient(api_key="k", api_secret="s")
    response = {
        "event": "balance-update",
        "data": '[{"id":"12345","balance":"1.02","locked_balance":"0.5","currency_short_name":"USDT"}]',
    }
    await client._on_balance_update_event(response)
    assert client.account_state.balance[0]["currency_short_name"] == "USDT"


@pytest.mark.asyncio
async def test_on_position_update_event_adapter_never_crashes_on_malformed_data():
    client = CoinDCXWebSocketClient(api_key="k", api_secret="s")
    await client._on_position_update_event({"event": "df-position-update", "data": "{not valid json"})
    assert client.account_state.positions == {}
