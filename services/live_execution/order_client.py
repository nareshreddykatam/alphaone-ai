"""The ONE place in this entire codebase where a real CoinDCX order
WOULD be submitted, if AlphaOne ever verified the real order-creation
contract. It does not do so today -- this function always raises,
regardless of arguments, regardless of how it's called, and is never
reached in practice anyway because services/live_execution/gates.py's
ORDER_CONTRACT_VERIFIED gate can never pass (see that module's own
docstring for why).

This is deliberate defense-in-depth, matching the existing project
pattern: services/exchange/coindcx.py's CoinDCXReadOnlyAccountProvider
has NO order-placement METHOD AT ALL (the strongest possible guarantee --
not "returns an error", but "the capability does not exist in code").
This module adds a second, independent layer for the live-execution path
specifically: even if the gate check were ever bypassed by a future bug,
this function still refuses, unconditionally.

Filling this in for real requires, at minimum (see
docs/coindcx_api_findings.md and reports/LIVE_EXECUTION_V1_REPORT.txt):
  1. The exact request body schema for
     POST /exchange/v1/derivatives/futures/orders/create (order_type,
     side/position-side semantics, how leverage is set per-order vs.
     per-position, native SL/TP attachment mechanism if any, client
     order ID field for idempotency).
  2. Real per-instrument quantity step / minimum quantity / minimum
     notional / price precision (from the instrument-details endpoint's
     REAL response, not assumed).
  3. Manual verification against a real account in a low-risk,
     controlled way BEFORE any automated system is allowed to call it
     (matching the same "Real Account Acceptance Test" discipline
     docs/known_limitations.md already documents for the read-only
     account sync).
"""
from typing import Optional


class OrderContractNotVerifiedError(RuntimeError):
    pass


def submit_futures_order(
    instrument: str, side: str, quantity: float, leverage: int,
    stop_loss: Optional[float] = None, take_profit: Optional[float] = None,
    client_order_id: Optional[str] = None,
) -> dict:
    """Always raises. Never makes an HTTP request. See module docstring."""
    raise OrderContractNotVerifiedError(
        "CoinDCX's real futures order-creation contract has not been verified against a live account -- "
        "real order submission is permanently disabled in this codebase until that verification is done "
        "and this function is deliberately rewritten. See reports/LIVE_EXECUTION_V1_REPORT.txt."
    )
