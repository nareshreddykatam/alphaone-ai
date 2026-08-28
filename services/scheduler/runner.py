"""Background scheduler runner (Phase 5, section 30). Must run on a
persistent process -- NEVER inside a serverless function (see
docs/deployment.md). Each job runs in its own asyncio loop, guarded by its
own CircuitBreaker so a failing CoinDCX API doesn't get hammered and one
job's failure can't take down the others.

Each `_run_once_*` method is a single tick, fully testable in isolation
without running the infinite loop -- the loops themselves
(`_loop_account_sync` etc.) are thin `while True: tick(); sleep()` wrappers
around them, started only from `start()`.

Watchdog + heartbeat (added after a real production incident: every
DB-writing scheduler job went silent for 12+ minutes -- no new SyncEvent
row, success or failure -- while the CoinDCX WebSocket, which does not
touch the DB per-tick, kept updating fine; root cause could not be
confirmed from Railway logs, so this closes every plausible mechanism
rather than guessing one). Each `_loop` iteration is now bounded by
`JOB_TICK_TIMEOUT_SECONDS` via `asyncio.wait_for` -- a single stuck DB
call, connection-pool checkout, or network call can therefore never block
that job's loop forever; it times out, is logged, and the loop continues
on schedule. `_last_tick_at` is stamped at the START of every iteration
(before the job itself runs), independent of success/failure/timeout --
this is what actually distinguishes "the loop stopped iterating" from
"the loop is iterating but every job attempt fails before writing
anything," which black-box DB inspection alone cannot. Exposed read-only
via GET /api/v1/health/scheduler (apps/api/routers/health.py) -- no
credentials, matching that router's existing disclosure policy.
"""
import asyncio
from datetime import datetime

import structlog

from apps.api.config import get_settings
from database.schema import async_session
from services.exchange.coindcx import CoinDCXReadOnlyAccountProvider
from services.market_data.binance import BinanceExchange
from services.market_data.live_state import market_ws, live_candle_aggregators
from services.scheduler.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from services.scheduler.jobs import (
    account_sync_job, exit_alert_job, signal_generation_job, outcome_evaluation_job, candle_ingestion_job,
    live_breakout_job, ai_paper_trading_job,
)

logger = structlog.get_logger()

ACCOUNT_SYNC_INTERVAL_SECONDS = 30
EXIT_ALERT_INTERVAL_SECONDS = 30
SIGNAL_GENERATION_INTERVAL_SECONDS = 900  # 15 min -- primary strategy is 4h, no value in checking more often
SIGNAL_GENERATION_15M_INTERVAL_SECONDS = 900  # 15 min -- exactly one check per new 15m candle; see jobs.py
OUTCOME_EVALUATION_INTERVAL_SECONDS = 900
CANDLE_INGESTION_INTERVAL_SECONDS = 900  # 15 min -- see services/scheduler/jobs.py's module docstring for the reasoning
LIVE_BREAKOUT_INTERVAL_SECONDS = 30  # same cadence as account_sync/exit_alert -- see jobs.py's module docstring

# Generous enough for a normal tick (DB round-trip + at most one external
# API call) under real-world load, but bounded -- see module docstring.
JOB_TICK_TIMEOUT_SECONDS = 60


