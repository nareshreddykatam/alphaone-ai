"""Integration coverage for the live CoinDCX market-data WebSocket's
effect on the rest of AlphaOne: the dashboard's price/freshness fields,
the USDT->INR conversion-failure path, and the architectural boundary
that live ticks never touch signal generation or position/exit
monitoring (those stay exactly as Phase 4/5 built them)."""
import asyncio
from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app
from apps.api.config import get_settings
from database.schema.models import ConnectionState
from services.market_data.live_state import market_ws, start_market_data_ws, stop_market_data_ws, _startup_retry


@pytest.fixture(autouse=True)
def _reset_market_ws_state():
    """Every dashboard/integration test in the whole suite shares the
    process-wide `market_ws` singleton -- reset it before and after each
    test here so this file's WS-state manipulation can never leak into
    unrelated tests (e.g. the Phase 4/5 dashboard tests asserting the
    Binance-candle fallback path)."""
    market_ws.state.last_price_usdt = None
    market_ws.state.mark_price_usdt = None
    market_ws.state.received_at = None
    market_ws._connected = False
    market_ws._ever_connected = False
    yield
    market_ws.state.last_price_usdt = None
    market_ws.state.mark_price_usdt = None
    market_ws.state.received_at = None
    market_ws._connected = False
    market_ws._ever_connected = False


@pytest.fixture
async def client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "coindcx_api_key", "")
    monkeypatch.setattr(settings, "coindcx_api_secret", "")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_dashboard_uses_live_ws_price_when_available(client, monkeypatch):
    market_ws._connected = True
    market_ws._ever_connected = True
    market_ws.state.last_price_usdt = 78000.0
    market_ws.state.mark_price_usdt = 78010.0
    market_ws.state.received_at = datetime.utcnow()

    from services.exchange import fx
    fx._reset_cache_for_tests()

    async def fake_rate(client=None, now=None):
        return fx.ConversionRate(rate=90.0, rate_timestamp=1.0, fetched_at=1e18)

    monkeypatch.setattr("apps.api.routers.dashboard.get_usdt_inr_rate", fake_rate)

    resp = await client.get("/api/v1/dashboard/")
    body = resp.json()
    assert body["btc_price_usdt"] == 78000.0
    assert body["btc_price_inr"] == 78000.0 * 90.0
    assert body["market_data_source"] == "CoinDCX WebSocket"
    assert body["market_data_status"] == "LIVE"
    assert body["market_data_mark_price_usdt"] == 78010.0


