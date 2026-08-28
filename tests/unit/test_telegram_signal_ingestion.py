"""Multi-Coin AI Futures System, Phases 21-27: message ingestion must
never process an unauthorized channel, never double-process a duplicate
delivery, correctly handle edits, and never chase a stale entry."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import ExternalTelegramMessage, ExternalSignal
from services.telegram_signals.ingestion import (
    ingest_message, process_message, is_authorized_channel, classify_entry_deviation,
    MAX_ENTRY_DEVIATION_PCT,
)

SUPPORTED = {"BTC/USDT", "ETH/USDT"}
CHANNEL = "@suncrypto_trading_alerts"


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def test_only_the_allowlisted_channel_is_authorized():
    assert is_authorized_channel("@suncrypto_trading_alerts") is True
    assert is_authorized_channel("suncrypto_trading_alerts") is True  # with/without leading @
    assert is_authorized_channel("@some_random_pump_channel") is False
    assert is_authorized_channel("@SUNCRYPTO_TRADING_ALERTS") is True  # case-insensitive


def test_channel_id_is_preferred_over_username_when_both_configured(monkeypatch):
    """Phase 4: a username can be reassigned to a different channel later;
    the numeric ID cannot -- when a channel ID is configured, it must be
    the deciding factor, not a matching (or even mismatched) username."""
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_external_signal_channel_id", "-100123456789")

    assert is_authorized_channel("@suncrypto_trading_alerts", "-100123456789") is True
    # Same real channel ID but a DIFFERENT (e.g. renamed) username -- still authorized by ID.
    assert is_authorized_channel("@renamed_channel", "-100123456789") is True
    # Matching username but a DIFFERENT numeric ID (e.g. the username was
    # reassigned to an impostor channel) -- must be REJECTED by ID, not
    # waved through because the username still matches.
    assert is_authorized_channel("@suncrypto_trading_alerts", "-100999999999") is False


def test_falls_back_to_username_when_no_channel_id_observed(monkeypatch):
    """The Bot API path can supply an ID; some callers may not -- when no
    source_channel_id is observed at all, username matching still applies
    even if a channel ID IS configured (nothing to compare against)."""
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_external_signal_channel_id", "-100123456789")

    assert is_authorized_channel("@suncrypto_trading_alerts", None) is True
    assert is_authorized_channel("@some_other_channel", None) is False


async def test_unauthorized_channel_is_rejected_before_parsing(session_maker):
    async with session_maker() as session:
        message = await ingest_message(session, "@random_channel", "1", datetime(2026, 1, 1), "BTC/USDT LONG\nEntry: 80000\nSL: 79000\nTP1: 83000")
        signal = await process_message(session, message, SUPPORTED)
        assert signal.status == "REJECTED_UNAUTHORIZED_SOURCE"
        assert signal.symbol is None  # never even parsed


async def test_duplicate_message_id_is_not_reprocessed(session_maker):
    async with session_maker() as session:
        m1 = await ingest_message(session, CHANNEL, "MSG-1", datetime(2026, 1, 1), "text one")
        m2 = await ingest_message(session, CHANNEL, "MSG-1", datetime(2026, 1, 1), "text one (retry delivery)")
        assert m1.id == m2.id
        assert m2.text == "text one"  # original text preserved, not overwritten by the "duplicate delivery"

        count = (await session.execute(select(ExternalTelegramMessage))).scalars().all()
        assert len(count) == 1


async def test_edited_message_updates_edited_text_not_original(session_maker):
    async with session_maker() as session:
        original_time = datetime(2026, 1, 1, 10, 0)
        edit_time = datetime(2026, 1, 1, 10, 5)
        m1 = await ingest_message(session, CHANNEL, "MSG-2", original_time, "BTC LONG entry 80000")
        m2 = await ingest_message(session, CHANNEL, "MSG-2", original_time, "BTC LONG entry 81000", edited_timestamp=edit_time)

        assert m1.id == m2.id
        assert m2.text == "BTC LONG entry 80000"  # original preserved
        assert m2.edited_text == "BTC LONG entry 81000"
        assert m2.edited_timestamp == edit_time


async def test_older_edit_timestamp_does_not_overwrite_a_newer_edit(session_maker):
    async with session_maker() as session:
        t0 = datetime(2026, 1, 1, 10, 0)
        await ingest_message(session, CHANNEL, "MSG-3", t0, "v0")
        await ingest_message(session, CHANNEL, "MSG-3", t0, "v2", edited_timestamp=t0 + timedelta(minutes=10))
        stale_replay = await ingest_message(session, CHANNEL, "MSG-3", t0, "v1-replayed-late", edited_timestamp=t0 + timedelta(minutes=5))
        assert stale_replay.edited_text == "v2"  # the later edit wins, not the out-of-order replay


def test_entry_within_tolerance_is_valid():
    status, _ = classify_entry_deviation(entry_price=80000.0, current_price=80100.0)
    assert status == "ENTRY_VALID"


def test_entry_far_from_market_is_too_far():
    status, reason = classify_entry_deviation(entry_price=80000.0, current_price=85000.0)
    assert status == "ENTRY_TOO_FAR"
    assert "%" in reason


def test_entry_deviation_boundary_exactly_at_threshold():
    current = 80000.0
    entry_at_exactly_max = current * (1 + MAX_ENTRY_DEVIATION_PCT / 100)
    status, _ = classify_entry_deviation(entry_at_exactly_max, current)
    assert status == "ENTRY_VALID"  # exactly at the boundary is still valid (not over it)


def test_no_live_price_is_entry_stale_never_assumed_valid():
    status, reason = classify_entry_deviation(entry_price=80000.0, current_price=None)
    assert status == "ENTRY_STALE"


async def test_valid_signal_processes_to_valid_status_with_real_market_price(session_maker):
    async with session_maker() as session:
        message = await ingest_message(session, CHANNEL, "MSG-4", datetime(2026, 1, 1), "BTC/USDT LONG\nEntry: 80000\nSL: 79000\nTP1: 83000")

        async def price_lookup(symbol):
            return 80050.0

        signal = await process_message(session, message, SUPPORTED, current_price_lookup=price_lookup)
        assert signal.status == "VALID"
        assert signal.market_price_at_validation == pytest.approx(80050.0)


async def test_signal_with_entry_too_far_from_market_is_rejected_not_chased(session_maker):
    async with session_maker() as session:
        message = await ingest_message(session, CHANNEL, "MSG-5", datetime(2026, 1, 1), "BTC/USDT LONG\nEntry: 80000\nSL: 79000\nTP1: 83000")

        async def price_lookup(symbol):
            return 90000.0  # way off from the stated 80000 entry

        signal = await process_message(session, message, SUPPORTED, current_price_lookup=price_lookup)
        assert signal.status == "ENTRY_TOO_FAR"


async def test_rejected_signals_are_persisted_not_silently_dropped(session_maker):
    """Every rejection reason must be recorded (Phase 33's Telegram
    report needs real rejected-signal counts, not an assumption)."""
    async with session_maker() as session:
        message = await ingest_message(session, CHANNEL, "MSG-6", datetime(2026, 1, 1), "BTC long soon, feeling bullish")
        signal = await process_message(session, message, SUPPORTED)
        assert signal.status == "INVALID"
        assert signal.rejection_reason is not None

        rows = (await session.execute(select(ExternalSignal))).scalars().all()
        assert len(rows) == 1
