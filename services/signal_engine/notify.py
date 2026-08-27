"""Telegram notification for newly-generated signals (Phase 5, section 23:
"Signal Duplication -- prevent duplicate alerts... use NotificationLog...
implement cooldown/deduplication"). Deduped by signal_id -- in practice
`generate_and_persist_signal` always mints a fresh signal_id per call, so
this mainly guards against a future retry/scheduler path re-processing
the same Signal row.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import Signal, NotificationLog
from services.signal_engine.multi_strategy import get_definition_by_persisted_name


def _dedup_key(signal_id: str) -> str:
    return f"signal_alert:{signal_id}"


def _serialize_for_telegram(signal: Signal) -> dict:
    # Reverse-looked-up display name/strategy_id for the Telegram header
    # ("Strategy: S06 -- Supertrend + ATR") -- falls back to the raw
    # strategy_name string for a signal whose strategy isn't (or is no
    # longer) in the registry, rather than guessing.
    definition = get_definition_by_persisted_name(signal.strategy_name)
    return {
        "signal_id": signal.signal_id, "signal_type": signal.signal_type,
        "quality": signal.quality, "entry_price": signal.entry_price,
        "stop_loss": signal.stop_loss, "take_profit_1": signal.take_profit_1,
        "take_profit_2": signal.take_profit_2, "take_profit_3": signal.take_profit_3,
        "risk_reward": signal.risk_reward, "market_regime": signal.market_regime,
        "reasoning": signal.reasoning, "strategy_name": signal.strategy_name,
        "strategy_id": definition.strategy_id if definition else signal.strategy_name,
        "strategy_display_name": definition.display_name if definition else signal.strategy_name,
        "timeframe": signal.timeframe or (definition.timeframe if definition else None),
    }


async def notify_new_signal(session: AsyncSession, signal: Signal) -> bool:
    """Sends a Telegram alert for a new tradeable signal, unless already
    sent for this exact signal_id. Returns True if a send was attempted
    (TelegramBot.send_signal itself still no-ops if TELEGRAM_ENABLED is
    false or the signal is NO_TRADE), False if skipped as a duplicate."""
    if signal.signal_type == "NO_TRADE":
        return False

    key = _dedup_key(signal.signal_id)
    already_sent = (await session.execute(
        select(NotificationLog).where(NotificationLog.message_type == key)
    )).scalar_one_or_none()
    if already_sent is not None:
        return False

    from services.telegram.bot import TelegramBot  # local import: avoids a hard telegram dependency for callers that never notify

    bot = TelegramBot()
    await bot.send_signal(_serialize_for_telegram(signal))
    session.add(NotificationLog(signal_id=signal.signal_id, channel="telegram", message_type=key, status="sent"))
    await session.commit()
    return True
