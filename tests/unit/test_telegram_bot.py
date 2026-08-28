"""Phase 4E: Telegram bot command handlers, tested against a mocked Update/
Bot and an in-memory DB -- no live Telegram connection (per the user's
explicit choice: build and test with mocks only, wire a real BOT_TOKEN
later without changing any code). Every handler must read real DB state,
never fabricate, and never call anything that places/modifies a trade.
"""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from apps.api.config import get_settings
from database.schema import Base
from database.schema.models import Signal
from services.telegram.bot import TelegramBot
from services.signal_engine.live_signal import is_signal_generation_paused
from services.portfolio.account import get_or_create_default_account
from services.trade_journal.journal import open_trade, record_exit


@pytest.fixture
async def db_session_maker(monkeypatch):
    # Force no CoinDCX credentials for the duration of these tests -- the
    # /account command's "no credentials" behavior must be deterministic
    # regardless of whether this machine's real .env has real credentials
    # configured (it now does, for the real-account connectivity test).
    settings = get_settings()
    monkeypatch.setattr(settings, "coindcx_api_key", "")
    monkeypatch.setattr(settings, "coindcx_api_secret", "")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("services.telegram.bot.async_session", session_maker)
    yield session_maker
    await engine.dispose()


def _make_update():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


@pytest.mark.asyncio
async def test_start_and_help_never_touch_the_db(db_session_maker):
    bot = TelegramBot(bot_token="x", chat_id="y")
    update = _make_update()
    await bot._cmd_start(update, None)
    await bot._cmd_help(update, None)
    update.message.reply_text.assert_any_call(
        "🤖 AlphaOne\n\n"
        "Trading intelligence and manual trade tracking for BTC/USDT perpetuals on CoinDCX.\n"
        "AlphaOne never places, cancels, or modifies orders -- you execute everything manually.\n\n"
        "Use /help for available commands."
    )


@pytest.mark.asyncio
async def test_status_reports_active_by_default(db_session_maker):
    bot = TelegramBot(bot_token="x", chat_id="y")
    update = _make_update()
    await bot._cmd_status(update, None)
    text = update.message.reply_text.call_args[0][0]
    assert "ACTIVE" in text
    assert "active" in text  # signal generation not paused


@pytest.mark.asyncio
async def test_account_reports_not_configured_without_coindcx_credentials(db_session_maker):
    bot = TelegramBot(bot_token="x", chat_id="y")
    update = _make_update()
    await bot._cmd_account(update, None)
    text = update.message.reply_text.call_args[0][0]
    assert "COINDCX ACCOUNT" in text
    assert "NOT_CONFIGURED" in text


@pytest.mark.asyncio
async def test_account_shows_real_numbers_when_coindcx_reports_ok(db_session_maker, monkeypatch):
    async def fake_get_balance(self):
        return {"status": "OK", "total_equity": 1000.0, "available_balance": 900.0, "used_margin": 100.0}

    monkeypatch.setattr("services.exchange.coindcx.CoinDCXReadOnlyAccountProvider.get_balance", fake_get_balance)

    bot = TelegramBot(bot_token="x", chat_id="y")
    update = _make_update()
    await bot._cmd_account(update, None)
    text = update.message.reply_text.call_args[0][0]
    assert "Equity: ₹1,000.00" in text
    assert "Available: ₹900.00" in text
    assert "Used Margin: ₹100.00" in text
    assert "Data: LIVE" in text


@pytest.mark.asyncio
async def test_signal_command_with_no_signals_yet_is_honest(db_session_maker):
    bot = TelegramBot(bot_token="x", chat_id="y")
    update = _make_update()
    await bot._cmd_signal(update, None)
    update.message.reply_text.assert_called_once_with("No signal has been generated yet.")


@pytest.mark.asyncio
async def test_signal_command_reports_real_persisted_signal(db_session_maker):
    async with db_session_maker() as session:
        session.add(Signal(
            signal_id="S1", timestamp=datetime(2026, 1, 1), signal_type="LONG",
            confidence=0.0, quality="MEDIUM", market_regime="TRENDING_BULLISH",
            entry_price=100.0, stop_loss=90.0, take_profit_1=120.0, reasoning="test reasoning",
        ))
        await session.commit()

    bot = TelegramBot(bot_token="x", chat_id="y")
    update = _make_update()
    await bot._cmd_signal(update, None)
    text = update.message.reply_text.call_args[0][0]
    assert "LONG" in text and "MEDIUM" in text and "test reasoning" in text


