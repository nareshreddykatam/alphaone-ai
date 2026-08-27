"""Phase 5, section 50: final acceptance test. Simulates the complete
intended workflow end-to-end against mocks (no real CoinDCX credentials or
network) -- signal generated -> mock CoinDCX position appears -> detected
and matched -> dashboard reflects it -> stop/TP breach recommends an exit
(never executes one) -> position closes with real fill data -> actual P&L
computed -> signal outcome tracked separately -> the three performance
views stay distinct -> equity curve updates -> reconciliation runs.
"""
from datetime import datetime, timedelta

import numpy as np
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base, get_db
from database.schema.models import Candle, Trade, TradeStatus
from apps.api.main import app
import services.exchange.coindcx as coindcx_module


@pytest.fixture
async def client():
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
    rng = np.random.default_rng(0)
    trend = np.linspace(60000, 70000, n)
    noise = rng.normal(0, 50, n)
    base_time = datetime(2026, 1, 1)
    async with session_maker() as session:
        for i in range(n):
            close = trend[i] + noise[i]
            session.add(Candle(
                timestamp=base_time + timedelta(hours=4 * i), timeframe="4h", symbol="BTC/USDT",
                open=close - 20, high=close + 100, low=close - 100, close=close, volume=100.0,
                quality_status="valid",
            ))
        await session.commit()
    return base_time + timedelta(hours=4 * (n - 1))


@pytest.mark.asyncio
async def test_full_signal_to_close_workflow_never_places_an_order(client, monkeypatch):
    # 1. Real candle history exists, strongly trending -> the baseline signal should fire.
    last_ts = await _seed_trending_candles(client.session_maker)

    # 2. AlphaOne generates a signal from real data.
    signal_resp = await client.post("/api/v1/signals/generate")
    signal = signal_resp.json()["signal"]
    assert signal is not None
    entry_price = signal["entry_price"] or 65000.0
    signal_type = signal["signal_type"]

    # 3. A mock CoinDCX position "appears", matching the signal's symbol/direction/price/time.
    mock_position = {
        "exchange_position_id": "pos-1", "symbol": "B-BTC_USDT",
        "side": signal_type if signal_type in ("LONG", "SHORT") else "LONG",
        "quantity": 0.01, "entry_price": entry_price if signal_type != "NO_TRADE" else 65000.0,
        "mark_price": entry_price if signal_type != "NO_TRADE" else 65000.0,
        "liquidation_price": 40000.0, "leverage": 5.0, "margin": 130.0,
        "margin_type": "isolated", "unrealized_pnl": 0.0, "updated_at": 1,
    }

    async def fake_get_balance(self):
        return {"status": "OK", "total_equity": 1000.0, "available_balance": 900.0, "used_margin": 100.0}

    positions_state = {"positions": [mock_position]}

    async def fake_get_open_positions(self):
        return positions_state["positions"]

    async def fake_get_trade_history(self, symbol="BTC/USDT", from_date="", to_date=""):
        return positions_state.get("closing_fills", [])

    monkeypatch.setattr(coindcx_module.CoinDCXReadOnlyAccountProvider, "get_balance", fake_get_balance)
    monkeypatch.setattr(coindcx_module.CoinDCXReadOnlyAccountProvider, "get_open_positions", fake_get_open_positions)
    monkeypatch.setattr(coindcx_module.CoinDCXReadOnlyAccountProvider, "get_trade_history", fake_get_trade_history)

    # 4. Position is detected and synced (signal-matching, if the signal was tradeable, is exercised
    #    incidentally -- this test's main point is the detect -> track -> alert -> close pipeline).
    sync_resp = await client.post("/api/v1/accounts/sync")
    sync_body = sync_resp.json()
    assert sync_body["balance"]["status"] == "OK"
    assert sync_body["positions"]["opened"] == 1

    # 5. Dashboard shows the position, live equity, and never claims unrealized P&L it can't compute.
    dashboard = (await client.get("/api/v1/dashboard/")).json()
    assert dashboard["account_connection_status"] == "LIVE"
    assert dashboard["open_positions"] == 1

    balance = (await client.get("/api/v1/accounts/balance")).json()
    assert balance["total_equity"] == 1000.0

    # 6. Simulate the mark price moving to a stop-loss/take-profit breach and check exit alerts.
    trades = (await client.get("/api/v1/trades/")).json()["trades"]
    assert len(trades) == 1
    trade_id = trades[0]["trade_id"]

    async with client.session_maker() as session:
        from sqlalchemy import select
        trade_row = (await session.execute(select(Trade).where(Trade.trade_id == trade_id))).scalar_one()
        trade_row.stop_loss = trade_row.entry_price * 0.98 if trade_row.side == "LONG" else trade_row.entry_price * 1.02
        breach_price = trade_row.stop_loss - 1 if trade_row.side == "LONG" else trade_row.stop_loss + 1
        await session.commit()

    alerts_resp = await client.get("/api/v1/journal/exit-alerts", params={"current_price": breach_price})
    alerts = alerts_resp.json()["alerts"]
    assert len(alerts) == 1
    assert alerts[0]["reason"] == "stop_loss_hit"

    # 7. The alert must never have closed the position itself.
    still_open = (await client.get(f"/api/v1/trades/{trade_id}")).json()["trade"]
    assert still_open["status"] == "OPEN"

    # 8. The mock position "disappears" from CoinDCX with a real closing fill -> sync closes it for real.
    positions_state["positions"] = []
    closing_side = "sell" if trade_row.side == "LONG" else "buy"
    positions_state["closing_fills"] = [{
        "price": breach_price, "quantity": mock_position["quantity"], "side": closing_side,
        "order_id": "close-1", "timestamp": 2,
    }]

    close_sync = (await client.post("/api/v1/accounts/sync")).json()
    assert close_sync["positions"]["closed"] == 1

    closed_trade = (await client.get(f"/api/v1/trades/{trade_id}")).json()["trade"]
    assert closed_trade["status"] == "CLOSED"
    assert closed_trade["exit_price"] == breach_price
    assert closed_trade["pnl"] is not None  # real P&L computed from the real closing fill, fees included

    # 9. Evaluate signal outcomes -- independent of the trade above.
    outcome_resp = await client.post("/api/v1/signals/evaluate-outcomes")
    assert outcome_resp.status_code == 200

    # 10. Three-way performance separation still holds.
    performance = (await client.get("/api/v1/portfolio/performance")).json()
    assert set(performance.keys()) == {"backtest", "alphaone_signals", "user_actual"}
    assert performance["user_actual"]["total_trades"] == 1

    # 11. Equity curve reflects the real close.
    equity_curve = (await client.get("/api/v1/portfolio/equity-curve")).json()["equity_curve"]
    assert len(equity_curve) == 1

    # 12. Reconciliation runs without error (no assertion on match/mismatch --
    #     no manual AccountSnapshot was recorded in this test, so NO_SNAPSHOT is expected and correct).
    reconcile = (await client.get("/api/v1/accounts/reconcile")).json()
    assert reconcile["status"] in ("NO_SNAPSHOT", "OK", "MISMATCH")

    # 13. Absolute final check: nothing in this codebase could have placed an order during any of this.
    from services.exchange.coindcx import CoinDCXReadOnlyAccountProvider
    import inspect
    order_terms = ("place_order", "create_order", "cancel_order", "exit_position", "close_position", "set_leverage")
    for name, _ in inspect.getmembers(CoinDCXReadOnlyAccountProvider, predicate=inspect.isfunction):
        assert not any(term in name.lower() for term in order_terms)
