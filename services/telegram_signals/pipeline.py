"""Shared processing pipeline for one incoming external-channel message,
regardless of HOW it was received -- the Bot API's channel_post updates
(services/telegram/bot.py, requires the bot to be a channel admin) and
the MTProto user-account listener (services/telegram_mtproto/, for a
channel AlphaOne's operator does not administer) both call this SAME
function, so the ingestion/parsing/validation/risk/execution logic is
never duplicated between the two channel-reading transports.

Signal -> Validation -> Risk Engine -> Paper Execution -- exactly one
path, exercised identically no matter which transport delivered the
message.
"""
from datetime import datetime
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import ExternalSignal
from services.telegram_signals.ingestion import is_authorized_channel, ingest_message, process_message
from services.telegram_signals.paper_execution import execute_valid_signal

logger = structlog.get_logger()


async def process_incoming_channel_message(
    session: AsyncSession, channel: str, telegram_message_id: str, message_timestamp: datetime,
    text: str, edited_timestamp: Optional[datetime] = None, channel_id: Optional[str] = None,
    notify: bool = True,
) -> Optional[ExternalSignal]:
    """Returns the persisted ExternalSignal, or None if the channel isn't
    authorized (nothing is even persisted in that case -- Phase 21)."""
    if not is_authorized_channel(channel, channel_id):
        logger.warning("Ignoring message from an unauthorized/unallowlisted channel", channel=channel)
        return None

    message = await ingest_message(
        session, channel, telegram_message_id, message_timestamp, text,
        edited_timestamp=edited_timestamp, source_channel_id=channel_id,
    )

    from services.scanner.multi_coin import DEFAULT_WHITELIST, check_instrument_availability
    supported = set(DEFAULT_WHITELIST)

    async def _price_lookup(symbol: str):
        results = await check_instrument_availability([symbol])
        return results[0].last_price if results and results[0].available else None

    signal = await process_message(session, message, supported, current_price_lookup=_price_lookup)
    logger.info("External Telegram signal processed", channel=channel, status=signal.status, symbol=signal.symbol)

    if signal.status != "VALID":
        return signal

    from services.exchange.fx import get_usdt_inr_rate
    rate = await get_usdt_inr_rate()
    inr_rate = rate.rate if rate is not None else None
    position, reason = await execute_valid_signal(session, signal, inr_rate)

    if position is None:
        logger.info("External Telegram signal was VALID but not executed", reason=reason)
        return signal

    logger.info("External Telegram signal opened a paper position", trade_id=position.trade_id)
    if notify:
        from services.telegram.bot import TelegramBot
        bot = TelegramBot()
        if bot.enabled:
            await bot.send_paper_signal({
                "symbol": signal.symbol, "market": f"CoinDCX {signal.symbol} Perpetual",
                "timeframe": signal.timeframe_stated or "unspecified",
                "strategy_sources": [f"TELEGRAM:{channel}"], "direction": signal.direction,
                "probability_long": None, "probability_short": None, "probability_no_trade": None,
                "expected_return": None, "expected_volatility": None, "regime": "UNKNOWN",
                "confidence": "EXTERNAL_SIGNAL", "entry": signal.entry_price, "stop_loss": signal.stop_loss,
                "take_profit_1": signal.take_profit_1, "take_profit_2": signal.take_profit_2,
                "take_profit_3": signal.take_profit_3, "risk_reward": None,
                "model_status": "NO_MODEL_DEPLOYED", "model_version": None,
            })
    return signal
