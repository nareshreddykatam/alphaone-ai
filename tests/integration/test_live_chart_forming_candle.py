"""Integration coverage for GET /api/v1/market/candles' forming_candle
field -- the fix for a real, measured gap found during the live-price
audit: the chart's "latest" bar was always the last COMPLETED historical
candle, which could sit stale for up to a full timeframe period (measured
5h40m for 4h) even while the dashboard already showed a live price.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app
from apps.api.config import get_settings
from database.schema import Base, get_db
from database.schema.models import Candle, ConnectionState
from services.market_data.live_state import market_ws, live_candle_aggregator


@pytest.fixture(autouse=True)
def _reset_shared_live_state():
    """market_ws and live_candle_aggregator are both process-wide
    singletons shared with every other test file -- reset before and
    after every test here so nothing leaks in either direction."""
    def _reset():
        market_ws.state.last_price_usdt = None
        market_ws.state.mark_price_usdt = None
        market_ws.state.received_at = None
        market_ws._connected = False
        market_ws._ever_connected = False
        live_candle_aggregator.current = None

    _reset()
    yield
    _reset()


@pytest.fixture
async def client(monkeypatch):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    settings = get_settings()
    monkeypatch.setattr(settings, "coindcx_api_key", "")
    monkeypatch.setattr(settings, "coindcx_api_secret", "")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, session_maker
    app.dependency_overrides.clear()
    await engine.dispose()


def _make_live(price=79700.0, received_at=None):
    market_ws._connected = True
    market_ws._ever_connected = True
    market_ws.state.last_price_usdt = price
    market_ws.state.received_at = received_at or datetime.utcnow()


async def _seed_candle(session_maker, timestamp, close=79600.0, timeframe="4h", symbol="BTC/USDT"):
    async with session_maker() as session:
        session.add(Candle(
            timestamp=timestamp, timeframe=timeframe, symbol=symbol,
            open=close - 10, high=close + 20, low=close - 20, close=close, volume=100.0,
            quality_status="valid",
        ))
        await session.commit()


# ---- 23/24. Dashboard/chart price consistency, live chart behavior ----

@pytest.mark.asyncio
async def test_forming_candle_is_null_when_market_data_is_not_live(client):
    ac, session_maker = client
    await _seed_candle(session_maker, datetime(2026, 1, 1, 0, 0, 0))
    resp = await ac.get("/api/v1/market/candles", params={"symbol": "BTC/USDT", "timeframe": "4h"})
    body = resp.json()
    assert body["forming_candle"] is None
    assert body["market_data_status"] == "UNAVAILABLE"
    assert len(body["candles"]) == 1  # existing historical behavior unaffected


@pytest.mark.asyncio
async def test_forming_candle_present_when_market_data_is_live(client):
    ac, session_maker = client
    now = datetime.utcnow()
    _make_live(price=79700.0, received_at=now)
    resp = await ac.get("/api/v1/market/candles", params={"symbol": "BTC/USDT", "timeframe": "4h"})
    body = resp.json()
    assert body["market_data_status"] == "LIVE"
    assert body["forming_candle"] is not None
    assert body["forming_candle"]["close"] == 79700.0
    assert body["forming_candle"]["open"] == 79700.0  # first tick this bar -- open == close


@pytest.mark.asyncio
async def test_forming_candle_reflects_high_low_across_repeated_ticks(client):
    ac, session_maker = client
    now = datetime.utcnow()
    _make_live(price=79700.0, received_at=now)
    await ac.get("/api/v1/market/candles", params={"symbol": "BTC/USDT", "timeframe": "4h"})

    _make_live(price=79900.0, received_at=now + timedelta(seconds=5))
    await ac.get("/api/v1/market/candles", params={"symbol": "BTC/USDT", "timeframe": "4h"})

    _make_live(price=79500.0, received_at=now + timedelta(seconds=10))
    resp = await ac.get("/api/v1/market/candles", params={"symbol": "BTC/USDT", "timeframe": "4h"})
    body = resp.json()
    forming = body["forming_candle"]
    assert forming["open"] == 79700.0  # unchanged since the first tick
    assert forming["high"] == 79900.0
    assert forming["low"] == 79500.0
    assert forming["close"] == 79500.0
    assert forming["tick_count"] == 3


@pytest.mark.asyncio
async def test_forming_candle_only_computed_for_the_4h_timeframe(client):
    """Other chart tabs (15m/1h/1d) keep showing only historical data --
    the shared aggregator is fixed to 4h, matching the validated strategy,
    not duplicated per tab."""
    ac, session_maker = client
    _make_live(price=79700.0)
    resp = await ac.get("/api/v1/market/candles", params={"symbol": "BTC/USDT", "timeframe": "15m"})
    body = resp.json()
    assert body["forming_candle"] is None


@pytest.mark.asyncio
async def test_forming_candle_inr_conversion_present_when_rate_available(client, monkeypatch):
    ac, session_maker = client
    _make_live(price=79700.0)

    from services.exchange import fx
    fx._reset_cache_for_tests()

    async def fake_rate(client=None, now=None):
        return fx.ConversionRate(rate=100.0, rate_timestamp=1.0, fetched_at=1e18)

    monkeypatch.setattr("apps.api.routers.market.get_usdt_inr_rate", fake_rate)
    resp = await ac.get("/api/v1/market/candles", params={"symbol": "BTC/USDT", "timeframe": "4h"})
    body = resp.json()
    assert body["forming_candle"]["close_inr"] == 79700.0 * 100.0
    assert body["conversion_status"] == "LIVE"


@pytest.mark.asyncio
async def test_forming_candle_never_fabricates_inr_when_conversion_unavailable(client, monkeypatch):
    ac, session_maker = client
    _make_live(price=79700.0)

    async def failing_rate(client=None, now=None):
        return None

    monkeypatch.setattr("apps.api.routers.market.get_usdt_inr_rate", failing_rate)
    resp = await ac.get("/api/v1/market/candles", params={"symbol": "BTC/USDT", "timeframe": "4h"})
    body = resp.json()
    assert body["forming_candle"]["close"] == 79700.0  # raw value still shown
    assert body["forming_candle"]["close_inr"] is None  # never a guessed INR number
    assert body["conversion_status"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_forming_candle_is_null_only_for_btc_usdt(client):
    ac, session_maker = client
    _make_live(price=79700.0)
    resp = await ac.get("/api/v1/market/candles", params={"symbol": "ETH/USDT", "timeframe": "4h"})
    body = resp.json()
    assert body["forming_candle"] is None


@pytest.mark.asyncio
async def test_existing_historical_candles_unaffected_by_forming_candle_addition(client):
    """The pre-existing 'candles' array must be exactly what it was before
    this change -- forming_candle is purely additive."""
    ac, session_maker = client
    await _seed_candle(session_maker, datetime(2026, 1, 1, 0, 0, 0), close=79600.0)
    await _seed_candle(session_maker, datetime(2026, 1, 1, 4, 0, 0), close=79650.0)
    _make_live(price=79700.0)
    resp = await ac.get("/api/v1/market/candles", params={"symbol": "BTC/USDT", "timeframe": "4h"})
    body = resp.json()
    assert len(body["candles"]) == 2
    assert body["candles"][-1]["close"] == 79650.0  # last historical bar, untouched by the forming one
    assert body["forming_candle"]["close"] == 79700.0  # separate field entirely
