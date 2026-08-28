"""Deterministic idempotency keys for live execution candidates (Phase
10-11). The DATABASE's own unique constraint on LiveExecution.idempotency_key
is the actual safety mechanism -- this module only computes the key
deterministically from the candidate's own identity, so two independent
calls describing the "same" signal always produce the identical key,
regardless of process restart, Telegram reconnect, or scheduler retry.
"""
from dataclasses import dataclass
from typing import Optional


def compute_idempotency_key(
    source: str, symbol: str, signal_id: Optional[str] = None,
    strategy_or_channel: Optional[str] = None, candle_timestamp: Optional[str] = None,
) -> str:
    """Prefers signal_id (already globally unique per source -- an
    AlphaOne Signal.signal_id or an ExternalSignal's own id) when
    available; falls back to a source+symbol+strategy+candle-timestamp
    composite otherwise, matching Phase 10's "appropriate combination of
    source / channel-or-message-id / symbol / timeframe / strategy /
    signal ID" -- either way, calling this twice for the objectively same
    real-world event must always return the same string."""
    if signal_id:
        return f"{source}:{symbol}:{signal_id}"
    parts = [source, symbol, strategy_or_channel or "unknown", candle_timestamp or "unknown"]
    return ":".join(parts)
