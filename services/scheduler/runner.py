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
from services.scheduler.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from services.scheduler.jobs import account_sync_job, exit_alert_job, signal_generation_job, outcome_evaluation_job

logger = structlog.get_logger()

ACCOUNT_SYNC_INTERVAL_SECONDS = 30
EXIT_ALERT_INTERVAL_SECONDS = 30
SIGNAL_GENERATION_INTERVAL_SECONDS = 900  # 15 min -- primary strategy is 4h, no value in checking more often
OUTCOME_EVALUATION_INTERVAL_SECONDS = 900


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

    def _make_provider(self) -> CoinDCXReadOnlyAccountProvider:
        return CoinDCXReadOnlyAccountProvider(self._api_key, self._api_secret)

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
