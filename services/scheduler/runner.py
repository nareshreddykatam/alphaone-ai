"""Background scheduler runner (Phase 5, section 30). Must run on a
persistent process -- NEVER inside a serverless function (see
docs/deployment.md). Each job runs in its own asyncio loop, guarded by its
own CircuitBreaker so a failing CoinDCX API doesn't get hammered and one
job's failure can't take down the others.

Each `_run_once_*` method is a single tick, fully testable in isolation
without running the infinite loop -- the loops themselves
(`_loop_account_sync` etc.) are thin `while True: tick(); sleep()` wrappers
around them, started only from `start()`.
"""
import asyncio
from datetime import datetime

import structlog

from database.schema import async_session
from services.exchange.coindcx import CoinDCXReadOnlyAccountProvider
from services.market_data.binance import BinanceExchange
from services.market_data.live_state import market_ws
from services.scheduler.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from services.scheduler.jobs import (
    account_sync_job, exit_alert_job, signal_generation_job, outcome_evaluation_job, candle_ingestion_job,
    live_breakout_job,
)
from services.signal_engine.live_breakout import LiveCandleAggregator

logger = structlog.get_logger()

ACCOUNT_SYNC_INTERVAL_SECONDS = 30
EXIT_ALERT_INTERVAL_SECONDS = 30
SIGNAL_GENERATION_INTERVAL_SECONDS = 900  # 15 min -- primary strategy is 4h, no value in checking more often
OUTCOME_EVALUATION_INTERVAL_SECONDS = 900
CANDLE_INGESTION_INTERVAL_SECONDS = 900  # 15 min -- see services/scheduler/jobs.py's module docstring for the reasoning
LIVE_BREAKOUT_INTERVAL_SECONDS = 30  # same cadence as account_sync/exit_alert -- see jobs.py's module docstring


class SchedulerRunner:
    def __init__(self, api_key: str = "", api_secret: str = ""):
        self._api_key = api_key
        self._api_secret = api_secret
        self._tasks: list[asyncio.Task] = []
        self._running = False

        self.account_sync_breaker = CircuitBreaker(config=CircuitBreakerConfig())
        self.exit_alert_breaker = CircuitBreaker(config=CircuitBreakerConfig())
        self.signal_breaker = CircuitBreaker(config=CircuitBreakerConfig())
        self.outcome_breaker = CircuitBreaker(config=CircuitBreakerConfig())
        self.candle_ingestion_breaker = CircuitBreaker(config=CircuitBreakerConfig())
        self.live_breakout_breaker = CircuitBreaker(config=CircuitBreakerConfig())
        # Persistent across ticks (unlike the per-tick-fresh CoinDCX/Binance
        # clients above) -- it must remember the forming candle's running
        # high/low between calls. Reuses the process-wide live market-data
        # singleton (services/market_data/live_state.py) rather than
        # opening a second connection.
        self._live_candle_aggregator = LiveCandleAggregator(timeframe="4h")

    def _make_provider(self) -> CoinDCXReadOnlyAccountProvider:
        return CoinDCXReadOnlyAccountProvider(self._api_key, self._api_secret)

    def _make_binance_exchange(self) -> BinanceExchange:
        # Public OHLCV data only -- no credentials, mirrors the exact
        # construction scripts/_common.py: new_exchange() already uses for
        # the manual backfill script. Never used for CoinDCX account/order
        # access, which is entirely separate (_make_provider above).
        return BinanceExchange(api_key="", api_secret="", testnet=False)

    async def run_once_account_sync(self) -> None:
        if not self.account_sync_breaker.can_attempt():
            logger.warning("account_sync_job skipped -- circuit open")
            return
        provider = self._make_provider()
        try:
            async with async_session() as session:
                await account_sync_job(session, provider)
            self.account_sync_breaker.record_success()
        except Exception as e:
            self.account_sync_breaker.record_failure()
            logger.error("account_sync_job failed", error=str(e))
        finally:
            await provider.close()

    async def run_once_exit_alerts(self) -> None:
        if not self.exit_alert_breaker.can_attempt():
            return
        try:
            async with async_session() as session:
                alerts = await exit_alert_job(session)
                if alerts:
                    from services.telegram.bot import TelegramBot
                    from dataclasses import asdict
                    bot = TelegramBot()
                    for alert in alerts:
                        await bot.send_exit_alert(asdict(alert))
            self.exit_alert_breaker.record_success()
        except Exception as e:
            self.exit_alert_breaker.record_failure()
            logger.error("exit_alert_job failed", error=str(e))

    async def run_once_signal_generation(self) -> None:
        if not self.signal_breaker.can_attempt():
            return
        try:
            async with async_session() as session:
                await signal_generation_job(session)
            self.signal_breaker.record_success()
        except Exception as e:
            self.signal_breaker.record_failure()
            logger.error("signal_generation_job failed", error=str(e))

    async def run_once_outcome_evaluation(self) -> None:
        if not self.outcome_breaker.can_attempt():
            return
        try:
            async with async_session() as session:
                await outcome_evaluation_job(session)
            self.outcome_breaker.record_success()
        except Exception as e:
            self.outcome_breaker.record_failure()
            logger.error("outcome_evaluation_job failed", error=str(e))

    async def run_once_candle_ingestion(self) -> None:
        if not self.candle_ingestion_breaker.can_attempt():
            logger.warning("candle_ingestion_job skipped -- circuit open")
            return
        exchange = self._make_binance_exchange()
        try:
            async with async_session() as session:
                stored = await candle_ingestion_job(session, exchange)
            self.candle_ingestion_breaker.record_success()
            logger.info("candle_ingestion_job succeeded", stored=stored)
        except Exception as e:
            self.candle_ingestion_breaker.record_failure()
            logger.error("candle_ingestion_job failed", error=str(e))
        finally:
            await exchange.close()

    async def run_once_live_breakout(self) -> None:
        if not self.live_breakout_breaker.can_attempt():
            logger.warning("live_breakout_job skipped -- circuit open")
            return
        try:
            async with async_session() as session:
                signal = await live_breakout_job(session, market_ws, self._live_candle_aggregator)
            self.live_breakout_breaker.record_success()
            if signal is not None:
                logger.info("live_breakout_job detected a new breakout", signal_id=signal.signal_id, signal_type=signal.signal_type)
        except Exception as e:
            self.live_breakout_breaker.record_failure()
            logger.error("live_breakout_job failed", error=str(e))

    async def _loop(self, tick_fn, interval_seconds: float) -> None:
        while self._running:
            await tick_fn()
            await asyncio.sleep(interval_seconds)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._loop(self.run_once_account_sync, ACCOUNT_SYNC_INTERVAL_SECONDS)),
            asyncio.create_task(self._loop(self.run_once_exit_alerts, EXIT_ALERT_INTERVAL_SECONDS)),
            asyncio.create_task(self._loop(self.run_once_signal_generation, SIGNAL_GENERATION_INTERVAL_SECONDS)),
            asyncio.create_task(self._loop(self.run_once_outcome_evaluation, OUTCOME_EVALUATION_INTERVAL_SECONDS)),
            asyncio.create_task(self._loop(self.run_once_candle_ingestion, CANDLE_INGESTION_INTERVAL_SECONDS)),
            asyncio.create_task(self._loop(self.run_once_live_breakout, LIVE_BREAKOUT_INTERVAL_SECONDS)),
        ]
        logger.info("Scheduler started", jobs=len(self._tasks))

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
        logger.info("Scheduler stopped")
