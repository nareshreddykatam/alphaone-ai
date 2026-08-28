"""Multi-Coin AI Futures System, Phase 20: independent paper positions
across multiple coins -- BTC LONG + ETH SHORT + SOL LONG must all be able
to exist simultaneously, and one coin's signal must never block or
overwrite another's."""
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import ExternalSignal, ExternalTelegramMessage, Trade, TradeSource
from services.telegram_signals.paper_execution import execute_valid_signal
from services.telegram_signals.live_state import multi_coin_paper_trader


@pytest.fixture(autouse=True)
def _reset_paper_trader():
    multi_coin_paper_trader.positions.clear()
    multi_coin_paper_trader.closed_trades.clear()
    multi_coin_paper_trader.risk_engine.state.positions_open = 0
    yield
    multi_coin_paper_trader.positions.clear()
    multi_coin_paper_trader.closed_trades.clear()
    multi_coin_paper_trader.risk_engine.state.positions_open = 0


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


_msg_counter = 0


async def _valid_signal(session, symbol, direction, entry, sl, tp1):
    global _msg_counter
    _msg_counter += 1
    message = ExternalTelegramMessage(
        source_channel="@suncrypto_trading_alerts", telegram_message_id=str(_msg_counter),
        message_timestamp=datetime(2026, 1, 1), text=f"{symbol} {direction}", raw_hash=f"hash-{_msg_counter}",
    )
    session.add(message)
    await session.flush()
    signal = ExternalSignal(
        message_id=message.id, source_channel="@suncrypto_trading_alerts", status="VALID",
        symbol=symbol, direction=direction, entry_price=entry, stop_loss=sl, take_profit_1=tp1,
        created_at=datetime(2026, 1, 1),
    )
    session.add(signal)
    await session.commit()
    return signal


async def test_only_valid_signals_are_executed(session_maker):
    async with session_maker() as session:
        non_valid = await _valid_signal(session, "BTC/USDT", "LONG", 80000.0, 79000.0, 83000.0)
        non_valid.status = "INCOMPLETE"
        await session.commit()
        position, reason = await execute_valid_signal(session, non_valid, usdt_inr_rate=88.0)
        assert position is None
        assert "not VALID" in reason


async def test_execute_opens_a_real_paper_position_with_fixed_margin_sizing(session_maker):
    async with session_maker() as session:
        signal = await _valid_signal(session, "BTC/USDT", "LONG", 80000.0, 79000.0, 83000.0)

        position, reason = await execute_valid_signal(session, signal, usdt_inr_rate=80.0)
        assert reason == "OK"
        assert position is not None
        assert position.leverage == 10
        assert position.quantity == pytest.approx((200 / 80 * 10) / 80000.0)

        trade = (await session.execute(select(Trade).where(Trade.trade_id == position.trade_id))).scalar_one()
        assert trade.source == TradeSource.TELEGRAM_EXTERNAL.value
        assert trade.symbol == "BTC/USDT"
        assert signal.trade_id == position.trade_id


async def test_multiple_coins_hold_independent_concurrent_positions(session_maker):
    """BTC LONG + ETH SHORT + SOL LONG simultaneously -- none blocks another."""
    async with session_maker() as session:
        btc = await _valid_signal(session, "BTC/USDT", "LONG", 80000.0, 79000.0, 83000.0)
        eth = await _valid_signal(session, "ETH/USDT", "SHORT", 2500.0, 2600.0, 2300.0)
        sol = await _valid_signal(session, "SOL/USDT", "LONG", 100.0, 95.0, 110.0)

        pos_btc, r1 = await execute_valid_signal(session, btc, usdt_inr_rate=88.0)
        pos_eth, r2 = await execute_valid_signal(session, eth, usdt_inr_rate=88.0)
        pos_sol, r3 = await execute_valid_signal(session, sol, usdt_inr_rate=88.0)

        assert r1 == r2 == r3 == "OK"
        assert pos_btc is not None and pos_eth is not None and pos_sol is not None
        assert pos_btc.trade_id != pos_eth.trade_id != pos_sol.trade_id

        trades = (await session.execute(select(Trade))).scalars().all()
        symbols = {t.symbol for t in trades}
        assert symbols == {"BTC/USDT", "ETH/USDT", "SOL/USDT"}
        # Each trade references its OWN entry/price -- never another coin's.
        by_symbol = {t.symbol: t for t in trades}
        assert by_symbol["BTC/USDT"].entry_price == pytest.approx(80000.0)
        assert by_symbol["ETH/USDT"].entry_price == pytest.approx(2500.0)
        assert by_symbol["SOL/USDT"].entry_price == pytest.approx(100.0)


async def test_second_signal_on_same_open_symbol_and_source_is_blocked(session_maker):
    async with session_maker() as session:
        first = await _valid_signal(session, "BTC/USDT", "LONG", 80000.0, 79000.0, 83000.0)
        second = await _valid_signal(session, "BTC/USDT", "LONG", 80100.0, 79100.0, 83100.0)

        pos1, r1 = await execute_valid_signal(session, first, usdt_inr_rate=88.0)
        pos2, r2 = await execute_valid_signal(session, second, usdt_inr_rate=88.0)

        assert pos1 is not None
        assert pos2 is None
        assert "already exists" in r2


async def test_no_live_inr_rate_blocks_execution(session_maker):
    async with session_maker() as session:
        signal = await _valid_signal(session, "BTC/USDT", "LONG", 80000.0, 79000.0, 83000.0)
        position, reason = await execute_valid_signal(session, signal, usdt_inr_rate=None)
        assert position is None
