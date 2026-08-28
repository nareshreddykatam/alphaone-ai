"""Live Futures Auto-Trading V1, Phase 27-28: GET /api/v1/live-execution/status
-- read-only observability for the safety architecture. Overrides get_db
with an isolated in-memory SQLite DB (same pattern as
tests/integration/test_api_phase4_flow.py) so these tests never touch the
real research database. Proves: correct DISABLED/ARMED/ACTIVE derivation,
correct daily-entries/margin/leverage/position-limit reporting, no write
methods, and no credential exposure.
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from apps.api.config import get_settings
from apps.api.main import app
from database.schema import Base, get_db


@pytest.fixture
async def client(monkeypatch):
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
        yield ac
    app.dependency_overrides.clear()
    await engine.dispose()


async def _get(client):
    return await client.get("/api/v1/live-execution/status")


async def test_disabled_by_default(client):
    settings = get_settings()
    assert settings.automatic_trading_enabled is False  # the real production default, not mocked

    resp = await _get(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["automatic_trading"] == "DISABLED"
    assert body["automatic_trading_enabled"] is False
    assert body["live_execution_armed"] is False


async def test_armed_state_when_enabled_and_armed_but_contract_unverified(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    resp = await _get(client)
    body = resp.json()
    assert body["automatic_trading"] == "ARMED"  # never ACTIVE -- ORDER_CONTRACT_VERIFIED can never pass
    assert body["order_contract_verified"] is False


async def test_enabled_alone_without_armed_is_still_disabled(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    # live_execution_armed left at its default False

    resp = await _get(client)
    body = resp.json()
    assert body["automatic_trading"] == "DISABLED"


async def test_armed_alone_without_enabled_is_still_disabled(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "live_execution_armed", True)
    # automatic_trading_enabled left at its default False

    resp = await _get(client)
    body = resp.json()
    assert body["automatic_trading"] == "DISABLED"


async def test_emergency_stop_clear_by_default(client):
    resp = await _get(client)
    body = resp.json()
    assert body["emergency_stop"] == "CLEAR"
    assert body["emergency_stop_reason"] is None


async def test_emergency_stop_active_reports_the_real_reason(client):
    from services.live_execution.kill_switch import activate_emergency_stop
    override = app.dependency_overrides[get_db]
    async for session in override():
        await activate_emergency_stop(session, reason="manual halt for a status-endpoint test")
        break

    resp = await _get(client)
    body = resp.json()
    assert body["emergency_stop"] == "ACTIVE"
    assert body["emergency_stop_reason"] == "manual halt for a status-endpoint test"


async def test_margin_and_leverage_are_the_exact_fixed_constants(client):
    resp = await _get(client)
    body = resp.json()
    assert body["margin_inr"] == 200.0
    assert body["leverage"] == 10


async def test_daily_entries_reports_target_and_max(client):
    resp = await _get(client)
    body = resp.json()
    assert body["daily_entries"]["count"] == 0
    assert body["daily_entries"]["target"] == 10
    assert body["daily_entries"]["max"] == 15


async def test_max_open_positions_live_default_is_one(client):
    resp = await _get(client)
    body = resp.json()
    assert body["max_open_positions_live"] == 1
    assert body["open_positions_live"] == 0


async def test_endpoint_only_supports_get(client):
    for method in ("post", "put", "patch", "delete"):
        resp = await client.request(method, "/api/v1/live-execution/status")
        assert resp.status_code == 405, f"{method.upper()} must not be supported"


async def test_response_never_contains_any_credential_value(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "coindcx_api_key", "fake-coindcx-key")
    monkeypatch.setattr(settings, "coindcx_api_secret", "fake-coindcx-secret")
    monkeypatch.setattr(settings, "telegram_api_hash", "deadbeefcafefeed0011223344556677")
    monkeypatch.setattr(settings, "telegram_session", "1BVtsOK4Bu1secretsessionstringvaluehere==")

    resp = await _get(client)
    raw_text = resp.text
    for forbidden in (
        "fake-coindcx-key", "fake-coindcx-secret", "deadbeefcafefeed0011223344556677",
        "1BVtsOK4Bu1secretsessionstringvaluehere==",
    ):
        assert forbidden not in raw_text
    body = resp.json()
    for forbidden_key in ("coindcx_api_key", "coindcx_api_secret", "telegram_api_hash", "telegram_session", "api_key", "api_secret"):
        assert forbidden_key not in body


def test_status_router_module_has_no_order_mutating_or_write_capability():
    import inspect
    import apps.api.routers.live_execution_status as status_module
    source = inspect.getsource(status_module)
    for forbidden in ("create_order", "place_order", "submit_order", "@router.post", "@router.put", "@router.patch", "@router.delete"):
        assert forbidden not in source
    assert "services.exchange.coindcx" not in source
    assert "submit_futures_order" not in source
