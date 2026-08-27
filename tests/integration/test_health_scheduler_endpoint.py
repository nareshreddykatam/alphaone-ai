"""Integration coverage for GET /api/v1/health/scheduler -- added after a
real production incident where every DB-writing scheduler job went silent
for 12+ minutes with no way to tell, from the outside, whether the job
LOOP had stopped iterating versus every attempt merely failing before
writing anything. See services/scheduler/runner.py's module docstring.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_scheduler_health_endpoint_returns_expected_shape(client):
    resp = await client.get("/api/v1/health/scheduler")
    assert resp.status_code == 200
    body = resp.json()
    assert "scheduler_running" in body
    assert set(body["jobs"].keys()) == {
        "account_sync", "exit_alerts", "signal_generation",
        "outcome_evaluation", "candle_ingestion", "live_breakout",
    }
    for job in body["jobs"].values():
        assert set(job.keys()) == {"last_tick_at", "seconds_since_last_tick", "circuit_state", "consecutive_failures"}


@pytest.mark.asyncio
async def test_scheduler_health_endpoint_never_exposes_credentials(client, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "coindcx_api_key", "super-secret-key-value")
    monkeypatch.setattr(settings, "coindcx_api_secret", "super-secret-secret-value")

    resp = await client.get("/api/v1/health/scheduler")
    text = resp.text
    assert "super-secret-key-value" not in text
    assert "super-secret-secret-value" not in text