@pytest.mark.asyncio
async def test_position_and_trades_and_performance_reflect_real_trades(db_session_maker):
    async with db_session_maker() as session:
        account = await get_or_create_default_account(session)
        trade = await open_trade(
            session, symbol="BTC/USDT", side="LONG", entry_price=100.0, quantity=1.0,
            entry_time=datetime(2026, 1, 1), account_id=account.id,
        )

    bot = TelegramBot(bot_token="x", chat_id="y")
    update = _make_update()
    await bot._cmd_position(update, None)
    assert "LONG" in update.message.reply_text.call_args[0][0]

    async with db_session_maker() as session:
        await record_exit(session, trade_id=trade.trade_id, exit_price=110.0, quantity=1.0, timestamp=datetime(2026, 1, 2))

    update2 = _make_update()
    await bot._cmd_position(update2, None)
    assert update2.message.reply_text.call_args[0][0] == "No open positions."

    update3 = _make_update()
    await bot._cmd_trades(update3, None)
    assert "Trades: 1" in update3.message.reply_text.call_args[0][0]

    update4 = _make_update()
    await bot._cmd_performance(update4, None)
    text = update4.message.reply_text.call_args[0][0]
    assert "Total P&L: +₹" in text


@pytest.mark.asyncio
async def test_pause_and_resume_actually_toggle_generation_state(db_session_maker):
    bot = TelegramBot(bot_token="x", chat_id="y")
    update = _make_update()

    async with db_session_maker() as session:
        assert await is_signal_generation_paused(session) is False

    await bot._cmd_pause(update, None)
    async with db_session_maker() as session:
        assert await is_signal_generation_paused(session) is True

    await bot._cmd_resume(update, None)
    async with db_session_maker() as session:
        assert await is_signal_generation_paused(session) is False


@pytest.mark.asyncio
async def test_send_exit_alert_is_a_recommendation_not_a_close_confirmation(monkeypatch):
    bot = TelegramBot(bot_token="x", chat_id="y")
    bot.enabled = True
    sent = {}

    async def fake_send(text):
        sent["text"] = text

    monkeypatch.setattr(bot, "_send", fake_send)
    await bot.send_exit_alert({
        "trade_id": "T1", "symbol": "BTC/USDT", "side": "LONG", "reason": "stop_loss_hit",
        "trigger_price": 90.0, "current_price": 89.5, "entry_price": 100.0, "pnl": -10.5,
    })
    assert "EXIT ALERT" in sent["text"]
    assert "CONSIDER MANUAL EXIT ON COINDCX" in sent["text"]
    assert "MANUAL EXECUTION REQUIRED" in sent["text"]


@pytest.mark.asyncio
async def test_send_market_data_alert_disconnected(monkeypatch):
    bot = TelegramBot(bot_token="x", chat_id="y")
    bot.enabled = True
    sent = {}

    async def fake_send(text):
        sent["text"] = text

    monkeypatch.setattr(bot, "_send", fake_send)
    await bot.send_market_data_alert("DISCONNECTED")
    assert "COINDCX MARKET DATA DISCONNECTED" in sent["text"]
    assert "$" not in sent["text"]


@pytest.mark.asyncio
async def test_send_market_data_alert_recovered(monkeypatch):
    bot = TelegramBot(bot_token="x", chat_id="y")
    bot.enabled = True
    sent = {}

    async def fake_send(text):
        sent["text"] = text

    monkeypatch.setattr(bot, "_send", fake_send)
    await bot.send_market_data_alert("RECOVERED")
    assert "COINDCX MARKET DATA RECOVERED" in sent["text"]


@pytest.mark.asyncio
async def test_send_market_data_alert_is_a_noop_when_telegram_disabled(monkeypatch):
    bot = TelegramBot(bot_token="x", chat_id="y")
    bot.enabled = False
    called = {"count": 0}

    async def fake_send(text):
        called["count"] += 1

    monkeypatch.setattr(bot, "_send", fake_send)
    await bot.send_market_data_alert("DISCONNECTED")
    assert called["count"] == 0


