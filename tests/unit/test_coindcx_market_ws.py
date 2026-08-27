"""Mocked tests for services/market_data/coindcx_ws.py -- the live public
BTC/USDT market-data WebSocket. None of these need (or ever open) a real
CoinDCX WebSocket connection; every test drives the pure message-handling
methods and the socket.io event adapters directly against a client whose
underlying socketio.AsyncClient is never actually connected."""
import asyncio
from datetime import datetime, timedelta

import pytest

from database.schema.models import ConnectionState
from services.market_data.coindcx_ws import CoinDCXMarketDataWebSocket, _extract_payload, _ms_to_dt


def _client():
    return CoinDCXMarketDataWebSocket(symbol="BTC/USDT")


async def _cancel(task):
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---- 6. Normalize symbol ----

def test_instrument_is_usdt_margined_not_the_account_default_inr():
    client = _client()
    assert client._instrument == "B-BTC_USDT"


# ---- 3/4/7. Receive valid ticker / parse price / update live state ----

def test_handle_price_change_updates_state():
    client = _client()
    now = datetime(2026, 1, 1, 12, 0, 0)
    client.handle_price_change({"T": 1735732800000, "p": "78000.5", "pr": "f"}, now=now)
    assert client.state.last_price_usdt == 78000.5
    assert client.state.received_at == now
    assert client.state.raw == {"T": 1735732800000, "p": "78000.5", "pr": "f"}


# ---- 5. Parse timestamp ----

def test_ms_to_dt_parses_epoch_millis():
    dt = _ms_to_dt(1735732800000)
    assert dt == datetime(2025, 1, 1, 12, 0, 0)


def test_ms_to_dt_returns_none_for_garbage():
    assert _ms_to_dt("not-a-number") is None
    assert _ms_to_dt(None) is None


def test_handle_price_change_sets_event_timestamp_from_T_field():
    client = _client()
    client.handle_price_change({"T": 1735732800000, "p": "1.0"})
    assert client.state.event_timestamp == _ms_to_dt(1735732800000)


# ---- Mark price (currentPrices@futures#update) ----

def test_handle_current_prices_updates_mark_price_for_our_instrument():
    client = _client()
    now = datetime(2026, 1, 1, 12, 0, 0)
    client.handle_current_prices(
        {"ts": 1735732800000, "prices": {"B-BTC_USDT": {"mp": 78050.25}, "B-ETH_USDT": {"mp": 3000.0}}},
        now=now,
    )
    assert client.state.mark_price_usdt == 78050.25
    assert client.state.received_at == now


def test_handle_current_prices_never_fabricates_index_price():
    client = _client()
    client.handle_current_prices({"ts": 1, "prices": {"B-BTC_USDT": {"mp": 100.0}}})
    assert client.state.index_price_usdt is None


# ---- 16. Unknown symbol ----

def test_handle_current_prices_ignores_message_without_our_instrument():
    client = _client()
    client.handle_current_prices({"ts": 1, "prices": {"B-ETH_USDT": {"mp": 3000.0}}})
    assert client.state.mark_price_usdt is None


def test_handle_current_prices_ignores_entry_missing_mp():
    client = _client()
    client.handle_current_prices({"ts": 1, "prices": {"B-BTC_USDT": {"bmST": 123}}})
    assert client.state.mark_price_usdt is None


# ---- 17. Missing price ----

def test_handle_price_change_ignores_message_without_price_field():
    client = _client()
    client.handle_price_change({"T": 1})
    assert client.state.last_price_usdt is None
    assert client.state.received_at is None


def test_handle_current_prices_ignores_non_dict_prices():
    client = _client()
    client.handle_current_prices({"ts": 1, "prices": None})
    assert client.state.mark_price_usdt is None


# ---- 14. Malformed message ----

def test_handle_price_change_ignores_non_numeric_price():
    client = _client()
    client.handle_price_change({"p": "not-a-price"})
    assert client.state.last_price_usdt is None


def test_handle_current_prices_ignores_non_numeric_mark_price():
    client = _client()
    client.handle_current_prices({"prices": {"B-BTC_USDT": {"mp": "garbage"}}})
    assert client.state.mark_price_usdt is None


