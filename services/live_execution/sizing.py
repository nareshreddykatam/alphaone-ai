"""Exact-precision Rs.200/10x position sizing (Contract Audit V2, Phase
3) -- extends services/risk_engine/fixed_margin.py's
size_fixed_margin_trade() (which computes a THEORETICAL quantity but never
rounds it to a real instrument's tradeable precision) with the real
CoinDCX instrument constraints from services/exchange/coindcx_instruments.py:
quantity_increment (step size), min_quantity, max_quantity, min_notional,
min_price/max_price.

CoinDCX's order-creation endpoint takes only `total_quantity` -- there is
no `margin_inr` parameter -- so achieving "Rs.200 margin" is entirely a
function of the quantity AlphaOne itself computes and rounds. Rounding
away from the theoretical quantity necessarily changes the realized
margin. This module tries both the floor and ceiling candidate at the
instrument's own quantity_increment and picks whichever lands closer to
the exact Rs.200 target -- then REJECTS if even the closer candidate
deviates beyond MAX_MARGIN_DEVIATION_PCT, rather than silently accepting
an instrument where Rs.200 is not really achievable (Section 8: "if exact
Rs.200 cannot be achieved due to exchange precision constraints: REJECT
rather than silently exceeding the limit").
"""
import math
from dataclasses import dataclass
from typing import Optional

from services.exchange.coindcx_instruments import InstrumentMetadata
from services.risk_engine.fixed_margin import FIXED_MARGIN_INR, FIXED_LEVERAGE

# How far the REALIZED margin (after rounding to the instrument's real
# quantity_increment) may deviate from the exact Rs.200 target before this
# module rejects the trade rather than silently exceeding/undershooting
# the user's stated limit. Conservative and explicit, not a guess dressed
# up as precision: 10% of Rs.200 = Rs.20, an amount small enough that a
# user reading "approximately Rs.200" would not consider it a violation of
# "Rs.200 exactly", but far tighter than accepting whatever an
# instrument's coarse step size happens to produce.
MAX_MARGIN_DEVIATION_PCT = 10.0


@dataclass
class PrecisionSizingResult:
    approved: bool
    reason: str
    quantity: float = 0.0
    realized_notional_usdt: float = 0.0
    realized_margin_usdt: float = 0.0
    realized_margin_inr: float = 0.0
    margin_deviation_pct: float = 0.0


def _round_to_increment(value: float, increment: float, direction: str) -> float:
    if increment <= 0:
        return value
    steps = value / increment
    steps = math.floor(steps) if direction == "down" else math.ceil(steps)
    # Fixed generous precision to guard float noise (e.g. 0.1 + 0.2 !=
    # 0.3) -- not derived from the increment's own string representation,
    # since very small increments (e.g. 1e-08) format in scientific
    # notation and would otherwise round away all real precision.
    return round(steps * increment, 10)


def calculate_precision_sized_quantity(
    entry_price_usdt: float, usdt_inr_rate: Optional[float], instrument: Optional[InstrumentMetadata],
    margin_inr: float = FIXED_MARGIN_INR, leverage: int = FIXED_LEVERAGE,
) -> PrecisionSizingResult:
    if instrument is None:
        return PrecisionSizingResult(approved=False, reason="No CoinDCX instrument metadata available -- cannot round to a real quantity precision.")
    if usdt_inr_rate is None or usdt_inr_rate <= 0:
        return PrecisionSizingResult(approved=False, reason="No live USDT/INR conversion rate available.")
    if entry_price_usdt is None or entry_price_usdt <= 0:
        return PrecisionSizingResult(approved=False, reason="Invalid entry price.")
    if instrument.status != "active" or instrument.exit_only:
        return PrecisionSizingResult(approved=False, reason=f"Instrument {instrument.pair} is not active for new entries (status={instrument.status}, exit_only={instrument.exit_only}).")
    if leverage > instrument.max_leverage_long or leverage > instrument.max_leverage_short:
        return PrecisionSizingResult(
            approved=False,
            reason=f"{instrument.pair} max leverage is {min(instrument.max_leverage_long, instrument.max_leverage_short):.0f}x -- {leverage}x is not supported.",
        )

    margin_usdt = margin_inr / usdt_inr_rate
    target_notional_usdt = margin_usdt * leverage
    raw_quantity = target_notional_usdt / entry_price_usdt

    candidates = []
    for direction in ("down", "up"):
        qty = _round_to_increment(raw_quantity, instrument.quantity_increment, direction)
        if qty < instrument.min_quantity or qty <= 0:
            continue
        if qty > instrument.max_quantity:
            continue
        notional = qty * entry_price_usdt
        if notional < instrument.min_notional:
            continue
        realized_margin_usdt = notional / leverage
        realized_margin_inr = realized_margin_usdt * usdt_inr_rate
        deviation_pct = abs(realized_margin_inr - margin_inr) / margin_inr * 100
        candidates.append(PrecisionSizingResult(
            approved=True, reason="OK", quantity=qty, realized_notional_usdt=round(notional, 8),
            realized_margin_usdt=round(realized_margin_usdt, 8), realized_margin_inr=round(realized_margin_inr, 4),
            margin_deviation_pct=round(deviation_pct, 4),
        ))

    if not candidates:
        return PrecisionSizingResult(
            approved=False,
            reason=(
                f"{instrument.pair}: no tradeable quantity (step={instrument.quantity_increment}, "
                f"min_quantity={instrument.min_quantity}, min_notional={instrument.min_notional} USDT) can represent "
                f"Rs.{margin_inr:.0f} margin at {leverage}x without violating the instrument's own minimums -- "
                f"target notional was only {target_notional_usdt:.4f} USDT."
            ),
        )

    best = min(candidates, key=lambda c: c.margin_deviation_pct)
    if best.margin_deviation_pct > MAX_MARGIN_DEVIATION_PCT:
        return PrecisionSizingResult(
            approved=False,
            reason=(
                f"{instrument.pair}: closest achievable margin after rounding to the real quantity precision is "
                f"Rs.{best.realized_margin_inr:.2f} ({best.margin_deviation_pct:.1f}% away from the Rs.{margin_inr:.0f} "
                f"target) -- exceeds the {MAX_MARGIN_DEVIATION_PCT:.0f}% deviation tolerance, rejecting rather than "
                f"silently exceeding the user's stated margin limit."
            ),
            quantity=best.quantity, realized_notional_usdt=best.realized_notional_usdt,
            realized_margin_usdt=best.realized_margin_usdt, realized_margin_inr=best.realized_margin_inr,
            margin_deviation_pct=best.margin_deviation_pct,
        )
    return best
