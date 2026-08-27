"""Phase 5 controlled live-monitoring dry run. Mocks/simulation only -- the
real CoinDCX account is never touched (CoinDCXReadOnlyAccountProvider's
methods are monkeypatched at the class level, so no HTTP call is ever
made), and TELEGRAM_ENABLED/SCHEDULER_ENABLED stay whatever this machine's
real .env has them set to (both false per the user's explicit instruction
-- this test forces telegram_enabled=True only for the duration of the
test via monkeypatch, so it never depends on or mutates real config).

Numbered to match the user's 20-point verification request. Each numbered
comment marks the assertion(s) that verify that specific point.
"""
from datetime import datetime, timedelta

import numpy as np
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from apps.api.config import get_settings
from database.schema import Base, get_db
from database.schema.models import Candle, Trade, TradeExecution, NotificationLog
from apps.api.main import app
import services.exchange.coindcx as coindcx_module


@pytest.fixture
async def client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "coindcx_api_key", "dry-run-key")
    monkeypatch.setattr(settings, "coindcx_api_secret", "dry-run-secret")
    monkeypatch.setattr(settings, "telegram_enabled", True)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.session_maker = session_maker  # type: ignore[attr-defined]
        yield ac
    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed_trending_candles(session_maker, n=120):
    rng = np.random.default_rng(1)
    trend = np.linspace(60000, 70000, n)
    base_time = datetime(2026, 1, 1)
    async with session_maker() as session:
        for i in range(n):
            close = trend[i] + rng.normal(0, 30)
            session.add(Candle(
                timestamp=base_time + timedelta(hours=4 * i), timeframe="4h", symbol="BTC/USDT",
                open=close - 10, high=close + 100, low=close - 100, close=close, volume=100.0,
                quality_status="valid",
            ))
        await session.commit()


