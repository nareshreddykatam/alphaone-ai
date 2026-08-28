"""Timestamp safety for CoinDCX futures order requests (Contract Audit
V2, Phase 6). CoinDCX's own documented order-creation contract states:
"Orders with a delay of more than 10 seconds will be rejected." -- every
official code sample uses `int(time.time() * 1000)` (epoch
MILLISECONDS), matching AlphaOne's existing convention in
services/exchange/coindcx.py's `_sign()`/`_post()`.

Two distinct failure modes this module exists to keep separate:
  1. Our own request is built too long before it's sent (clock skew on
     this machine, or time spent in the gate-check pipeline before
     submission) -- caught by `is_order_timestamp_fresh()` BEFORE ever
     attempting a real call.
  2. The response to a real call never arrives (network timeout) -- this
     is NOT a staleness problem and must NEVER be treated as one. An
     ambiguous timeout means AlphaOne does not know whether CoinDCX
     received and processed the order; the safe action is to reconcile
     against the real account (services/live_execution/reconciliation.py)
     before ever considering a retry, never to blindly resend the same
     request against a "the clock must have moved" assumption.
"""
import time
from dataclasses import dataclass
from typing import Optional

# The documented rejection window itself.
ORDER_TIMESTAMP_MAX_AGE_SECONDS = 10

# AlphaOne's own conservative internal margin below the exchange's actual
# 10s cutoff -- a request built at exactly 9.9s old and then subject to
# normal network latency could still arrive at CoinDCX past the real
# limit. Building the request with headroom avoids relying on a boundary
# the docs describe as an outright rejection, not a warning.
SAFE_SUBMISSION_MARGIN_SECONDS = 3


@dataclass
class TimestampCheck:
    ok: bool
    reason: str
    age_seconds: Optional[float] = None


def current_order_timestamp_ms(now: Optional[float] = None) -> int:
    """Epoch milliseconds, matching every official CoinDCX code sample --
    never seconds, despite some of the docs' own prose saying "EPOCH
    timestamp in seconds" (the code samples were trusted over the
    possibly-stale prose, per the existing note in
    docs/coindcx_api_findings.md)."""
    now = time.time() if now is None else now
    return int(round(now * 1000))


def is_order_timestamp_fresh(built_at_ms: int, now: Optional[float] = None) -> TimestampCheck:
    """Checked immediately before a (hypothetical, currently-unreachable)
    real submission -- a request built too long ago must never be sent
    as-is; the caller must rebuild it with a fresh timestamp rather than
    resubmit a stale one."""
    now_ms = current_order_timestamp_ms(now)
    age_seconds = (now_ms - built_at_ms) / 1000.0
    if age_seconds < 0:
        return TimestampCheck(ok=False, reason=f"Timestamp is {abs(age_seconds):.1f}s in the future -- clock skew.", age_seconds=age_seconds)
    if age_seconds > (ORDER_TIMESTAMP_MAX_AGE_SECONDS - SAFE_SUBMISSION_MARGIN_SECONDS):
        return TimestampCheck(
            ok=False,
            reason=f"Timestamp is {age_seconds:.1f}s old -- exceeds the safe submission window "
                   f"({ORDER_TIMESTAMP_MAX_AGE_SECONDS - SAFE_SUBMISSION_MARGIN_SECONDS}s, CoinDCX's own documented "
                   f"cutoff is {ORDER_TIMESTAMP_MAX_AGE_SECONDS}s).",
            age_seconds=age_seconds,
        )
    return TimestampCheck(ok=True, reason="OK", age_seconds=age_seconds)