@pytest.mark.asyncio
async def test_dashboard_falls_back_to_candle_when_ws_never_delivered_a_price(client):
    """market_ws never having a price (WS disabled or never connected --
    the default state in this test suite) must not change existing
    Phase 4/5 dashboard behavior at all."""
    resp = await client.get("/api/v1/dashboard/")
    body = resp.json()
    assert body["market_data_source"] == "Binance (historical candle ingestion)"
    assert body["market_data_status"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_dashboard_shows_disconnected_status_after_a_ws_drop(client):
    market_ws._ever_connected = True
    market_ws._connected = False
    resp = await client.get("/api/v1/dashboard/")
    assert resp.json()["market_data_status"] == "DISCONNECTED"


# ---- 18. INR conversion failure ----

@pytest.mark.asyncio
async def test_dashboard_reports_conversion_unavailable_never_fabricates_inr(client, monkeypatch):
    market_ws._connected = True
    market_ws._ever_connected = True
    market_ws.state.last_price_usdt = 78000.0
    market_ws.state.received_at = datetime.utcnow()

    async def failing_rate(client=None, now=None):
        return None

    monkeypatch.setattr("apps.api.routers.dashboard.get_usdt_inr_rate", failing_rate)

    resp = await client.get("/api/v1/dashboard/")
    body = resp.json()
    assert body["btc_price_usdt"] == 78000.0
    assert body["btc_price_inr"] is None  # never a fabricated INR number
    assert body["conversion_status"] == "UNAVAILABLE"


# ---- 19. Signal deduplication / architectural boundary ----

@pytest.mark.asyncio
async def test_feeding_many_ticks_never_generates_a_signal_or_sends_telegram(monkeypatch):
    """The live tick stream must never itself trigger signal generation --
    only the existing candle-completion-driven scheduler job does that
    (unchanged by this phase). Proven by feeding the WS client many ticks
    directly and asserting neither generate_and_persist_signal nor
    notify_new_signal is ever called as a side effect."""
    called = {"generate": 0, "notify": 0}

    async def fake_generate(*args, **kwargs):
        called["generate"] += 1
        return None

    async def fake_notify(*args, **kwargs):
        called["notify"] += 1
        return False

    monkeypatch.setattr("services.signal_engine.live_signal.generate_and_persist_signal", fake_generate)
    monkeypatch.setattr("services.signal_engine.notify.notify_new_signal", fake_notify)

    for i in range(50):
        market_ws.handle_price_change({"T": 1735732800000 + i, "p": str(78000 + i)})

    assert called == {"generate": 0, "notify": 0}


# ---- 20/21. Position P&L / exit monitoring boundary ----

def test_market_ws_module_is_not_imported_by_position_or_exit_monitoring():
    """The real CoinDCX position's own mark price (INR-native, 30s REST
    poll, Phase 5) is a separate, already-correct data path -- this
    phase's public B-BTC_USDT WebSocket must never be wired into position
    PnL or exit-alert math, which would silently apply the wrong
    instrument's price to a real INR-margined position."""
    import inspect
    import services.position_monitor.monitor as monitor_module
    import services.scheduler.jobs as jobs_module

    for module in (monitor_module, jobs_module):
        source = inspect.getsource(module)
        assert "coindcx_ws" not in source, f"{module.__name__} must not import the live market-data WebSocket"


# ---- Startup retry (production hardening) integration coverage ----
# Unit-level backoff/idempotency/shutdown mechanics are covered by
# tests/unit/test_market_data_startup_retry.py against a bare
# _StartupRetrySupervisor. These tests drive the REAL process-wide
# market_ws singleton (and the module-level _startup_retry it's wired
# to) through start_market_data_ws()/stop_market_data_ws() -- the exact
# functions apps/api/main.py's lifespan calls -- to prove the production
# wiring, not just the standalone class, behaves correctly end-to-end.

@pytest.fixture(autouse=True)
async def _reset_startup_retry_and_notified_status():
    async def _reset():
        market_ws._last_notified_status = None
        if _startup_retry._task is not None:
            _startup_retry._task.cancel()
            try:
                await _startup_retry._task
            except (asyncio.CancelledError, Exception):
                pass
        _startup_retry._task = None
        _startup_retry._stop_event = None

    await _reset()
    yield
    await _reset()
    market_ws._connected = False
    market_ws._ever_connected = False


@pytest.mark.asyncio
async def test_existing_reconnect_still_works_after_a_supervisor_driven_startup(monkeypatch):
    """After start_market_data_ws() (the supervisor path) succeeds, a
    SUBSEQUENT real drop+reconnect must still be handled entirely by
    market_ws's own existing, unmodified reconnect logic -- proving the
    supervisor doesn't leave the singleton in some different state than
    a direct connect() would have."""
    join_calls = []

    async def fake_emit(event, data=None):
        if event == "join":
            join_calls.append(data)

    connect_attempts = {"count": 0}

    async def fake_sio_connect(*args, **kwargs):
        connect_attempts["count"] += 1
        await market_ws._on_connect()  # simulate what a real successful socketio connect triggers

    monkeypatch.setattr(market_ws._sio, "connect", fake_sio_connect)
    monkeypatch.setattr(market_ws._sio, "emit", fake_emit)

    await start_market_data_ws()
    await _startup_retry._task  # wait for the (immediately successful) startup task to finish

    assert market_ws._connected is True
    assert connect_attempts["count"] == 1
    joins_after_startup = len(join_calls)

    # Now simulate a real drop and recovery -- entirely via market_ws's
    # own existing on_disconnect/on_connect handlers, never touching the
    # supervisor again (it already finished its one job).
    await market_ws._on_disconnect()
    assert market_ws.connection_status() == ConnectionState.DISCONNECTED
    await market_ws._on_connect()
    assert market_ws.connection_status() in (ConnectionState.LIVE, ConnectionState.UNAVAILABLE)
    assert market_ws._connected is True
    assert len(join_calls) > joins_after_startup  # resubscribed again, unmodified existing behavior

    await _cancel_ping(market_ws)


@pytest.mark.asyncio
async def test_telegram_alerts_not_duplicated_by_startup_retries(monkeypatch):
    """Failed startup retries must never reach _on_connect/_on_disconnect
    at all (see services/market_data/coindcx_ws.py), so they can never
    fire a Telegram alert -- and the eventual first successful connect is
    a baseline, not a "recovery", so it must not alert either."""
    alert_calls = []

    async def fake_send_alert(self, status):
        alert_calls.append(status)

    monkeypatch.setattr("services.telegram.bot.TelegramBot.send_market_data_alert", fake_send_alert)

    attempts = {"count": 0}

    async def flaky_sio_connect(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise ConnectionError("Connection error")
        await market_ws._on_connect()

    async def fake_emit(event, data=None):
        return None

    monkeypatch.setattr(market_ws._sio, "connect", flaky_sio_connect)
    monkeypatch.setattr(market_ws._sio, "emit", fake_emit)
    monkeypatch.setattr(_startup_retry, "_wait_fn", _instant_wait)

    await start_market_data_ws()
    await _startup_retry._task

    assert attempts["count"] == 3  # failed twice, succeeded on the third
    assert alert_calls == []  # zero alerts -- neither for the 2 failures nor the eventual first connect

    await _cancel_ping(market_ws)


async def _instant_wait(stop_event, timeout):
    return None


async def _cancel_ping(client):
    if client._ping_task is not None:
        client._ping_task.cancel()
        try:
            await client._ping_task
        except asyncio.CancelledError:
            pass
        client._ping_task = None
