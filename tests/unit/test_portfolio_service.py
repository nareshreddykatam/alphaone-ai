"""Phase 4, sections 6/20/26/33/34: portfolio accounting must keep Backtest /
AlphaOne-signal / User-actual performance strictly separate, exclude
deposits & withdrawals from the trading equity curve, and never silently
fix a reconciliation mismatch.
"""
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import (
    Account, Trade, TradeStatus, Signal, SignalOutcome, SignalOutcomeType,
    BacktestRun, BacktestMetric, Deposit, Withdrawal, AccountSnapshot,
)
from services.portfolio.service import (
    get_user_actual_performance,
    get_alphaone_signal_performance,
    get_missed_signals,
    get_backtest_performance,
    get_equity_curve,
    get_pnl_breakdown,
    reconcile_account,
)


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _make_account(session):
    account = Account()
    session.add(account)
    await session.flush()
    return account


async def _make_closed_trade(session, account_id, pnl, fees=0.0, funding=0.0, exit_time=None):
    trade = Trade(
        trade_id=f"T-{pnl}-{exit_time}", side="LONG", entry_price=100.0, quantity=1.0,
        entry_time=datetime(2026, 1, 1), exit_time=exit_time or datetime(2026, 1, 2),
        status=TradeStatus.CLOSED.value, pnl=pnl, fees=fees, funding=funding,
        r_multiple=pnl / 10 if pnl else 0, account_id=account_id,
    )
    session.add(trade)
    return trade


@pytest.mark.asyncio
async def test_user_actual_performance_only_reads_trades_table(session_maker):
    async with session_maker() as session:
        account = await _make_account(session)
        await _make_closed_trade(session, account.id, pnl=100.0, fees=2.0)
        await _make_closed_trade(session, account.id, pnl=-40.0, fees=2.0)
        await session.commit()

        stats = await get_user_actual_performance(session, account_id=account.id)
        assert stats["total_trades"] == 2
        assert stats["total_pnl"] == pytest.approx(60.0)
        assert stats["winning_trades"] == 1
        assert stats["losing_trades"] == 1
        assert stats["win_rate"] == pytest.approx(0.5)
        assert stats["profit_factor"] == pytest.approx(100.0 / 40.0)


@pytest.mark.asyncio
async def test_alphaone_signal_performance_ignores_trades_entirely(session_maker):
    async with session_maker() as session:
        account = await _make_account(session)
        # A real trade exists but must have zero influence on signal performance.
        await _make_closed_trade(session, account.id, pnl=99999.0)

        for i, (outcome, pct) in enumerate([
            (SignalOutcomeType.WIN.value, 3.0),
            (SignalOutcomeType.LOSS.value, -1.0),
            (SignalOutcomeType.NO_TRADE.value, None),
        ]):
            session.add(Signal(signal_id=f"S{i}", timestamp=datetime(2026, 1, 1), signal_type="LONG", confidence=0.5))
            session.add(SignalOutcome(signal_id=f"S{i}", outcome=outcome, hypothetical_pnl_pct=pct))
        await session.commit()

        stats = await get_alphaone_signal_performance(session)
        assert stats["total_signals"] == 3
        assert stats["resolved_signals"] == 2
        assert stats["total_hypothetical_pnl_pct"] == pytest.approx(2.0)
        assert stats["win_rate"] == pytest.approx(0.5)
        assert stats["no_trade_rate"] == pytest.approx(1 / 3)
        # confirm the huge real trade PnL never leaked into this figure
        assert stats["total_hypothetical_pnl_pct"] != pytest.approx(99999.0)