# ---- 15. Duplicate message ----

def test_duplicate_identical_price_change_is_idempotent():
    client = _client()
    msg = {"T": 1735732800000, "p": "78000.5"}
    client.handle_price_change(msg)
    first = (client.state.last_price_usdt, client.state.event_timestamp)
    client.handle_price_change(msg)
    second = (client.state.last_price_usdt, client.state.event_timestamp)
    assert first == second


# ---- Real wire-format regression: CoinDCX wraps every event's payload as
# {"event": <name>, "data": <JSON-encoded STRING>} -- this crashed the
# first real connectivity test attempt with the docs' own inner-shape-only
# samples (AttributeError: 'str' object has no attribute 'get') before
# _extract_payload() was added to handle it. ----

def test_extract_payload_parses_the_real_json_string_wrapper():
    response = {"event": "price-change", "data": '{"T":1787763803239,"p":"77979","pr":"f"}'}
    assert _extract_payload(response) == {"T": 1787763803239, "p": "77979", "pr": "f"}


def test_extract_payload_still_accepts_an_already_parsed_dict():
    response = {"event": "price-change", "data": {"T": 1, "p": "1.0"}}
    assert _extract_payload(response) == {"T": 1, "p": "1.0"}


def test_extract_payload_accepts_a_bare_payload_with_no_wrapper():
    assert _extract_payload({"T": 1, "p": "1.0"}) == {"T": 1, "p": "1.0"}


def test_extract_payload_never_raises_on_garbage_json_string():
    assert _extract_payload({"data": "{not valid json"}) == {}


def test_extract_payload_never_raises_on_non_dict_response():
    assert _extract_payload("just a string") == {}
    assert _extract_payload(None) == {}


@pytest.mark.asyncio
async def test_on_price_change_event_adapter_handles_the_real_string_wrapper():
    client = _client()
    response = {"event": "price-change", "data": '{"T":1787763803239,"p":"77979","pr":"f"}'}
    await client._on_price_change_event(response)
    assert client.state.last_price_usdt == 77979.0


@pytest.mark.asyncio
async def test_on_current_prices_event_adapter_handles_the_real_string_wrapper():
    client = _client()
    response = {
        "event": "currentPrices@futures#update",
        "data": '{"ts":1787763829625,"prices":{"B-BTC_USDT":{"mp":77980.5}}}',
    }
    await client._on_current_prices_event(response)
    assert client.state.mark_price_usdt == 77980.5


# ---- 1/2. Connect / subscribe ----

@pytest.mark.asyncio
async def test_on_connect_subscribes_to_both_public_channels(monkeypatch):
    client = _client()
    emitted = []

    async def fake_emit(event, data=None):
        emitted.append((event, data))

    monkeypatch.setattr(client._sio, "emit", fake_emit)
    await client._on_connect()

    assert client._connected is True
    assert client._ever_connected is True
    assert ("join", {"channelName": "B-BTC_USDT@prices-futures"}) in emitted
    assert ("join", {"channelName": "currentPrices@futures@rt"}) in emitted
    await _cancel(client._ping_task)


# ---- 9. Disconnect ----

@pytest.mark.asyncio
async def test_on_disconnect_sets_disconnected_and_cancels_ping(monkeypatch):
    client = _client()
    monkeypatch.setattr(client._sio, "emit", _noop_emit)
    await client._on_connect()
    ping_task = client._ping_task
    await client._on_disconnect()
    assert client._connected is False
    assert client._ping_task is None
    with pytest.raises(asyncio.CancelledError):
        await ping_task
    assert ping_task.cancelled()


async def _noop_emit(event, data=None):
    return None


# ---- 10/11. Reconnect + resubscribe ----

@pytest.mark.asyncio
async def test_reconnect_transitions_disconnected_then_connected_and_resubscribes(monkeypatch):
    client = _client()
    emitted = []

    async def fake_emit(event, data=None):
        emitted.append((event, data))

    monkeypatch.setattr(client._sio, "emit", fake_emit)

    await client._on_connect()
    assert client._connected is True
    await client._on_disconnect()
    assert client.connection_status() == ConnectionState.DISCONNECTED

    emitted.clear()
    await client._on_connect()
    assert client._connected is True
    # Resubscribed after reconnect -- not skipped just because it joined before.
    assert ("join", {"channelName": "B-BTC_USDT@prices-futures"}) in emitted
    assert ("join", {"channelName": "currentPrices@futures@rt"}) in emitted
    await _cancel(client._ping_task)


