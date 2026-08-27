"""End-to-end Phase 4 API flow against a real (in-memory) SQLite DB, not the
project's real research database -- overrides the `get_db` FastAPI
dependency so no test ever touches alphaone_research.db. Exercises: manual
trade open -> exit -> risk-engine feed -> three-way performance separation
-> dashboard honesty when nothing is connected/available yet.

Also forces CoinDCX credentials to empty for the duration of these tests
(see conftest-style note in test_api_phase5_coindcx_flow.py) so results
are deterministic regardless of whether real credentials happen to be
configured in this machine's .env -- the "no credentials" scenario these
tests check must never depend on ambient environment state.
"""
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from apps.api.config import get_settings
from database.schema import Base, get_db
from database.schema.models import Candle, Signal
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
async def test_dashboard_is_honest_with_no_data(client):
    resp = await client.get("/api/v1/dashboard/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["account_connection_status"] == "NOT_CONFIGURED"  # Phase 5: no CoinDCX credentials configured in tests
    assert body["btc_price_inr"] is None
    assert body["btc_price_usdt"] is None
    assert body["btc_price_source"] == "UNAVAILABLE"
    assert body["current_signal"] is None
    assert body["unrealized_pnl"] is None  # never fabricated


@pytest.mark.asyncio
async def test_open_and_exit_manual_trade_via_api(client):
    open_resp = await client.post("/api/v1/journal/open", json={
        "symbol": "BTC/USDT", "side": "LONG", "entry_price": 100.0, "quantity": 1.0,
        "stop_loss": 90.0, "take_profit_1": 120.0,
    })
    assert open_resp.status_code == 200
    trade = open_resp.json()
    assert trade["status"] == "OPEN"
    assert trade["is_manual_entry"] is True

    exit_resp = await client.post(f"/api/v1/journal/{trade['trade_id']}/exit", json={
        "exit_price": 110.0, "quantity": 1.0, "reason": "manual_take_profit",
    })
    assert exit_resp.status_code == 200
    closed = exit_resp.json()
    assert closed["status"] == "CLOSED"
    assert closed["pnl"] > 0

    # the closed trade must show up in trade history
    history = await client.get("/api/v1/trades/")
    assert history.json()["count"] == 1

    # and must have fed the (informational) risk dashboard
    risk = await client.get("/api/v1/risk/")
    assert risk.json()["trades_today"] == 1


@pytest.mark.asyncio
async def test_cannot_exit_more_than_open_quantity_via_api(client):
    open_resp = await client.post("/api/v1/journal/open", json={
        "side": "LONG", "entry_price": 100.0, "quantity": 1.0,
    })
    trade_id = open_resp.json()["trade_id"]
    resp = await client.post(f"/api/v1/journal/{trade_id}/exit", json={"exit_price": 105.0, "quantity": 5.0})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_portfolio_performance_keeps_three_views_separate(client):
    await client.post("/api/v1/journal/open", json={"side": "LONG", "entry_price": 100.0, "quantity": 1.0})
    trades = (await client.get("/api/v1/trades/")).json()["trades"]
    trade_id = trades[0]["trade_id"]
    await client.post(f"/api/v1/journal/{trade_id}/exit", json={"exit_price": 150.0, "quantity": 1.0})

    resp = await client.get("/api/v1/portfolio/performance")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"backtest", "alphaone_signals", "user_actual"}
    assert body["backtest"] is None  # no BacktestRun rows seeded in this test DB
    assert body["user_actual"]["total_trades"] == 1
    assert body["user_actual"]["total_pnl"] > 0
    assert body["alphaone_signals"]["total_signals"] == 0  # independent of the trade above


