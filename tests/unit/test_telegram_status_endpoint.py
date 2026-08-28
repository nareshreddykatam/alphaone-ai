"""Multi-Coin AI Futures System: GET /api/v1/telegram/mtproto-status --
read-only observability for the MTProto listener. Every scenario here
drives the REAL listener singleton's state directly (no live Telegram
connection) and asserts on the REAL HTTP response, so this proves both
the status-computation logic and the endpoint wiring together.
"""
from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app
from services.telegram_mtproto.client import mtproto_listener


@pytest.fixture(autouse=True)
def _reset_listener_state():
    """The listener is a process-wide singleton (matches market_ws's own
    pattern) -- reset its state before and after every test so one test's
    simulated state can never leak into another's."""
    def _reset():
        mtproto_listener._client = None
        mtproto_listener._task = None
        mtproto_listener._stop_event = None
        mtproto_listener._authorized = False
        mtproto_listener._resolved_channel_id = None
        mtproto_listener._resolved_channel_username = None
        mtproto_listener._last_event_at = None
        mtproto_listener._last_event_type = None
    _reset()
    yield
    _reset()


async def _get(path="/api/v1/telegram/mtproto-status"):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.get(path)


# 1. disabled state
async def test_disabled_state(monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_mtproto_enabled", False)

    resp = await _get()
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["connected"] is False
    assert body["authorized"] is False
    assert body["listener_running"] is False


# 2. enabled but disconnected
async def test_enabled_but_disconnected(monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_mtproto_enabled", True)

    resp = await _get()
    body = resp.json()
    assert body["enabled"] is True
    assert body["connected"] is False
    assert body["authorized"] is False


# 3. connected and authorized
async def test_connected_and_authorized(monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_mtproto_enabled", True)

    class _FakeClient:
        def is_connected(self):
            return True

    mtproto_listener._client = _FakeClient()
    mtproto_listener._authorized = True
    mtproto_listener._resolved_channel_id = "-100123456789"

    resp = await _get()
    body = resp.json()
    assert body["connected"] is True
    assert body["authorized"] is True
    assert body["resolved_channel_id"] == "-100123456789"


# 4. channel configured
async def test_channel_configured_reflects_the_real_authorized_source(monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_external_signal_channel", "@suncrypto_trading_alerts")

    resp = await _get()
    body = resp.json()
    assert body["channel_configured"] is True
    assert body["authorized_channel"] == "@suncrypto_trading_alerts"


async def test_channel_not_configured_when_setting_is_empty(monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_external_signal_channel", "")

    resp = await _get()
    body = resp.json()
    assert body["channel_configured"] is False
    assert body["authorized_channel"] is None


# 5. last event timestamp
async def test_last_event_timestamp_reported_after_a_real_event():
    now = datetime.utcnow()
    mtproto_listener._last_event_at = now
    mtproto_listener._last_event_type = "channel_post"

    resp = await _get()
    body = resp.json()
    assert body["last_event_at"] is not None
    assert body["last_event_type"] == "channel_post"
    assert body["seconds_since_last_event"] is not None
    assert body["seconds_since_last_event"] >= 0


# 6. stale/no-event state
async def test_no_event_state_reports_null_never_fabricated():
    resp = await _get()
    body = resp.json()
    assert body["last_event_at"] is None
    assert body["last_event_type"] is None
    assert body["seconds_since_last_event"] is None


async def test_stale_event_reports_a_large_seconds_since_last_event():
    mtproto_listener._last_event_at = datetime.utcnow() - timedelta(hours=6)
    mtproto_listener._last_event_type = "channel_post"

    resp = await _get()
    body = resp.json()
    assert body["seconds_since_last_event"] > 3600 * 5


# 7. reconnect state -- connected flips false immediately when the
# underlying Telethon sender reports disconnected, before any reconnect
# attempt has resolved.
async def test_reconnect_in_progress_reports_disconnected_not_stale_connected():
    class _ReconnectingClient:
        def is_connected(self):
            return False

    mtproto_listener._client = _ReconnectingClient()
    mtproto_listener._authorized = True  # cached from the prior successful session

    resp = await _get()
    body = resp.json()
    assert body["connected"] is False
    assert body["authorized"] is False  # must not claim authorized without a live connection


# 8. shutdown state
async def test_shutdown_state_reports_fully_disconnected():
    await mtproto_listener.stop()  # safe even though nothing was really started
    resp = await _get()
    body = resp.json()
    assert body["connected"] is False
    assert body["authorized"] is False
    assert body["listener_running"] is False


# 9. credential non-exposure
async def test_response_never_contains_any_credential_value(monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_api_id", "12345678")
    monkeypatch.setattr(settings, "telegram_api_hash", "deadbeefcafefeed0011223344556677")
    monkeypatch.setattr(settings, "telegram_session", "1BVtsOK4Bu1secretsessionstringvaluehere==")
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:ABC-fake-bot-token")
    monkeypatch.setattr(settings, "coindcx_api_key", "fake-coindcx-key")
    monkeypatch.setattr(settings, "coindcx_api_secret", "fake-coindcx-secret")

    resp = await _get()
    raw_text = resp.text
    for forbidden in (
        "12345678", "deadbeefcafefeed0011223344556677", "1BVtsOK4Bu1secretsessionstringvaluehere==",
        "123456:ABC-fake-bot-token", "fake-coindcx-key", "fake-coindcx-secret",
    ):
        assert forbidden not in raw_text
    body = resp.json()
    for forbidden_key in (
        "api_id", "api_hash", "session", "telegram_session", "bot_token", "phone",
        "code", "password", "2fa", "coindcx_api_key", "coindcx_api_secret",
    ):
        assert forbidden_key not in body


# 10. unauthorized channel cannot be represented as the authorized source
async def test_authorized_channel_field_never_reflects_an_unauthorized_source(monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_external_signal_channel", "@suncrypto_trading_alerts")

    # Simulate state that COULD exist if a bug let a different channel's
    # data leak into the listener's resolved fields.
    mtproto_listener._resolved_channel_id = "-100999999999"
    mtproto_listener._resolved_channel_username = "@some_impostor_channel"

    resp = await _get()
    body = resp.json()
    assert body["authorized_channel"] == "@suncrypto_trading_alerts"
    assert "impostor" not in resp.text


# 11. endpoint is read-only
async def test_endpoint_only_supports_get():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        for method in ("post", "put", "patch", "delete"):
            resp = await ac.request(method, "/api/v1/telegram/mtproto-status")
            assert resp.status_code == 405, f"{method.upper()} must not be supported"


# 12. no order-placement capability (module-level, source-based -- see
# also tests/unit/test_no_order_placement_capability.py's own coverage)
def test_status_router_module_has_no_order_mutating_or_send_capability():
    import inspect
    import apps.api.routers.telegram_status as status_module
    source = inspect.getsource(status_module)
    for forbidden in ("create_order", "place_order", "submit_order", "send_message", "forward_messages"):
        assert forbidden not in source
    assert "services.exchange.coindcx" not in source