@pytest.mark.asyncio
async def test_missed_signals_split_all_vs_taken_vs_missed(session_maker):
    async with session_maker() as session:
        session.add(Signal(signal_id="S1", timestamp=datetime(2026, 1, 1), signal_type="LONG", confidence=0.5))
        session.add(SignalOutcome(signal_id="S1", outcome=SignalOutcomeType.WIN.value, hypothetical_pnl_pct=5.0, was_taken_by_user=True))
        session.add(Signal(signal_id="S2", timestamp=datetime(2026, 1, 1), signal_type="LONG", confidence=0.5))
        session.add(SignalOutcome(signal_id="S2", outcome=SignalOutcomeType.WIN.value, hypothetical_pnl_pct=5.0, was_taken_by_user=False))
        session.add(Signal(signal_id="S3", timestamp=datetime(2026, 1, 1), signal_type="SHORT", confidence=0.5))
        session.add(SignalOutcome(signal_id="S3", outcome=SignalOutcomeType.LOSS.value, hypothetical_pnl_pct=-2.0, was_taken_by_user=False))
        await session.commit()

        stats = await get_missed_signals(session)
        assert stats["all_signals"]["count"] == 3
        assert stats["user_taken"]["count"] == 1
        assert stats["missed"]["count"] == 2
        assert stats["missed"]["win_rate"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_backtest_performance_is_independent_of_live_trades(session_maker):
    async with session_maker() as session:
        account = await _make_account(session)
        await _make_closed_trade(session, account.id, pnl=-500.0)  # user losing money live

        run = BacktestRun(
            strategy_name="trend_following", timeframe="4h",
            config_json={}, dataset_start=datetime(2023, 1, 1), dataset_end=datetime(2026, 1, 1),
        )
        session.add(run)
        await session.flush()
        session.add(BacktestMetric(run_id=run.id, total_pnl_pct=12.5, win_rate=0.55, profit_factor=1.3, sharpe_ratio=0.8, max_drawdown_pct=8.0, total_trades=40))
        await session.commit()

        result = await get_backtest_performance(session)
        assert result is not None
        assert result["total_pnl_pct"] == pytest.approx(12.5)
        # the live loss must not have altered the backtest figure
        assert result["strategy_name"] == "trend_following"


@pytest.mark.asyncio
async def test_backtest_performance_returns_none_when_no_runs_exist(session_maker):
    async with session_maker() as session:
        assert await get_backtest_performance(session) is None


@pytest.mark.asyncio
async def test_equity_curve_excludes_deposits_and_withdrawals(session_maker):
    async with session_maker() as session:
        account = await _make_account(session)
        await _make_closed_trade(session, account.id, pnl=100.0, exit_time=datetime(2026, 1, 2))
        await _make_closed_trade(session, account.id, pnl=-30.0, exit_time=datetime(2026, 1, 3))
        session.add(Deposit(account_id=account.id, amount=5000.0, timestamp=datetime(2026, 1, 2, 12)))
        session.add(Withdrawal(account_id=account.id, amount=1000.0, timestamp=datetime(2026, 1, 2, 13)))
        await session.commit()

        curve = await get_equity_curve(session, account.id, initial_equity=10000.0)
        assert len(curve) == 2
        assert curve[0]["equity"] == pytest.approx(10100.0)
        assert curve[1]["equity"] == pytest.approx(10070.0)  # deposit/withdrawal never applied here


@pytest.mark.asyncio
async def test_pnl_breakdown_gross_equals_net_plus_fees_plus_funding(session_maker):
    async with session_maker() as session:
        account = await _make_account(session)
        await _make_closed_trade(session, account.id, pnl=90.0, fees=8.0, funding=2.0, exit_time=datetime(2026, 1, 5))
        await session.commit()

        breakdown = await get_pnl_breakdown(session, account.id, period="daily")
        assert len(breakdown) == 1
        row = breakdown[0]
        assert row["net"] == pytest.approx(90.0)
        assert row["gross"] == pytest.approx(90.0 + 8.0 + 2.0)


@pytest.mark.asyncio
async def test_reconciliation_flags_mismatch_without_correcting_anything(session_maker):
    async with session_maker() as session:
        account = await _make_account(session)
        await _make_closed_trade(session, account.id, pnl=100.0)
        session.add(AccountSnapshot(account_id=account.id, timestamp=datetime(2026, 1, 3), equity=10500.0))
        await session.commit()

        result = await reconcile_account(session, account.id, initial_equity=10000.0)
        # theoretical = 10000 + 100 = 10100, reported = 10500 -> mismatch of 400
        assert result["status"] == "MISMATCH"
        assert result["is_mismatched"] is True
        assert result["difference"] == pytest.approx(400.0)
        assert result["theoretical_equity"] == pytest.approx(10100.0)
        assert result["reported_equity"] == pytest.approx(10500.0)  # unchanged, not "corrected"


@pytest.mark.asyncio
async def test_reconciliation_with_no_snapshot_reports_that_plainly(session_maker):
    async with session_maker() as session:
        account = await _make_account(session)
        result = await reconcile_account(session, account.id, initial_equity=10000.0)
        assert result["status"] == "NO_SNAPSHOT"
        assert result["reported_equity"] is None
