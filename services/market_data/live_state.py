"""Process-wide singleton for the live CoinDCX market-data WebSocket
(services/market_data/coindcx_ws.py), plus its connection-transition side
effects (an audit-trail SyncEvent row and a deduplicated Telegram alert).

Living in its own module -- rather than instantiated inside apps/api/main.py
directly -- so routers (e.g. apps/api/routers/dashboard.py) can import the
singleton without a circular import back through main.py, mirroring how
services/scheduler/runner.py's SchedulerRunner is wired.

No tick is ever persisted here -- only the latest state (in the
CoinDCXMarketDataWebSocket instance's own memory) and connection-transition
events (via SyncEvent, reusing the same table Phase 5's account sync
already writes to, source="coindcx_market_ws") are ever written to the DB,
per the explicit instruction not to grow the database with per-tick rows.

Startup retry (production hardening, added after a real transient
"Connection error" was observed during validation, with no automatic
retry -- python-socketio's own reconnection only activates AFTER a
connection has succeeded at least once, so a failed INITIAL attempt used
to leave market data UNAVAILABLE forever until a manual backend restart).
_StartupRetrySupervisor owns a single background task that retries only
the very first connect() with exponential backoff (2s/4s/8s/16s/30s,
holding at 30s); the moment that first connect succeeds, this
supervisor's job is done and every subsequent drop/reconnect is handled
entirely by CoinDCXMarketDataWebSocket's own existing (unmodified)
python-socketio reconnection -- never re-entered by this supervisor.
"""
import asyncio
from typing import Optional

import structlog

from database.schema import async_session
from database.schema.models import ConnectionState, SyncEvent, SyncStatus
from services.market_data.coindcx_ws import CoinDCXMarketDataWebSocket

logger = structlog.get_logger()

# Exponential backoff for the INITIAL connection only -- holds at the last
# (maximum) value once the list is exhausted, rather than growing further
# or giving up.
STARTUP_RETRY_DELAYS_SECONDS = (2, 4, 8, 16, 30)


async def _on_connection_transition(old_status: ConnectionState, new_status: ConnectionState) -> None:
    logger.warning("CoinDCX market-data connection transition", old=old_status.value, new=new_status.value)

    async with async_session() as session:
        session.add(SyncEvent(
            source="coindcx_market_ws",
            status=SyncStatus.SUCCESS.value if new_status == ConnectionState.LIVE else SyncStatus.FAILED.value,
            detail=f"{old_status.value} -> {new_status.value}",
        ))
        await session.commit()

    from services.telegram.bot import TelegramBot  # local import: avoid a hard telegram dependency for callers that never notify
    bot = TelegramBot()
    if new_status == ConnectionState.DISCONNECTED:
        await bot.send_market_data_alert("DISCONNECTED")
    elif new_status == ConnectionState.LIVE:
        await bot.send_market_data_alert("RECOVERED")


market_ws = CoinDCXMarketDataWebSocket(on_state_change=_on_connection_transition)


async def _default_wait_or_stop(stop_event: asyncio.Event, timeout: float) -> None:
    """Waits up to `timeout` seconds for `stop_event`, returning either
    way -- callers distinguish "backoff elapsed normally" from "stop was
    requested early" via stop_event.is_set() afterwards. Kept as a
    standalone, injectable function (rather than inlined) so tests can
    substitute an instant fake without monkeypatching the global asyncio
    module."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


class _StartupRetrySupervisor:
    """Owns at most one background task that retries market_ws's INITIAL
    connect() with exponential backoff. Never touches market_ws once it
    has connected successfully -- python-socketio's own built-in
    reconnection (unmodified) takes over completely from that point on,
    so this supervisor never races or duplicates it. Retries never call
    market_ws.connect()'s underlying join/subscribe path more than once
    per attempt, and only a SUCCESSFUL attempt ever reaches
    CoinDCXMarketDataWebSocket._on_connect (where subscriptions and the
    Telegram-alert state machine live) -- so a failed retry can never
    subscribe twice or fire a duplicate/spurious alert."""

    def __init__(self, connect_fn, wait_fn=_default_wait_or_stop) -> None:
        self._connect_fn = connect_fn
        self._wait_fn = wait_fn
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.is_running():
            logger.warning("CoinDCX market-data startup retry already running -- ignoring duplicate start")
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        attempt = 0
        while True:
            try:
                await self._connect_fn()
                logger.info("CoinDCX market-data WebSocket connected", attempt=attempt + 1)
                return
            except Exception as e:
                delay = STARTUP_RETRY_DELAYS_SECONDS[min(attempt, len(STARTUP_RETRY_DELAYS_SECONDS) - 1)]
                logger.warning(
                    "CoinDCX market-data WebSocket initial connection failed -- retrying",
                    attempt=attempt + 1, retry_in_seconds=delay, error=str(e),
                )
                attempt += 1
                await self._wait_fn(self._stop_event, delay)
                if self._stop_event.is_set():
                    return  # shutdown requested during the backoff wait


_startup_retry = _StartupRetrySupervisor(connect_fn=market_ws.connect)


async def start_market_data_ws() -> None:
    _startup_retry.start()


async def stop_market_data_ws() -> None:
    await _startup_retry.stop()
    await market_ws.disconnect()
