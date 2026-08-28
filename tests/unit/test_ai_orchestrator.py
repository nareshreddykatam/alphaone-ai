"""AI Trading V1: the AI signal orchestrator must never fabricate a
probability or confidence number. With no model deployed (the current,
honest default -- see reports/AI_TRADING_RESEARCH_V1.txt), every
probability/expected_return field must be None, not zero or guessed, and
`confidence` must fall back to the strategy's own real quality tier."""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import BotState, Prediction, Signal
from services.signal_engine import ai_orchestrator


@pytest.fixture(autouse=True)
def _reset_model_cache():
    """The orchestrator caches the deployed-model lookup once per process
    (see ai_orchestrator._cache) -- reset it before every test so one
    test's result can never leak into another's."""
    ai_orchestrator._cache.update(loaded=False, model=None, feature_cols=None, metadata=None, barrier_config=None)
    yield
    ai_orchestrator._cache.update(loaded=False, model=None, feature_cols=None, metadata=None, barrier_config=None)


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _make_df(n=120, seed=0):
    rng = np.random.default_rng(seed)
    t0 = datetime(2026, 1, 1)
    close = 40000 + np.cumsum(rng.standard_normal(n) * 50)
    high = close + np.abs(rng.standard_normal(n) * 60)
    low = close - np.abs(rng.standard_normal(n) * 60)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    return pd.DataFrame({
        "timestamp": [t0 + timedelta(hours=4 * i) for i in range(n)],
        "open": open_, "high": high, "low": low, "close": close,
        "volume": rng.uniform(100, 1000, n),
    })


def _make_signal(signal_type="LONG"):
    return Signal(
        signal_id="SIG-TEST-001", timestamp=datetime(2026, 1, 20), symbol="BTC/USDT", timeframe="4h",
        signal_type=signal_type, confidence=0.0, entry_price=41000.0, stop_loss=40000.0,
        take_profit_1=43000.0, take_profit_2=44000.0, take_profit_3=45000.0,
        quality="MEDIUM", strategy_name="S06_SUPERTREND_ATR_4H",
    )


async def test_no_model_deployed_never_fabricates_probabilities(session_maker):
    async with session_maker() as session:
        df = _make_df()
        signal = _make_signal()
        decision = await ai_orchestrator.enrich_signal_with_ai_evidence(session, df, signal)

        assert decision.model_status == "NO_MODEL_DEPLOYED"
        assert decision.probability_long is None
        assert decision.probability_short is None
        assert decision.probability_no_trade is None
        assert decision.expected_return is None
        assert decision.model_version is None
        # Confidence falls back to the strategy's own real quality tier,
        # never an invented number standing in for a probability.
        assert decision.confidence == "MEDIUM"


async def test_decision_carries_real_computable_evidence_regardless_of_model(session_maker):
    async with session_maker() as session:
        df = _make_df()
        signal = _make_signal()
        decision = await ai_orchestrator.enrich_signal_with_ai_evidence(session, df, signal)

        assert decision.symbol == "BTC/USDT"
        assert decision.direction == "LONG"
        assert decision.strategy_sources == ["S06_SUPERTREND_ATR_4H"]
        assert decision.regime in {
            "TRENDING_BULLISH", "TRENDING_BEARISH", "RANGING", "HIGH_VOLATILITY",
            "LOW_VOLATILITY", "BREAKOUT", "POST_LIQUIDATION", "UNCERTAIN",
        }
        assert decision.expected_volatility is not None and decision.expected_volatility > 0
        assert decision.entry == pytest.approx(41000.0)
        assert decision.stop_loss == pytest.approx(40000.0)
        assert decision.take_profit_1 == pytest.approx(43000.0)
        assert decision.take_profit_2 == pytest.approx(44000.0)
        assert decision.take_profit_3 == pytest.approx(45000.0)
        assert decision.risk_reward == pytest.approx(2.0)  # (43000-41000) / (41000-40000)


async def test_model_backed_decision_uses_real_calibrated_probabilities(session_maker, monkeypatch):
    """When a model IS deployed, probabilities must come straight from the
    model's own predict_proba output -- never recomputed or guessed."""

    class _FakeModel:
        def predict_proba(self, X):
            return np.array([[0.1, 0.2, 0.7]])  # [short, no_trade, long]

    async with session_maker() as session:
        session.add(BotState(key=ai_orchestrator.DEPLOYED_MODEL_KEY, value={
            "model_name": "fake", "model_version": "v_test",
            "feature_names": ["ema_9"], "barrier_params": {}, "feature_version": "test",
        }))
        await session.commit()

        async def _fake_load(session_arg):
            from ml.labeling import TripleBarrierConfig
            return _FakeModel(), ["ema_9"], {"model_version": "v_test", "feature_version": "test"}, TripleBarrierConfig()

        monkeypatch.setattr(ai_orchestrator, "_load_deployed_model", _fake_load)

        df = _make_df()
        signal = _make_signal()
        decision = await ai_orchestrator.enrich_signal_with_ai_evidence(session, df, signal)

        assert decision.model_status == "MODEL_BACKED"
        assert decision.probability_long == pytest.approx(0.7)
        assert decision.probability_short == pytest.approx(0.1)
        assert decision.probability_no_trade == pytest.approx(0.2)
        assert decision.model_version == "v_test"
        assert decision.confidence == "70% calibrated probability"

        # A real Prediction row must be persisted for model-monitor drift tracking.
        pred = (await session.execute(select(Prediction))).scalars().first()
        assert pred is not None
        assert pred.signal_type == "LONG"
        assert pred.long_probability == pytest.approx(0.7)
