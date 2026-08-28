"""Multi-Coin AI Futures System: MTProto read-only listener lifecycle and
event handling. No real Telegram user-account connection is made in any
test here -- Telethon's client/events are mocked; the point is to prove
the LISTENER's own safety properties (never starts without full config,
never double-starts, routes every event through the exact same shared
pipeline the Bot API path uses, never calls a mutating Telethon method).
"""
from dataclasses import dataclass
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.telegram_mtproto.client import MTProtoListener, _client_configured, build_client


def test_not_configured_without_all_four_settings(monkeypatch):
    """Exercises _client_configured()'s own logic explicitly, rather than
    relying on this machine's real .env happening to be empty -- a real
    local dev environment may legitimately have real MTProto credentials
    configured (e.g. for manual verification), which must never make this
    test flaky or falsely fail."""
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_mtproto_enabled", False)
    monkeypatch.setattr(settings, "telegram_api_id", "")
    monkeypatch.setattr(settings, "telegram_api_hash", "")
    monkeypatch.setattr(settings, "telegram_session", "")
    assert _client_configured() is False


def test_configured_when_all_four_settings_are_present(monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_mtproto_enabled", True)
    monkeypatch.setattr(settings, "telegram_api_id", "12345")
    monkeypatch.setattr(settings, "telegram_api_hash", "fakehash")
    monkeypatch.setattr(settings, "telegram_session", "fakesession")
    assert _client_configured() is True


def test_build_client_raises_clear_error_without_full_config(monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_api_id", "")
    monkeypatch.setattr(settings, "telegram_api_hash", "")
    monkeypatch.setattr(settings, "telegram_session", "")
    with pytest.raises(ValueError, match="TELEGRAM_API_ID"):
        build_client()


async def test_start_is_a_safe_noop_when_not_configured(monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_mtproto_enabled", False)

    listener = MTProtoListener()
    listener.start()
    assert listener.is_running() is False  # no task created -- nothing to await/cancel


async def test_double_start_does_not_create_a_second_listener(monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_mtproto_enabled", True)
    monkeypatch.setattr(settings, "telegram_api_id", "123")
    monkeypatch.setattr(settings, "telegram_api_hash", "abc")
    monkeypatch.setattr(settings, "telegram_session", "fake-session-string")

    listener = MTProtoListener()

    async def _hang_forever():
        import asyncio
        await asyncio.Event().wait()

    monkeypatch.setattr(listener, "_run", _hang_forever)
    listener.start()
    first_task = listener._task
    listener.start()  # second call must be a no-op, not a second task
    assert listener._task is first_task
    await listener.stop()


async def test_stop_is_safe_when_never_started():
    listener = MTProtoListener()
    await listener.stop()  # must not raise


@dataclass
class _FakeChat:
    id: int
    username: str = None


class _FakeMessage:
    def __init__(self, id, text, date, edit_date=None):
        self.id = id
        self.text = text
        self.date = date
        self.edit_date = edit_date


class _FakeEvent:
    def __init__(self, message, chat):
        self.message = message
        self._chat = chat

    async def get_chat(self):
        return self._chat


async def test_handle_event_routes_through_the_shared_pipeline_with_channel_id(monkeypatch):
    captured = {}

    async def fake_process(session, channel, message_id, timestamp, text, edited_timestamp=None, channel_id=None, notify=True):
        captured.update(channel=channel, message_id=message_id, text=text, channel_id=channel_id, edited_timestamp=edited_timestamp)
        return None

    monkeypatch.setattr("services.telegram_signals.pipeline.process_incoming_channel_message", fake_process)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("database.schema.async_session", lambda: _FakeSession())

    chat = _FakeChat(id=-100123456789, username="suncrypto_trading_alerts")
    message = _FakeMessage(id=42, text="BTC/USDT LONG\nEntry: 80000\nSL: 79000\nTP1: 83000", date=datetime(2026, 1, 1))
    event = _FakeEvent(message, chat)

    listener = MTProtoListener()
    await listener._handle_event(event, is_edit=False)

    assert captured["channel"] == "@suncrypto_trading_alerts"
    assert captured["channel_id"] == "-100123456789"
    assert captured["message_id"] == "42"
    assert captured["edited_timestamp"] is None
    # Last-event state must be recorded for the status endpoint.
    assert listener._last_event_at is not None
    assert listener._last_event_type == "channel_post"


async def test_handle_event_records_edited_channel_post_type(monkeypatch):
    async def fake_process(session, channel, message_id, timestamp, text, edited_timestamp=None, channel_id=None, notify=True):
        return None

    monkeypatch.setattr("services.telegram_signals.pipeline.process_incoming_channel_message", fake_process)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("database.schema.async_session", lambda: _FakeSession())

    chat = _FakeChat(id=-100123456789, username="suncrypto_trading_alerts")
    message = _FakeMessage(id=42, text="edited text", date=datetime(2026, 1, 1), edit_date=datetime(2026, 1, 1, 0, 5))
    event = _FakeEvent(message, chat)

    listener = MTProtoListener()
    await listener._handle_event(event, is_edit=True)
    assert listener._last_event_type == "edited_channel_post"


async def test_handle_event_ignores_empty_text_messages():
    chat = _FakeChat(id=1, username="x")
    message = _FakeMessage(id=1, text="", date=datetime(2026, 1, 1))
    event = _FakeEvent(message, chat)
    listener = MTProtoListener()
    await listener._handle_event(event, is_edit=False)  # must not raise, must not touch the DB
    assert listener._last_event_at is None  # an ignored (empty-text) event is not a real event


def test_status_before_any_connection_reports_disconnected_and_unauthorized():
    listener = MTProtoListener()
    status = listener.get_status()
    assert status.connected is False
    assert status.authorized is False
    assert status.listener_running is False
    assert status.resolved_channel_id is None
    assert status.last_event_at is None
    assert status.seconds_since_last_event is None


def test_is_connected_reflects_live_telethon_state_not_a_cached_flag():
    listener = MTProtoListener()

    class _FakeClient:
        def is_connected(self):
            return True

    listener._client = _FakeClient()
    assert listener.is_connected() is True

    class _DisconnectedClient:
        def is_connected(self):
            return False

    listener._client = _DisconnectedClient()
    assert listener.is_connected() is False  # must not claim connected after the client disconnects


def test_authorized_requires_both_the_cached_flag_and_a_live_connection():
    listener = MTProtoListener()
    listener._authorized = True

    class _ConnectedClient:
        def is_connected(self):
            return True

    listener._client = _ConnectedClient()
    assert listener.is_authorized() is True

    class _DisconnectedClient:
        def is_connected(self):
            return False

    listener._client = _DisconnectedClient()
    # Session key may still be valid, but with no live connection this must
    # NOT be reported as "authorized right now".
    assert listener.is_authorized() is False


async def test_stop_clears_authorized_and_connected_state():
    listener = MTProtoListener()
    listener._authorized = True

    class _FakeClient:
        def is_connected(self):
            return False  # simulates the state right after disconnect() resolves

        async def disconnect(self):
            pass

    listener._client = _FakeClient()
    await listener.stop()
    assert listener.is_authorized() is False
    assert listener._client is None


def test_get_status_computes_seconds_since_last_event():
    from datetime import timedelta

    listener = MTProtoListener()
    listener._last_event_at = datetime.utcnow() - timedelta(seconds=30)
    listener._last_event_type = "channel_post"
    status = listener.get_status()
    assert status.last_event_type == "channel_post"
    assert status.seconds_since_last_event >= 29  # allow small test-runtime slack


def test_authorized_channel_always_reflects_configured_settings_not_event_data(monkeypatch):
    """An impostor/unauthorized channel's data can never leak into the
    reported authorized_channel field -- it is sourced exclusively from
    settings, never from anything an incoming event claims."""
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_external_signal_channel", "@suncrypto_trading_alerts")

    listener = MTProtoListener()
    # Simulate a resolved channel id/username from a hypothetical prior
    # connection -- still must not override the configured authorized_channel.
    listener._resolved_channel_id = "-100999999999"
    listener._resolved_channel_username = "@some_other_channel"

    status = listener.get_status()
    assert status.authorized_channel == "@suncrypto_trading_alerts"