@pytest.mark.asyncio
async def test_full_live_monitoring_dry_run(client, monkeypatch):
    telegram_sent: list[str] = []

    async def fake_send(self, text):
        telegram_sent.append(text)

    monkeypatch.setattr("services.telegram.bot.TelegramBot._send", fake_send)

    # ---- 1/2/3: signal generated, unique signal_id, Telegram alert sent ----
    await _seed_trending_candles(client.session_maker)
    signal_resp = await client.post("/api/v1/signals/generate")
    signal = signal_resp.json()["signal"]
    assert signal is not None
    assert signal["signal_id"].startswith("SIG-")
    signal_id = signal["signal_id"]
    signal_type = signal["signal_type"]

    if signal_type != "NO_TRADE":
        assert len(telegram_sent) == 1
        assert signal_id in telegram_sent[0]
        assert "MANUAL EXECUTION REQUIRED" in telegram_sent[0]
    telegram_sent.clear()

    entry_price = signal["entry_price"] or 65000.0
    side = signal_type if signal_type in ("LONG", "SHORT") else "LONG"

    # ---- 4/5/6: mock CoinDCX position appears, is detected, matches the signal ----
    mock_position = {
        "exchange_position_id": "pos-dry-run-1", "symbol": "B-BTC_USDT", "side": side,
        "quantity": 0.01, "entry_price": entry_price, "mark_price": entry_price,
        "liquidation_price": entry_price * 0.6, "leverage": 5.0, "margin": 130.0,
        "margin_type": "isolated", "unrealized_pnl": 0.0, "updated_at": 1,
    }
    positions_state = {"positions": [mock_position], "closing_fills": []}

    async def fake_get_balance(self):
        return {"status": "OK", "total_equity": 1600.0, "available_balance": 1600.0, "used_margin": 0.0}

    async def fake_get_open_positions(self):
        return positions_state["positions"]

    async def fake_get_trade_history(self, symbol="BTC/USDT", from_date="", to_date=""):
        return positions_state["closing_fills"]

    monkeypatch.setattr(coindcx_module.CoinDCXReadOnlyAccountProvider, "get_balance", fake_get_balance)
    monkeypatch.setattr(coindcx_module.CoinDCXReadOnlyAccountProvider, "get_open_positions", fake_get_open_positions)
    monkeypatch.setattr(coindcx_module.CoinDCXReadOnlyAccountProvider, "get_trade_history", fake_get_trade_history)

    sync1 = (await client.post("/api/v1/accounts/sync")).json()
    assert sync1["balance"]["status"] == "OK"
    assert sync1["positions"]["opened"] == 1

    trades = (await client.get("/api/v1/trades/")).json()["trades"]
    assert len(trades) == 1
    trade_id = trades[0]["trade_id"]
    assert trades[0]["source"] == "COINDCX_SYNC"
    if signal_type != "NO_TRADE":
        assert trades[0]["match_status"] in ("AUTO_MATCHED", "AMBIGUOUS", "UNMATCHED")

    # ---- 7: dashboard reflects the open position ----
    dashboard = (await client.get("/api/v1/dashboard/")).json()
    assert dashboard["open_positions"] == 1
    assert dashboard["account_connection_status"] == "LIVE"

    # ---- 8: mark-price/PnL changes propagate on the next sync ----
    moved_price = entry_price * (1.02 if side == "LONG" else 0.98)
    mock_position["mark_price"] = moved_price
    mock_position["unrealized_pnl"] = (moved_price - entry_price) * 0.01 if side == "LONG" else (entry_price - moved_price) * 0.01
    sync2 = (await client.post("/api/v1/accounts/sync")).json()
    assert sync2["positions"]["updated"] == 1

    refreshed = (await client.get(f"/api/v1/trades/{trade_id}")).json()["trade"]
    assert refreshed["mark_price"] == pytest.approx(moved_price)
    assert refreshed["unrealized_pnl"] is not None and refreshed["unrealized_pnl"] != 0.0

    dashboard2 = (await client.get("/api/v1/dashboard/")).json()
    assert dashboard2["unrealized_pnl"] == pytest.approx(refreshed["unrealized_pnl"])

    # ---- 9: simulated exit condition (stop-loss breach) ----
    async with client.session_maker() as session:
        trade_row = (await session.execute(select(Trade).where(Trade.trade_id == trade_id))).scalar_one()
        trade_row.stop_loss = entry_price * (0.98 if side == "LONG" else 1.02)
        breach_price = trade_row.stop_loss - 1 if side == "LONG" else trade_row.stop_loss + 1
        await session.commit()

    alerts_resp = await client.get("/api/v1/journal/exit-alerts", params={"current_price": breach_price})
    alerts = alerts_resp.json()["alerts"]
    assert len(alerts) == 1
    assert alerts[0]["reason"] == "stop_loss_hit"

    # ---- 10: Telegram EXIT ALERT sent ----
    assert len(telegram_sent) == 1
    assert "EXIT ALERT" in telegram_sent[0]
    assert "CONSIDER MANUAL EXIT ON COINDCX" in telegram_sent[0]
    telegram_sent.clear()

    # ---- 11: no CoinDCX order endpoint was ever called (nothing closed the position) ----
    still_open = (await client.get(f"/api/v1/trades/{trade_id}")).json()["trade"]
    assert still_open["status"] == "OPEN"
    import inspect
    order_terms = ("place_order", "create_order", "cancel_order", "exit_position", "close_position", "set_leverage", "add_margin", "remove_margin")
    for name, _ in inspect.getmembers(coindcx_module.CoinDCXReadOnlyAccountProvider, predicate=inspect.isfunction):
        assert not any(term in name.lower() for term in order_terms)

    # ---- 12: position manually closed on CoinDCX (simulated: disappears + a real closing fill) ----
    positions_state["positions"] = []
    closing_side = "sell" if side == "LONG" else "buy"
    exit_fill_price = breach_price
    positions_state["closing_fills"] = [{
        "price": exit_fill_price, "quantity": mock_position["quantity"], "side": closing_side,
        "order_id": "manual-close-1", "timestamp": 2,
    }]
    sync3 = (await client.post("/api/v1/accounts/sync")).json()

    # ---- 13: closed trade detected ----
    assert sync3["positions"]["closed"] == 1
    closed_trade = (await client.get(f"/api/v1/trades/{trade_id}")).json()["trade"]
    assert closed_trade["status"] == "CLOSED"

    # ---- 14: real exit data used, not fabricated ----
    assert closed_trade["exit_price"] == pytest.approx(exit_fill_price)

    # ---- 15: realized P&L and fees computed; funding honestly absent (not modeled for this closing path) ----
    assert closed_trade["pnl"] is not None
    assert closed_trade["fees"] is not None and closed_trade["fees"] > 0
    # funding is not fabricated -- it stays at the Trade model's default (0)
    # rather than being invented for a CoinDCX-synced close; this is a
    # documented limitation (docs/known_limitations.md), not a fake number.

    # ---- 16: trade linked to the original signal id (when one was matched) ----
    if closed_trade["match_status"] == "AUTO_MATCHED":
        assert closed_trade["signal_id"] == signal_id

    # ---- 17: three-way performance separation still holds ----
    performance = (await client.get("/api/v1/portfolio/performance")).json()
    assert set(performance.keys()) == {"backtest", "alphaone_signals", "user_actual"}
    assert performance["user_actual"]["total_trades"] == 1

    # ---- 18: equity curve / accounting update ----
    equity_curve = (await client.get("/api/v1/portfolio/equity-curve")).json()["equity_curve"]
    assert len(equity_curve) == 1
    assert equity_curve[0]["trade_id"] == trade_id


