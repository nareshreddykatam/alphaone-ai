"""Phase 5 API-level integration tests -- CoinDCX account endpoints,
against a real (in-memory) SQLite DB via the same ASGITransport pattern as
Phase 4's flow tests. No real CoinDCX network access.

Forces CoinDCX credentials to empty for the duration of these tests via
monkeypatch on the shared Settings singleton -- these tests specifically
verify the "no credentials configured" behavior, which must be
deterministic and never depend on whether this machine's real .env
happens to have real CoinDCX credentials in it (it now does, for the
real-account connectivity test -- see scripts/coindcx_connectivity_test.py
and reports/PHASE_5_FINAL_REPORT.txt section 21).
"""
from datetime import datetime

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from apps.api.config import get_settings
from database.schema import Base, get_db
from database.schema.models import Signal
from apps.api.main import app


@pytest.fixture
async def client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "coindcx_api_key", "")
    monkeypatch.setattr(settings, "coindcx_api_secret", "")

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


@pytest.mark.asyncio
async def test_account_endpoint_reports_coindcx_as_the_active_exchange(client):
    resp = await client.get("/api/v1/accounts/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["exchange"] == "coindcx"
    assert body["connection_status"] == "NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_balance_and_positions_are_honest_without_credentials(client):
    balance = (await client.get("/api/v1/accounts/balance")).json()
    assert balance["status"] == "NOT_CONFIGURED"
    assert balance["total_equity"] is None

    positions = (await client.get("/api/v1/accounts/positions")).json()
    assert positions["positions"] == []


@pytest.mark.asyncio
async def test_sync_without_credentials_never_claims_live(client):
    resp = await client.post("/api/v1/accounts/sync")
    body = resp.json()
    assert body["balance"]["status"] == "NOT_CONFIGURED"
    assert body["positions"] is None

    account = (await client.get("/api/v1/accounts/")).json()
    assert account["connection_status"] == "NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_dashboard_shows_coindcx_branding_and_honest_freshness(client):
    resp = await client.get("/api/v1/dashboard/")
    body = resp.json()
    assert resp.status_code == 200
    assert body["account_connection_status"] == "NOT_CONFIGURED"
    assert body["btc_price_inr"] is None
    assert body["unrealized_pnl"] is None


@pytest.mark.asyncio
async def test_trade_serialization_includes_phase5_match_and_sync_fields(client):
    """Regression test: found via manual browser verification that
    trades.py/journal.py's _serialize() omitted match_status and the live
    sync fields entirely, so the frontend's Match/Source columns silently
    rendered "--" for every trade."""
    open_resp = await client.post("/api/v1/journal/open", json={
        "side": "LONG", "entry_price": 100.0, "quantity": 1.0,
    })
    trade = open_resp.json()
    for field in ("match_status", "data_source", "mark_price", "unrealized_pnl", "liquidation_price", "margin"):
        assert field in trade, f"{field} missing from POST /journal/open response"
    assert trade["match_status"] == "MANUAL"

    list_resp = await client.get("/api/v1/trades/")
    listed = list_resp.json()["trades"][0]
    for field in ("match_status", "data_source", "mark_price", "unrealized_pnl", "liquidation_price", "margin"):
        assert field in listed, f"{field} missing from GET /trades/ response"


@pytest.mark.asyncio
async def test_signal_generation_sends_exactly_one_telegram_alert(client, monkeypatch):
    import numpy as np
    from datetime import datetime, timedelta
    from database.schema.models import Candle

    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_enabled", True)

    sent = []

    async def fake_send(self, text):
        sent.append(text)

    monkeypatch.setattr("services.telegram.bot.TelegramBot._send", fake_send)

    rng = np.random.default_rng(0)
    trend = np.linspace(60000, 70000, 120)
    base_time = datetime(2026, 1, 1)
    async with client.session_maker() as session:
        for i in range(120):
            close = trend[i] + rng.normal(0, 30)
            session.add(Candle(
                timestamp=base_time + timedelta(hours=4 * i), timeframe="4h", symbol="BTC/USDT",
                open=close - 10, high=close + 100, low=close - 100, close=close, volume=100.0,
                quality_status="valid",
            ))
        await session.commit()

    resp = await client.post("/api/v1/signals/generate")
    signal = resp.json()["signal"]

    if signal is not None and signal["signal_type"] != "NO_TRADE":
        assert len(sent) == 1
        assert signal["signal_id"] in sent[0]
        assert "MANUAL EXECUTION REQUIRED" in sent[0]


@pytest.mark.asyncio
async def test_notify_new_signal_dedups_by_signal_id(client, monkeypatch):
    """Exercises the real notify_new_signal() function (used by the
    /generate endpoint) directly -- calling it twice for the same Signal
    row must only send once."""
    from database.schema.models import Signal
    from services.signal_engine.notify import notify_new_signal

    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_enabled", True)

    sent = []

    async def fake_send(self, text):
        sent.append(text)

    monkeypatch.setattr("services.telegram.bot.TelegramBot._send", fake_send)

    async with client.session_maker() as session:
        signal = Signal(
            signal_id="SIG-DEDUP-TEST", timestamp=datetime.utcnow(), symbol="BTC/USDT",
            signal_type="LONG", confidence=0.0, entry_price=100.0, stop_loss=90.0, take_profit_1=120.0,
        )
        session.add(signal)
        await session.commit()

        first = await notify_new_signal(session, signal)
        second = await notify_new_signal(session, signal)

        assert first is True
        assert second is False
        assert len(sent) == 1
        assert "SIG-DEDUP-TEST" in sent[0]


@pytest.mark.asyncio
async def test_notify_new_signal_never_sends_for_no_trade(client, monkeypatch):
    from database.schema.models import Signal
    from services.signal_engine.notify import notify_new_signal

    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_enabled", True)

    sent = []

    async def fake_send(self, text):
        sent.append(text)

    monkeypatch.setattr("services.telegram.bot.TelegramBot._send", fake_send)

    async with client.session_maker() as session:
        signal = Signal(
            signal_id="SIG-NOTRADE", timestamp=datetime.utcnow(), symbol="BTC/USDT",
            signal_type="NO_TRADE", confidence=0.0,
        )
        session.add(signal)
        await session.commit()

        result = await notify_new_signal(session, signal)
        assert result is False
        assert sent == []


@pytest.mark.asyncio
async def test_market_candles_and_signals_endpoints_still_work_after_exchange_swap(client):
    # Sanity check that swapping the active exchange didn't break the
    # exchange-agnostic parts of Phase 4 (candles come from the DB, not CoinDCX).
    resp = await client.get("/api/v1/market/candles", params={"symbol": "BTC/USDT", "timeframe": "4h"})
    assert resp.status_code == 200
    assert resp.json()["candles"] == []

    resp2 = await client.get("/api/v1/signals/")
    assert resp2.status_code == 200
