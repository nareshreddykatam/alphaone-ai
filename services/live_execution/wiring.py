"""The real signal-to-live-execution integration point (Contract Audit
V2, Phase 11). Traced, not guessed: an external Telegram signal reaches
"VALID" status inside services/telegram_signals/pipeline.py::
process_incoming_channel_message() (used identically by both the Bot API
and MTProto transports), which currently only ever calls
services/telegram_signals/paper_execution.py::execute_valid_signal() --
PAPER only. This module is the corresponding LIVE path, called from that
same integration point (see pipeline.py's own call to
maybe_attempt_live_execution() immediately after paper execution), kept
completely inert while AUTOMATIC_TRADING_ENABLED=false.

The AlphaOne-strategy signal path (services/signal_engine/live_breakout.py
-> Signal rows -> services/scheduler/jobs.py's AI paper-trading job) is
the second identified integration point (Phase 11 requires documenting,
not necessarily wiring, every source) -- not wired to this module in this
pass; see docs/coindcx_futures_contract_audit_v2.md's remaining-blockers
section for why (the AI paper-trading job's own candidate shape differs
enough from ExternalSignal that wiring it needs its own scoped pass, not
a rushed reuse of this function's signal-shaped input).

BELT AND SUSPENDERS: `maybe_attempt_live_execution()` checks
`settings.automatic_trading_enabled` as its very FIRST action, before
touching the database, before constructing a candidate, before any
network call -- so with the real production default (False), calling this
function from the real pipeline is a single boolean check and an
immediate return, not merely "gated deep inside a chain that eventually
rejects". This is independent of (not a replacement for)
services/live_execution/gates.py's own AUTOMATIC_TRADING_ENABLED gate,
which still runs if this function's own guard is ever removed or bypassed
by a future bug -- the same defense-in-depth pattern as
ORDER_CONTRACT_VERIFIED + order_client.py's unconditional raise.
"""
from datetime import datetime
from typing import Optional

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.config import get_settings
from database.schema.models import ExternalSignal

logger = structlog.get_logger()


async def maybe_attempt_live_execution(session: AsyncSession, signal: ExternalSignal, channel: str):
    """Returns None immediately, with zero database writes and zero
    network calls, unless AUTOMATIC_TRADING_ENABLED is true -- which it
    never is in production today (default False, not configurable via
    Telegram/AI/API/frontend). The "enabled" branch below is real,
    executable code (not a stub) so the integration point is genuinely
    complete, but it has never been exercised against a live account --
    only against mocks (tests/unit/test_live_execution_wiring.py) -- since
    exercising it for real requires exactly the settings this phase
    keeps off."""
    settings = get_settings()
    if not settings.automatic_trading_enabled:
        return None

    from services.exchange.coindcx import CoinDCXReadOnlyAccountProvider
    from services.exchange.coindcx_instruments import get_instrument_metadata
    from services.live_execution.daily_loss import check_daily_loss_limit
    from services.live_execution.executor import process_live_execution_candidate
    from services.live_execution.gates import LiveExecutionCandidate
    from services.live_execution.reconciliation import get_last_reconciliation_status
    from services.scanner.multi_coin import DEFAULT_WHITELIST, RESEARCHED_SYMBOLS, check_instrument_availability

    provider = CoinDCXReadOnlyAccountProvider(api_key=settings.coindcx_api_key, api_secret=settings.coindcx_api_secret)
    account_status = await provider.get_connection_status()
    account_healthy = account_status.get("status") == "OK"

    availability_results = await check_instrument_availability([signal.symbol])
    availability = availability_results[0] if availability_results else None
    market_healthy = bool(availability and availability.available and availability.is_fresh)
    current_price = availability.last_price if availability else None

    instrument_pair = availability.instrument if availability else None
    instrument_metadata = await get_instrument_metadata(instrument_pair, margin_currency="USDT") if instrument_pair else None

    conversion = await provider.get_futures_conversion_rate(margin_currency="USDT")
    usdt_inr_rate = conversion["rate"] if conversion else None

    daily_loss = await check_daily_loss_limit(session, provider)
    reconciliation_ok, reconciliation_reason = await get_last_reconciliation_status(session)

    candidate = LiveExecutionCandidate(
        source="TELEGRAM_EXTERNAL", symbol=signal.symbol, direction=signal.direction,
        entry_price=signal.entry_price, stop_loss=signal.stop_loss,
        take_profit_1=signal.take_profit_1, take_profit_2=signal.take_profit_2, take_profit_3=signal.take_profit_3,
        # ExternalSignal has no message-timestamp field of its own (that
        # lives on the parent ExternalTelegramMessage row this function
        # doesn't have); `created_at` (when this signal was parsed/
        # validated, effectively the same moment for a live channel post)
        # is the most honest freshness timestamp available here.
        signal_timestamp=signal.created_at, signal_id=str(signal.id),
        instrument=instrument_pair,
        instrument_eligible=bool(signal.symbol in DEFAULT_WHITELIST and signal.symbol in RESEARCHED_SYMBOLS),
        instrument_eligibility_reason="OK" if signal.symbol in RESEARCHED_SYMBOLS else f"{signal.symbol} has no validated strategy/eligibility check beyond the scanner whitelist.",
        current_market_price=current_price, instrument_metadata=instrument_metadata,
    )

    logger.warning(
        "AUTOMATIC_TRADING_ENABLED is true -- attempting live execution gate check",
        symbol=signal.symbol, channel=channel, signal_id=str(signal.id),
    )
    return await process_live_execution_candidate(
        session, candidate, usdt_inr_rate=usdt_inr_rate, market_data_healthy=market_healthy,
        coindcx_account_healthy=account_healthy, daily_loss_ok=daily_loss.approved, daily_loss_reason=daily_loss.reason,
        reconciliation_ok=reconciliation_ok, reconciliation_reason=reconciliation_reason,
    )
