"""Read-only MTProto ingestion of @suncrypto_trading_alerts via a real
Telegram USER ACCOUNT (services/telegram_signals/, services/telegram/
bot.py's Bot API path cannot reach this channel -- AlphaOne's operator
does not own or administer it, and the Bot API can only ever receive
channel_post updates for a channel the bot itself administers).

Uses Telethon, a maintained, pure-Python MTProto client library.

READ-ONLY BY CONSTRUCTION -- this module calls exactly three Telethon
client methods: connect(), is_user_authorized(), and get_entity() (a
read lookup), plus registers event handlers via add_event_handler(). It
never calls send_message, forward_messages, edit_message, delete_messages,
send_file, or any other mutating method -- see
tests/unit/test_no_order_placement_capability.py's dedicated coverage of
this module for the automated proof, and
services/telegram_mtproto/setup_session.py's own docstring for why the
one-time login step that WOULD need send_code_request/sign_in is
deliberately kept in a separate script this codebase's own automated
processes never run.

The actual message-handling logic (parse/validate/risk/paper-execute) is
NOT duplicated here -- every event calls straight into
services.telegram_signals.pipeline.process_incoming_channel_message, the
exact same function services/telegram/bot.py's Bot API path uses.
"""
import asyncio
from typing import Optional

import structlog

logger = structlog.get_logger()

# Same backoff shape as services/market_data/live_state.py's
# _StartupRetrySupervisor -- holds at 30s once exhausted, never gives up.
RECONNECT_DELAYS_SECONDS = (2, 4, 8, 16, 30)


def _client_configured() -> bool:
    from apps.api.config import get_settings
    settings = get_settings()
    return bool(settings.telegram_mtproto_enabled and settings.telegram_api_id and settings.telegram_api_hash and settings.telegram_session)


def build_client():
    """Constructs (but does not connect) a TelegramClient from a
    StringSession -- never a session FILE, so there is never a session
    artifact on disk to accidentally commit or leave behind on a Railway
    filesystem. Raises ValueError with a clear message if config is
    incomplete, rather than silently constructing a broken client."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from apps.api.config import get_settings

    settings = get_settings()
    if not (settings.telegram_api_id and settings.telegram_api_hash and settings.telegram_session):
        raise ValueError(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION are not all set -- "
            "run services/telegram_mtproto/setup_session.py once, yourself, to obtain a session string."
        )
    return TelegramClient(StringSession(settings.telegram_session), int(settings.telegram_api_id), settings.telegram_api_hash)


async def _handle_event(event, is_edit: bool) -> None:
    from database.schema import async_session
    from services.telegram_signals.pipeline import process_incoming_channel_message

    message = event.message
    if not message or not message.text:
        return

    chat = await event.get_chat()
    channel_username = f"@{chat.username}" if getattr(chat, "username", None) else None
    channel_id = str(chat.id)

    async with async_session() as session:
        await process_incoming_channel_message(
            session, channel_username or channel_id, str(message.id), message.date.replace(tzinfo=None),
            message.text, edited_timestamp=(message.edit_date.replace(tzinfo=None) if is_edit and message.edit_date else None),
            channel_id=channel_id,
        )


class MTProtoListener:
    """Owns the MTProto connection's lifecycle -- start/stop, initial-
    connect retry with backoff (mirrors _StartupRetrySupervisor), and
    guards against a duplicate listener ever being started twice in the
    same process. Telethon's own client keeps its receive loop alive for
    as long as the surrounding asyncio event loop runs and the client
    stays connected -- no blocking run_until_disconnected() call is used
    here, so this never blocks FastAPI's own event loop."""

    def __init__(self):
        self._client = None
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.is_running():
            logger.warning("MTProto listener already running -- ignoring duplicate start")
            return
        if not _client_configured():
            logger.info("MTProto listener not started -- TELEGRAM_MTPROTO_ENABLED is false or credentials are incomplete")
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
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    async def _run(self) -> None:
        from telethon import events
        from apps.api.config import get_settings

        settings = get_settings()
        attempt = 0
        while True:
            try:
                self._client = build_client()
                await self._client.connect()
                if not await self._client.is_user_authorized():
                    logger.error(
                        "MTProto session is not authorized -- run services/telegram_mtproto/setup_session.py "
                        "once to produce a valid TELEGRAM_SESSION"
                    )
                    return

                entity = await self._client.get_entity(settings.telegram_external_signal_channel)
                logger.info("MTProto listener connected", channel=settings.telegram_external_signal_channel)

                async def _on_new(event):
                    await _handle_event(event, is_edit=False)

                async def _on_edit(event):
                    await _handle_event(event, is_edit=True)

                self._client.add_event_handler(_on_new, events.NewMessage(chats=entity))
                self._client.add_event_handler(_on_edit, events.MessageEdited(chats=entity))
                return  # connected + handlers registered; Telethon's own loop keeps receiving from here
            except Exception as e:
                delay = RECONNECT_DELAYS_SECONDS[min(attempt, len(RECONNECT_DELAYS_SECONDS) - 1)]
                logger.warning("MTProto initial connection failed -- retrying", attempt=attempt + 1, retry_in_seconds=delay, error=str(e))
                attempt += 1
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                if self._stop_event.is_set():
                    return


mtproto_listener = MTProtoListener()
