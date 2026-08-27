"""Pure PnL/fee/R-multiple math shared by PaperTrader (simulated fills) and
the manual trade journal (user-reported real fills). Kept as pure functions,
no DB/session dependency, so both callers compute a closed trade's numbers
identically and never drift apart.

Formulas match services/paper_trader/engine.py's original inline math
exactly -- extracted here rather than duplicated.
"""
from dataclasses import dataclass

DEFAULT_FEE_RATE = 0.0004  # taker-fee research assumption, see docs/exchange_assumptions.md


@dataclass
class SlicePnl:
    pnl: float
    fees: float
    pnl_pct: float


def compute_slice_pnl(
    side: str,
    entry_price: float,
    exit_price: float,
    quantity: float,
    leverage: int = 1,
    fee_rate: float = DEFAULT_FEE_RATE,
) -> SlicePnl:
    """PnL for one closed quantity slice (a full exit or one partial exit)."""
    if side == "LONG":
        pnl = (exit_price - entry_price) * quantity * leverage
    else:
        pnl = (entry_price - exit_price) * quantity * leverage

    fee = (entry_price + exit_price) * quantity * fee_rate
    pnl -= fee

    notional = entry_price * quantity
    pnl_pct = (pnl / notional) * 100 if notional > 0 else 0.0

    return SlicePnl(pnl=pnl, fees=fee, pnl_pct=pnl_pct)


def compute_r_multiple(entry_price: float, stop_loss: float, quantity: float, total_pnl: float) -> float:
    """R-multiple of a trade's total realized PnL against its original stop distance."""
    risk = abs(entry_price - stop_loss)
    if risk <= 0 or quantity <= 0:
        return 0.0
    return total_pnl / (risk * quantity)