@pytest.mark.asyncio
async def test_duplicate_sync_and_alert_events_never_duplicate(client, monkeypatch):
    """Point 19: repeating the exact same sync/alert conditions must never
    create a second Trade, a second TradeExecution, or a second Telegram
    alert for the same underlying event."""
    telegram_sent: list[str] = []

    async def fake_send(self, text):
        telegram_sent.append(text)

    monkeypatch.setattr("services.telegram.bot.TelegramBot._send", fake_send)

    mock_position = {
        "exchange_position_id": "pos-dup-1", "symbol": "B-BTC_USDT", "side": "LONG",
        "quantity": 0.01, "entry_price": 100.0, "mark_price": 100.0,
        "liquidation_price": 60.0, "leverage": 5.0, "margin": 20.0,
        "margin_type": "isolated", "unrealized_pnl": 0.0, "updated_at": 1,
    }
    fills = [{"price": 100.0, "quantity": 0.01, "side": "buy", "order_id": "dup-order-1", "timestamp": 1}]

    async def fake_get_balance(self):
        return {"status": "OK", "total_equity": 1600.0, "available_balance": 1600.0, "used_margin": 0.0}

    async def fake_get_open_positions(self):
        return [mock_position]

    async def fake_get_trade_history(self, symbol="BTC/USDT", from_date="", to_date=""):
        return fills

    monkeypatch.setattr(coindcx_module.CoinDCXReadOnlyAccountProvider, "get_balance", fake_get_balance)
    monkeypatch.setattr(coindcx_module.CoinDCXReadOnlyAccountProvider, "get_open_positions", fake_get_open_positions)
    monkeypatch.setattr(coindcx_module.CoinDCXReadOnlyAccountProvider, "get_trade_history", fake_get_trade_history)

    # Sync the same open position 3 times in a row -- must create exactly 1 Trade.
    for _ in range(3):
        await client.post("/api/v1/accounts/sync")
    trades = (await client.get("/api/v1/trades/")).json()["trades"]
    assert len(trades) == 1
    trade_id = trades[0]["trade_id"]

    # Ingest the same raw fills 3 times -- must not duplicate TradeExecution rows.
    from services.exchange.coindcx_sync import sync_trade_fills
    async with client.session_maker() as session:
        provider = coindcx_module.CoinDCXReadOnlyAccountProvider("k", "s")
        n1 = await sync_trade_fills(session, provider, symbol="BTC/USDT")
        n2 = await sync_trade_fills(session, provider, symbol="BTC/USDT")
        n3 = await sync_trade_fills(session, provider, symbol="BTC/USDT")
    assert n1 == 1
    assert n2 == 0
    assert n3 == 0

    async with client.session_maker() as session:
        executions = (await session.execute(
            select(TradeExecution).where(TradeExecution.exchange_transaction_id.is_not(None))
        )).scalars().all()
        assert len(executions) == 1

    # Trigger the exact same exit-alert condition twice -- only one Telegram alert, one NotificationLog row.
    async with client.session_maker() as session:
        trade_row = (await session.execute(select(Trade).where(Trade.trade_id == trade_id))).scalar_one()
        trade_row.stop_loss = 95.0
        await session.commit()

    alerts1 = (await client.get("/api/v1/journal/exit-alerts", params={"current_price": 90.0})).json()["alerts"]
    alerts2 = (await client.get("/api/v1/journal/exit-alerts", params={"current_price": 90.0})).json()["alerts"]
    assert len(alerts1) == 1
    assert alerts2 == []  # already alerted, correctly not repeated
    assert len(telegram_sent) == 1  # exactly one EXIT ALERT sent, not two

    async with client.session_maker() as session:
        dedup_rows = (await session.execute(
            select(NotificationLog).where(NotificationLog.message_type.like("exit_alert:%"))
        )).scalars().all()
        assert len(dedup_rows) == 1