# ---- 12/13. Stale detection + recovery ----

def test_connection_status_stale_after_threshold():
    client = _client()
    client._connected = True
    client._ever_connected = True
    now = datetime(2026, 1, 1, 12, 0, 30)
    client.state.received_at = datetime(2026, 1, 1, 12, 0, 0)
    assert client.connection_status(now=now, stale_after=timedelta(seconds=20)) == ConnectionState.STALE


def test_connection_status_live_within_threshold():
    client = _client()
    client._connected = True
    client._ever_connected = True
    now = datetime(2026, 1, 1, 12, 0, 10)
    client.state.received_at = datetime(2026, 1, 1, 12, 0, 0)
    assert client.connection_status(now=now, stale_after=timedelta(seconds=20)) == ConnectionState.LIVE


def test_connection_status_recovers_to_live_after_a_fresh_tick():
    client = _client()
    client._connected = True
    client._ever_connected = True
    client.state.received_at = datetime(2026, 1, 1, 12, 0, 0)
    stale_now = datetime(2026, 1, 1, 12, 5, 0)
    assert client.connection_status(now=stale_now) == ConnectionState.STALE
    client.handle_price_change({"p": "100"}, now=datetime(2026, 1, 1, 12, 5, 1))
    assert client.connection_status(now=datetime(2026, 1, 1, 12, 5, 2)) == ConnectionState.LIVE


def test_connection_status_unavailable_before_first_connect():
    client = _client()
    assert client.connection_status() == ConnectionState.UNAVAILABLE


def test_connection_status_disconnected_after_having_connected_once():
    client = _client()
    client._ever_connected = True
    client._connected = False
    assert client.connection_status() == ConnectionState.DISCONNECTED


# ---- 8. Heartbeat ----

@pytest.mark.asyncio
async def test_ping_loop_emits_documented_ping_payload(monkeypatch):
    client = _client()
    emitted = []

    async def fake_emit(event, data=None):
        emitted.append((event, data))
        client._connected = False  # stop the loop right after this ping

    async def instant_sleep(_seconds):
        return None

    monkeypatch.setattr(client._sio, "emit", fake_emit)
    monkeypatch.setattr("services.market_data.coindcx_ws.asyncio.sleep", instant_sleep)
    client._connected = True
    await client._ping_loop()
    assert ("ping", {"data": "Ping message"}) in emitted


# ---- 22. Telegram state-transition alert (dedup) ----

@pytest.mark.asyncio
async def test_state_transition_callback_not_fired_on_first_connect():
    calls = []

    async def on_change(old, new):
        calls.append((old, new))

    client = CoinDCXMarketDataWebSocket(on_state_change=on_change)
    client._sio.emit = _noop_emit
    await client._on_connect()
    assert calls == []  # baseline connect is not a "recovery" from anything
    await _cancel(client._ping_task)


@pytest.mark.asyncio
async def test_state_transition_callback_fires_once_on_real_disconnect_and_recovery():
    calls = []

    async def on_change(old, new):
        calls.append((old, new))

    client = CoinDCXMarketDataWebSocket(on_state_change=on_change)
    client._sio.emit = _noop_emit
    await client._on_connect()
    await client._on_disconnect()
    await client._on_connect()
    await _cancel(client._ping_task)

    assert calls == [
        (ConnectionState.LIVE, ConnectionState.DISCONNECTED),
        (ConnectionState.DISCONNECTED, ConnectionState.LIVE),
    ]


@pytest.mark.asyncio
async def test_state_transition_callback_never_fires_twice_for_same_state():
    calls = []

    async def on_change(old, new):
        calls.append((old, new))

    client = CoinDCXMarketDataWebSocket(on_state_change=on_change)
    # Manually invoke the transition-check twice while "connected" stays
    # True both times -- must not double-fire.
    client._connected = True
    await client._maybe_notify_transition()
    await client._maybe_notify_transition()
    assert calls == []
