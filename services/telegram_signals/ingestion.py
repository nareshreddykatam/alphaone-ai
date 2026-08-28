"""External Telegram signal ingestion + validation pipeline (Multi-Coin AI
Futures System, Phases 21, 23, 26-27). Read-only: this module only ever
READS from Telegram (via whatever real update handler calls into it, see
services/telegram/bot.py) and WRITES to AlphaOne's own database -- it
never sends, replies to, or forwards anything back to the external
channel.
"""
import hashlib
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.config import get_settings
from database.schema.models import ExternalTelegramMessage, ExternalSignal, ExternalSignalStatus
from services.telegram_signals.parser import parse_external_signal

# Real-time entry-deviation tolerance -- a signal whose stated entry is
# further than this from the CURRENT CoinDCX market price is stale/chased,
# not a fresh, actionable setup (Phase 26).
MAX_ENTRY_DEVIATION_PCT = 1.0


def _raw_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_authorized_channel(source_channel: str, source_channel_id: Optional[str] = None) -> bool:
    """Only the configured, explicitly allowlisted channel is processed
    (Phase 21) -- everything else is ignored at the earliest possible
    point, before even being persisted as a message worth parsing.

    Prefers the stable numeric channel ID over the username when BOTH a
    configured ID and an observed ID are available (Phase 4: a username
    can be reassigned to a different channel later; the numeric ID
    cannot) -- falls back to username matching otherwise, which is all
    the Bot API channel_post path can ever provide."""
    settings = get_settings()
    configured_id = settings.telegram_external_signal_channel_id.strip()
    if configured_id and source_channel_id is not None:
        return str(source_channel_id).strip() == configured_id

    configured_username = settings.telegram_external_signal_channel.lstrip("@").lower()
    return source_channel.lstrip("@").lower() == configured_username


async def ingest_message(
    session: AsyncSession, source_channel: str, telegram_message_id: str,
    message_timestamp: datetime, text: str, edited_timestamp: Optional[datetime] = None,
    source_channel_id: Optional[str] = None,
) -> Optional[ExternalTelegramMessage]:
    """Idempotent: a duplicate delivery of the same (channel, message_id)
    is recognized and returns the EXISTING row unchanged (Phase 23:
    'Never process the same message twice'). A genuine Telegram edit
    (edited_timestamp newer than what's stored) updates edited_text/
    edited_timestamp on the same row -- the original text/received_at are
    never overwritten, so a parse can always be traced to what was
    actually posted at each point in time."""
    existing = (await session.execute(
        select(ExternalTelegramMessage).where(
            ExternalTelegramMessage.source_channel == source_channel,
            ExternalTelegramMessage.telegram_message_id == telegram_message_id,
        )
    )).scalar_one_or_none()

    if existing is not None:
        if edited_timestamp is not None and (
            existing.edited_timestamp is None or edited_timestamp > existing.edited_timestamp
        ):
            existing.edited_text = text
            existing.edited_timestamp = edited_timestamp
            await session.commit()
        return existing

    message = ExternalTelegramMessage(
        source_channel=source_channel, source_channel_id=str(source_channel_id) if source_channel_id is not None else None,
        telegram_message_id=telegram_message_id,
        message_timestamp=message_timestamp, text=text, raw_hash=_raw_hash(text),
    )
    session.add(message)
    await session.commit()
    return message


def classify_entry_deviation(entry_price: float, current_price: Optional[float]) -> tuple[str, str]:
    if current_price is None:
        return "ENTRY_STALE", "No live CoinDCX price available to validate this entry against."
    if current_price <= 0 or entry_price <= 0:
        return "ENTRY_INVALID", "Non-positive price."
    deviation_pct = abs(entry_price - current_price) / current_price * 100
    if deviation_pct > MAX_ENTRY_DEVIATION_PCT:
        return "ENTRY_TOO_FAR", (
            f"Stated entry {entry_price} is {deviation_pct:.2f}% away from the current CoinDCX price "
            f"{current_price} (max allowed {MAX_ENTRY_DEVIATION_PCT}%) -- not chased."
        )
    return "ENTRY_VALID", "OK"


async def process_message(
    session: AsyncSession, message: ExternalTelegramMessage, supported_symbols: set[str],
    current_price_lookup=None,
) -> ExternalSignal:
    """Parses + validates one already-ingested message into a persisted
    ExternalSignal row. `current_price_lookup` is an injected async
    callable `(symbol) -> Optional[float]` (real CoinDCX price in
    production; a fixed fake in tests) -- kept as a parameter rather than
    a hard import so this function has no network dependency of its own.
    """
    if not is_authorized_channel(message.source_channel, message.source_channel_id):
        signal = ExternalSignal(
            message_id=message.id, source_channel=message.source_channel,
            status=ExternalSignalStatus.REJECTED_UNAUTHORIZED_SOURCE.value,
            rejection_reason=f"{message.source_channel} is not the allowlisted signal source.",
        )
        session.add(signal)
        await session.commit()
        return signal

    text = message.edited_text or message.text
    parsed = parse_external_signal(text, supported_symbols)

    market_price = None
    entry_status, entry_reason = "OK", "OK"
    if parsed.status == "VALID" and current_price_lookup is not None:
        market_price = await current_price_lookup(parsed.symbol)
        entry_status, entry_reason = classify_entry_deviation(parsed.entry_price, market_price)
        if entry_status != "ENTRY_VALID":
            parsed.status = entry_status
            parsed.rejection_reason = entry_reason

    signal = ExternalSignal(
        message_id=message.id, source_channel=message.source_channel, status=parsed.status,
        rejection_reason=parsed.rejection_reason, raw_symbol=parsed.raw_symbol, symbol=parsed.symbol,
        direction=parsed.direction, entry_price=parsed.entry_price, stop_loss=parsed.stop_loss,
        take_profit_1=parsed.take_profit_1, take_profit_2=parsed.take_profit_2, take_profit_3=parsed.take_profit_3,
        leverage_stated=parsed.leverage_stated, timeframe_stated=parsed.timeframe_stated,
        market_price_at_validation=market_price,
    )
    session.add(signal)
    await session.commit()
    return signal