class SchedulerRunner:
    def __init__(self, api_key: str = "", api_secret: str = ""):
        self._api_key = api_key
        self._api_secret = api_secret
        self._tasks: list[asyncio.Task] = []
        self._running = False
        # Stamped at the START of every _loop iteration, before the job
        # itself runs -- see module docstring. Keys match the `name`
        # strings passed to _loop() in start().
        self._last_tick_at: dict[str, datetime] = {}

        self.account_sync_breaker = CircuitBreaker(config=CircuitBreakerConfig())
        self.exit_alert_breaker = CircuitBreaker(config=CircuitBreakerConfig())
        self.signal_breaker = CircuitBreaker(config=CircuitBreakerConfig())
        self.signal_breaker_15m = CircuitBreaker(config=CircuitBreakerConfig())
        self.outcome_breaker = CircuitBreaker(config=CircuitBreakerConfig())
        self.candle_ingestion_breaker = CircuitBreaker(config=CircuitBreakerConfig())
        self.live_breakout_breaker = CircuitBreaker(config=CircuitBreakerConfig())
        self.ai_paper_trading_breaker = CircuitBreaker(config=CircuitBreakerConfig())
        # The SHARED, process-wide 4h forming-candle aggregator (services/
        # market_data/live_state.py's registry -- not a private instance)
        # -- so the Live Chart (apps/api/routers/market.py) and this
        # scheduler's live signal detection always see the exact same
        # forming-bar state, never two independently-drifting copies. The
        # validated strategy is 4h-only, so only that one entry is ever
        # read here; the registry's other timeframes exist purely for
        # chart/market-state display.
        self._live_candle_aggregator = live_candle_aggregators["4h"]

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
                await signal_generation_job(session, timeframe="4h")
                # AI Trading V1: runs strictly AFTER signal_generation_job's
                # own commit, in the same tick -- reads back the Signal rows
                # it just persisted, so this must never be its own
                # independently-scheduled loop (see ai_paper_trading_job's
                # own docstring for why). Guarded by its own breaker so a
                # paper-trading failure can never affect the 4h strategy
                # signal path's own breaker/schedule.
                if self.ai_paper_trading_breaker.can_attempt():
                    try:
                        opened = await ai_paper_trading_job(session, timeframe="4h")
                        self.ai_paper_trading_breaker.record_success()
                        if opened:
                            from dataclasses import asdict
                            from services.telegram.bot import TelegramBot
                            bot = TelegramBot()
                            for decision in opened:
                                await bot.send_paper_signal(asdict(decision))
                    except Exception as e:
                        self.ai_paper_trading_breaker.record_failure()
                        logger.error("ai_paper_trading_job failed", error=str(e))
            self.signal_breaker.record_success()
        except Exception as e:
            self.signal_breaker.record_failure()
            logger.error("signal_generation_job failed", error=str(e), timeframe="4h")

    async def run_once_signal_generation_15m(self) -> None:
        """Independent tick + breaker from the 4h path -- a failure
        evaluating 15m strategies must never affect the 4h path's own
        breaker/schedule, and vice versa. Currently a no-op in production
        (every registered 15m strategy is RESEARCH_ONLY, see
        services/signal_engine/multi_strategy.py) but ticks for real so a
        future production-eligible 15m strategy needs no new wiring."""
        if not self.signal_breaker_15m.can_attempt():
            return
        try:
            async with async_session() as session:
                await signal_generation_job(session, timeframe="15m")
            self.signal_breaker_15m.record_success()
        except Exception as e:
            self.signal_breaker_15m.record_failure()
            logger.error("signal_generation_job failed", error=str(e), timeframe="15m")

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

    async def _loop(self, name: str, tick_fn, interval_seconds: float) -> None:
        while self._running:
            self._last_tick_at[name] = datetime.utcnow()
            try:
                await asyncio.wait_for(tick_fn(), timeout=JOB_TICK_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                # tick_fn's own try/except (inside each run_once_* method)
                # cannot catch this -- wait_for cancels tick_fn() at its
                # current await point, and CancelledError is a BaseException,
                # not an Exception, so it never reaches `except Exception`
                # there. Logged here instead, and the loop continues on its
                # normal schedule rather than staying stuck.
                logger.error(
                    "Scheduler job tick exceeded watchdog timeout -- continuing on schedule",
                    job=name, timeout_seconds=JOB_TICK_TIMEOUT_SECONDS,
                )
            await asyncio.sleep(interval_seconds)

    def get_heartbeat(self) -> dict:
        """Read-only scheduler health, safe to expose over HTTP (no
        credentials) -- see GET /api/v1/health/scheduler. `last_tick_at`
        proves the job's loop is actually iterating, independent of
        whether the job itself is succeeding; `seconds_since_last_tick`
        makes staleness obvious without the caller doing datetime math."""
        now = datetime.utcnow()
        breakers = {
            "account_sync": self.account_sync_breaker,
            "exit_alerts": self.exit_alert_breaker,
            "signal_generation": self.signal_breaker,
            "signal_generation_15m": self.signal_breaker_15m,
            "outcome_evaluation": self.outcome_breaker,
            "candle_ingestion": self.candle_ingestion_breaker,
            "live_breakout": self.live_breakout_breaker,
        }
        jobs = {}
        for name, breaker in breakers.items():
            last_tick = self._last_tick_at.get(name)
            jobs[name] = {
                "last_tick_at": last_tick.isoformat() if last_tick else None,
                "seconds_since_last_tick": (now - last_tick).total_seconds() if last_tick else None,
                "circuit_state": breaker.state.value,
                "consecutive_failures": breaker.consecutive_failures,
            }
        # ai_paper_trading_job has no _loop/interval of its own -- it runs
        # inline, strictly after signal_generation_job, within the SAME
        # tick (see run_once_signal_generation) -- so it shares that tick's
        # timestamp rather than stamping a separate one.
        signal_tick = self._last_tick_at.get("signal_generation")
        jobs["ai_paper_trading"] = {
            "last_tick_at": signal_tick.isoformat() if signal_tick else None,
            "seconds_since_last_tick": (now - signal_tick).total_seconds() if signal_tick else None,
            "circuit_state": self.ai_paper_trading_breaker.state.value,
            "consecutive_failures": self.ai_paper_trading_breaker.consecutive_failures,
        }
        return {"scheduler_running": self._running, "jobs": jobs}

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._loop("account_sync", self.run_once_account_sync, ACCOUNT_SYNC_INTERVAL_SECONDS)),
            asyncio.create_task(self._loop("exit_alerts", self.run_once_exit_alerts, EXIT_ALERT_INTERVAL_SECONDS)),
            asyncio.create_task(self._loop("signal_generation", self.run_once_signal_generation, SIGNAL_GENERATION_INTERVAL_SECONDS)),
            asyncio.create_task(self._loop("signal_generation_15m", self.run_once_signal_generation_15m, SIGNAL_GENERATION_15M_INTERVAL_SECONDS)),
            asyncio.create_task(self._loop("outcome_evaluation", self.run_once_outcome_evaluation, OUTCOME_EVALUATION_INTERVAL_SECONDS)),
            asyncio.create_task(self._loop("candle_ingestion", self.run_once_candle_ingestion, CANDLE_INGESTION_INTERVAL_SECONDS)),
            asyncio.create_task(self._loop("live_breakout", self.run_once_live_breakout, LIVE_BREAKOUT_INTERVAL_SECONDS)),
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


# Process-wide singleton, constructed here (not in apps/api/main.py) so
# routers (e.g. apps/api/routers/health.py, for GET /health/scheduler) can
# import it directly without a circular import back through main.py --
# same reasoning as services/market_data/live_state.py's `market_ws`
# singleton. apps/api/main.py imports this instead of constructing its own
# SchedulerRunner. `apps.api.config` is already an existing transitive
# dependency of this module (via `database.schema`), so importing it at
# module scope here introduces no new import-order risk.
_settings = get_settings()
scheduler = SchedulerRunner(_settings.coindcx_api_key, _settings.coindcx_api_secret)
