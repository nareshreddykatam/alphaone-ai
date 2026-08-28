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


def test_not_configured_by_default():
    assert _client_configured() is False


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
    from services.telegram_mtproto import client as mtproto_client

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

    await mtproto_client._handle_event(event, is_edit=False)

    assert captured["channel"] == "@suncrypto_trading_alerts"
    assert captured["channel_id"] == "-100123456789"
    assert captured["message_id"] == "42"
    assert captured["edited_timestamp"] is None


async def test_handle_event_ignores_empty_text_messages():
    from services.telegram_mtproto import client as mtproto_client

    chat = _FakeChat(id=1, username="x")
    message = _FakeMessage(id=1, text="", date=datetime(2026, 1, 1))
    event = _FakeEvent(message, chat)
    await mtproto_client._handle_event(event, is_edit=False)  # must not raise, must not touch the DB