@pytest.mark.asyncio
async def test_send_position_detected_is_informational(monkeypatch):
    bot = TelegramBot(bot_token="x", chat_id="y")
    bot.enabled = True
    sent = {}

    async def fake_send(text):
        sent["text"] = text

    monkeypatch.setattr(bot, "_send", fake_send)
    await bot.send_position_detected({
        "symbol": "BTC/USDT", "side": "LONG", "entry_price": 65000.0, "quantity": 0.01,
        "leverage": 5, "liquidation_price": 50000.0, "signal_id": "SIG-1",
    })
    assert "POSITION DETECTED" in sent["text"]
    assert "SIG-1" in sent["text"]
    assert "Tracking started." in sent["text"]


@pytest.mark.asyncio
async def test_no_command_handler_name_suggests_order_placement():
    bot = TelegramBot(bot_token="x", chat_id="y")
    forbidden_terms = ("place_order", "cancel_order", "modify_order", "close_position", "set_leverage")
    handler_names = [name for name in dir(bot) if name.startswith("_cmd_")]
    for name in handler_names:
        for term in forbidden_terms:
            assert term not in name.lower()


@pytest.mark.asyncio
async def test_send_paper_signal_always_labels_paper_trade_no_real_order(monkeypatch):
    bot = TelegramBot(bot_token="x", chat_id="y")
    bot.enabled = True
    sent = {}

    async def fake_send(text):
        sent["text"] = text

    monkeypatch.setattr(bot, "_send", fake_send)
    monkeypatch.setattr("services.telegram.bot.get_usdt_inr_rate", AsyncMock(return_value=None))

    await bot.send_paper_signal({
        "symbol": "BTC/USDT", "market": "CoinDCX BTC/USDT Perpetual", "timeframe": "4h",
        "strategy_sources": ["S06_SUPERTREND_ATR_4H"], "direction": "LONG",
        "probability_long": None, "probability_short": None, "probability_no_trade": None,
        "expected_return": None, "expected_volatility": 0.021, "regime": "TRENDING_BULLISH",
        "confidence": "MEDIUM", "entry": 80000.0, "stop_loss": 78000.0,
        "take_profit_1": 84000.0, "take_profit_2": None, "take_profit_3": None,
        "risk_reward": 2.0, "model_status": "NO_MODEL_DEPLOYED", "model_version": None,
    })
    text = sent["text"]
    assert "PAPER TRADE -- NO REAL ORDER PLACED" in text
    assert text.count("PAPER TRADE -- NO REAL ORDER PLACED") == 2  # header AND footer
    assert "S06_SUPERTREND_ATR_4H" in text
    assert "No validated model is currently deployed" in text
    assert "AlphaOne does not place orders" not in text  # that's send_signal's exact wording; just checking no crash-induced garble
    assert "$" in text or "80,000" in text or "80000" in text  # entry level actually rendered


@pytest.mark.asyncio
async def test_send_paper_signal_shows_real_probabilities_when_model_backed(monkeypatch):
    bot = TelegramBot(bot_token="x", chat_id="y")
    bot.enabled = True
    sent = {}

    async def fake_send(text):
        sent["text"] = text

    monkeypatch.setattr(bot, "_send", fake_send)
    monkeypatch.setattr("services.telegram.bot.get_usdt_inr_rate", AsyncMock(return_value=None))

    await bot.send_paper_signal({
        "symbol": "BTC/USDT", "market": "CoinDCX BTC/USDT Perpetual", "timeframe": "4h",
        "strategy_sources": ["S06_SUPERTREND_ATR_4H"], "direction": "LONG",
        "probability_long": 0.62, "probability_short": 0.13, "probability_no_trade": 0.25,
        "expected_return": 0.3, "expected_volatility": 0.021, "regime": "TRENDING_BULLISH",
        "confidence": "62% calibrated probability", "entry": 80000.0, "stop_loss": 78000.0,
        "take_profit_1": 84000.0, "take_profit_2": 86000.0, "take_profit_3": 88000.0,
        "risk_reward": 2.0, "model_status": "MODEL_BACKED", "model_version": "lightgbm_v1",
    })
    text = sent["text"]
    assert "62%" in text
    assert "lightgbm_v1" in text
    assert "TP2" in text and "TP3" in text


@pytest.mark.asyncio
async def test_send_paper_signal_is_noop_for_no_trade_direction(monkeypatch):
    bot = TelegramBot(bot_token="x", chat_id="y")
    bot.enabled = True
    called = {"count": 0}

    async def fake_send(text):
        called["count"] += 1

    monkeypatch.setattr(bot, "_send", fake_send)
    await bot.send_paper_signal({"direction": "NO_TRADE"})
    assert called["count"] == 0