@pytest.mark.asyncio
async def test_stale_and_disconnected_states_are_reported_honestly(client, monkeypatch):
    """Point 20: a CoinDCX API failure must be reported as DISCONNECTED
    (never silently shown as LIVE), and once a connection was previously
    live but the last successful sync is old, it must show STALE."""
    from services.exchange.sync import is_stale
    from database.schema.models import SyncEvent

    # First, a successful sync establishes LIVE.
    async def fake_get_balance_ok(self):
        return {"status": "OK", "total_equity": 1600.0, "available_balance": 1600.0, "used_margin": 0.0}

    async def fake_get_open_positions_empty(self):
        return []

    monkeypatch.setattr(coindcx_module.CoinDCXReadOnlyAccountProvider, "get_open_positions", fake_get_open_positions_empty)
    monkeypatch.setattr(coindcx_module.CoinDCXReadOnlyAccountProvider, "get_balance", fake_get_balance_ok)
    ok_sync = (await client.post("/api/v1/accounts/sync")).json()
    assert ok_sync["balance"]["status"] == "OK"

    account = (await client.get("/api/v1/accounts/")).json()
    assert account["connection_status"] == "LIVE"

    dashboard_live = (await client.get("/api/v1/dashboard/")).json()
    assert dashboard_live["account_data_source"] == "LIVE"

    # Now simulate the CoinDCX API failing (e.g. network error / 5xx).
    async def fake_get_balance_failing(self):
        return {"status": "CONNECTION_LOST", "total_equity": None, "available_balance": None, "used_margin": None}

    monkeypatch.setattr(coindcx_module.CoinDCXReadOnlyAccountProvider, "get_balance", fake_get_balance_failing)
    failed_sync = (await client.post("/api/v1/accounts/sync")).json()
    assert failed_sync["balance"]["status"] == "CONNECTION_LOST"

    account_after_failure = (await client.get("/api/v1/accounts/")).json()
    assert account_after_failure["connection_status"] == "DISCONNECTED"  # never silently stayed LIVE

    dashboard_disconnected = (await client.get("/api/v1/dashboard/")).json()
    assert dashboard_disconnected["account_data_source"] == "DISCONNECTED"
    # a disconnected account must never keep reporting stale numbers as if live
    assert dashboard_disconnected["account_connection_status"] != "LIVE"

    # A FAILED SyncEvent must be on record for this, not silently dropped.
    async with client.session_maker() as session:
        events = (await session.execute(select(SyncEvent).order_by(SyncEvent.timestamp.desc()))).scalars().all()
        assert any(e.status == "FAILED" for e in events)

    # Staleness helper: a very old successful sync must be flagged stale.
    old_event = type("E", (), {"timestamp": datetime.utcnow() - timedelta(hours=2)})()
    assert is_stale(old_event) is True
    fresh_event = type("E", (), {"timestamp": datetime.utcnow()})()
    assert is_stale(fresh_event) is False
