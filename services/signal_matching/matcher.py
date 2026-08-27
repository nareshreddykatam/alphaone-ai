"""Signal-to-trade matching (Phase 4H). Matches a manually-reported trade
to a candidate AlphaOne signal by symbol + direction + timestamp proximity
+ entry-price proximity, so trade-journal entries can be linked back to
"which signal (if any) prompted this" for slippage analysis and the
taken-vs-missed signal split (services/portfolio/service.py). Never
silently guesses when candidates are ambiguous -- callers must surface
those for manual confirmation instead of auto-picking one.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.schema.models import Signal

DEFAULT_TIME_WINDOW_HOURS = 4.0
DEFAULT_PRICE_TOLERANCE_PCT = 2.0
# Two candidates within this confidence gap of each other are ambiguous --
# require manual confirmation rather than guessing which one is right.
AMBIGUITY_MARGIN = 0.15


@dataclass
class SignalMatchCandidate:
    signal_id: str
    confidence: float
    time_diff_minutes: float
    price_diff_pct: float


async def find_candidate_signals(
    session: AsyncSession,
    *,
    symbol: str,
    side: str,
    entry_price: float,
    timestamp: datetime,
    time_window_hours: float = DEFAULT_TIME_WINDOW_HOURS,
    price_tolerance_pct: float = DEFAULT_PRICE_TOLERANCE_PCT,
) -> list[SignalMatchCandidate]:
    window = timedelta(hours=time_window_hours)
    result = await session.execute(
        select(Signal).where(
            Signal.symbol == symbol,
            Signal.signal_type == side,
            Signal.timestamp >= timestamp - window,
            Signal.timestamp <= timestamp + window,
        )
    )
    candidates = []
    for signal in result.scalars().all():
        if not signal.entry_price:
            continue
        price_diff_pct = abs(entry_price - signal.entry_price) / signal.entry_price * 100
        if price_diff_pct > price_tolerance_pct:
            continue
        time_diff_minutes = abs((signal.timestamp - timestamp).total_seconds()) / 60

        time_score = 1 - min(time_diff_minutes / (time_window_hours * 60), 1.0)
        price_score = 1 - min(price_diff_pct / price_tolerance_pct, 1.0)
        confidence = 0.5 * time_score + 0.5 * price_score

        candidates.append(SignalMatchCandidate(
            signal_id=signal.signal_id, confidence=round(confidence, 4),
            time_diff_minutes=round(time_diff_minutes, 2), price_diff_pct=round(price_diff_pct, 4),
        ))

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates


def pick_confident_match(candidates: list[SignalMatchCandidate]) -> Optional[SignalMatchCandidate]:
    """Returns the single confident match, or None if there isn't one --
    zero candidates, or the top two are too close to call automatically."""
    if len(candidates) == 0:
        return None
    if len(candidates) == 1:
        return candidates[0]
    if candidates[0].confidence - candidates[1].confidence >= AMBIGUITY_MARGIN:
        return candidates[0]
    return None  # ambiguous -- caller must surface all candidates for manual confirmation