@pytest.mark.asyncio
async def test_reset_hard_kill_endpoint_is_the_only_way_to_clear_it(client):
    for _ in range(1):
        open_resp = await client.post("/api/v1/journal/open", json={"side": "LONG", "entry_price": 100.0, "quantity": 100.0})
        trade_id = open_resp.json()["trade_id"]
        # a catastrophic loss to breach the default 10% max-drawdown hard kill
        await client.post(f"/api/v1/journal/{trade_id}/exit", json={"exit_price": 1.0, "quantity": 100.0})

    status = (await client.get("/api/v1/risk/")).json()
    assert status["risk_status"] == "HARD_KILL"
    assert status["kill_switch_active"] is True

    reset = await client.post("/api/v1/risk/reset-hard-kill")
    assert reset.json()["kill_switch_active"] is False


@pytest.mark.asyncio
async def test_market_candles_endpoint_returns_chronological_ohlcv(client):
    async with client.session_maker() as session:
        base_time = datetime(2026, 1, 1)
        for i in range(5):
            session.add(Candle(
                timestamp=base_time + timedelta(hours=4 * i), timeframe="4h", symbol="BTC/USDT",
                open=100 + i, high=101 + i, low=99 + i, close=100.5 + i, volume=10.0,
                quality_status="valid",
            ))
        await session.commit()

    resp = await client.get("/api/v1/market/candles", params={"symbol": "BTC/USDT", "timeframe": "4h"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["candles"]) == 5
    times = [c["time"] for c in body["candles"]]
    assert times == sorted(times)  # chronological, not reversed
    assert body["markers"] == []


@pytest.mark.asyncio
async def test_manual_trade_auto_matches_a_confident_signal(client):
    entry_time = datetime(2026, 1, 1, 12, 0)
    async with client.session_maker() as session:
        session.add(Signal(
            signal_id="SIG-MATCH", timestamp=entry_time, symbol="BTC/USDT",
            signal_type="LONG", confidence=0.0, entry_price=100.0,
        ))
        await session.commit()

    resp = await client.post("/api/v1/journal/open", json={
        "side": "LONG", "entry_price": 100.0, "quantity": 1.0,
        "entry_time": entry_time.isoformat(),
    })
    trade = resp.json()
    assert trade["signal_id"] == "SIG-MATCH"
    assert trade["match_candidates"] == []


@pytest.mark.asyncio
async def test_manual_trade_surfaces_ambiguous_candidates_for_confirmation(client):
    entry_time = datetime(2026, 1, 1, 12, 0)
    async with client.session_maker() as session:
        session.add(Signal(signal_id="SIG-A", timestamp=entry_time, symbol="BTC/USDT", signal_type="LONG", confidence=0.0, entry_price=100.0))
        session.add(Signal(signal_id="SIG-B", timestamp=entry_time, symbol="BTC/USDT", signal_type="LONG", confidence=0.0, entry_price=100.05))
        await session.commit()

    resp = await client.post("/api/v1/journal/open", json={
        "side": "LONG", "entry_price": 100.02, "quantity": 1.0,
        "entry_time": entry_time.isoformat(),
    })
    trade = resp.json()
    assert trade["signal_id"] is None
    assert len(trade["match_candidates"]) == 2

    confirm = await client.post(f"/api/v1/journal/{trade['trade_id']}/confirm-match", json={"signal_id": "SIG-B", "confidence": 0.9})
    assert confirm.json()["signal_id"] == "SIG-B"


@pytest.mark.asyncio
async def test_exit_alerts_endpoint_recommends_without_closing(client):
    open_resp = await client.post("/api/v1/journal/open", json={
        "side": "LONG", "entry_price": 100.0, "quantity": 1.0, "stop_loss": 90.0, "take_profit_1": 120.0,
    })
    trade_id = open_resp.json()["trade_id"]

    resp = await client.get("/api/v1/journal/exit-alerts", params={"current_price": 89.0})
    body = resp.json()
    assert len(body["alerts"]) == 1
    assert body["alerts"][0]["reason"] == "stop_loss_hit"

    # the trade must still be open -- this only recommends
    trade_check = await client.get(f"/api/v1/trades/{trade_id}")
    assert trade_check.json()["trade"]["status"] == "OPEN"

    # a second check at the same price must not re-alert
    resp2 = await client.get("/api/v1/journal/exit-alerts", params={"current_price": 89.0})
    assert resp2.json()["alerts"] == []
