"""Exact CoinDCX futures request-payload builders (Contract Audit V2,
Phases 4-8). These functions build the EXACT dict that CoinDCX's own
official documentation (https://docs.coindcx.com/, fetched directly
2026-08-28) shows for each endpoint's request body -- verbatim field
names, verbatim nesting, verbatim value types -- so the "exact request
contract" work Phase 5 requires is done and independently testable.

CRITICAL: nothing in this codebase calls these functions to actually send
a request. They exist ONLY so a unit test can assert "this is exactly what
WOULD be sent, if this were ever wired to a real HTTP call" --
services/live_execution/order_client.py's submit_futures_order() remains
the single, permanently-blocked path to a real order (see that module's
own docstring), and never imports anything from this file. See
docs/coindcx_futures_contract_audit_v2.md for the full doc-vs-repo audit
this module's payloads were built from, including the documented
discrepancies (e.g. the order-creation code sample uses order_type
"market_order" while the same page's own parameter table lists "market" --
both are reproduced here as build_create_order_request()'s exact
documented default, "market", per the parameter table, which is the more
authoritative of the two per that endpoint's own "Request Definitions"
section).
"""
from dataclasses import dataclass, field
from typing import Optional

# Exact enum values from the official docs' "Request Definitions" tables.
ORDER_SIDES = ("buy", "sell")
ORDER_TYPES = ("market", "limit", "stop_limit", "stop_market", "take_profit_limit", "take_profit_market")
TIME_IN_FORCE_OPTIONS = ("good_till_cancel", "fill_or_kill", "immediate_or_cancel")
NOTIFICATION_OPTIONS = ("no_notification", "email_notification")
POSITION_MARGIN_TYPES = ("isolated", "crossed")

# create_tpsl's own docs are explicit: "Only 'take_profit_market' is
# supported for now" / "Only 'stop_market' is supported for now" -- a
# narrower set than the order-creation endpoint's own order_types list,
# and NOT interchangeable with it.
TPSL_TAKE_PROFIT_ORDER_TYPE = "take_profit_market"
TPSL_STOP_LOSS_ORDER_TYPE = "stop_market"

# Documented order-request rejection window (Contract Audit V2, Phase 6):
# "Orders with a delay of more than 10 seconds will be rejected."
ORDER_TIMESTAMP_MAX_AGE_SECONDS = 10


def build_create_order_request(
    timestamp_ms: int, side: str, pair: str, total_quantity: float, leverage: Optional[int] = None,
    order_type: str = "market", price: Optional[float] = None, stop_price: Optional[float] = None,
    notification: str = "no_notification", time_in_force: Optional[str] = None,
    margin_currency_short_name: str = "USDT", position_margin_type: Optional[str] = None,
) -> dict:
    """Mirrors the EXACT documented body shape for
    POST /exchange/v1/derivatives/futures/orders/create -- note the
    top-level "order" wrapper object; the docs' own sample nests every
    order field inside body["order"], with only "timestamp" at the top
    level. A flat body (no "order" wrapper) would not match the documented
    contract.

    Per the docs' own explicit NOTE: "Do not include 'time_in_force'
    parameter for market orders" -- this builder omits it (and `price`)
    entirely for order_type == "market", rather than sending an explicit
    null, since the docs distinguish "omit the key" from "send null" for
    other fields (e.g. `price` is documented as "Keep this NULL for market
    orders" -- an explicit null -- while time_in_force's own NOTE says not
    to include the key at all)."""
    if side not in ORDER_SIDES:
        raise ValueError(f"side must be one of {ORDER_SIDES}, got {side!r}")
    if order_type not in ORDER_TYPES:
        raise ValueError(f"order_type must be one of {ORDER_TYPES}, got {order_type!r}")

    order: dict = {
        "side": side,
        "pair": pair,
        "order_type": order_type,
        "price": None if order_type == "market" else price,
        "total_quantity": total_quantity,
        "notification": notification,
        "hidden": False,
        "post_only": False,
        "margin_currency_short_name": margin_currency_short_name,
    }
    if stop_price is not None:
        order["stop_price"] = stop_price
    if leverage is not None:
        order["leverage"] = leverage
    if order_type != "market" and time_in_force is not None:
        if time_in_force not in TIME_IN_FORCE_OPTIONS:
            raise ValueError(f"time_in_force must be one of {TIME_IN_FORCE_OPTIONS}, got {time_in_force!r}")
        order["time_in_force"] = time_in_force
    if position_margin_type is not None:
        if position_margin_type not in POSITION_MARGIN_TYPES:
            raise ValueError(f"position_margin_type must be one of {POSITION_MARGIN_TYPES}, got {position_margin_type!r}")
        order["position_margin_type"] = position_margin_type

    return {"timestamp": timestamp_ms, "order": order}


def build_update_leverage_request(
    timestamp_ms: int, leverage: int, pair: Optional[str] = None, position_id: Optional[str] = None,
    margin_currency_short_name: str = "USDT",
) -> dict:
    """Mirrors POST /exchange/v1/derivatives/futures/positions/update_leverage.
    The docs' own NOTE: use EITHER `pair` OR `id` (position id), not both --
    this builder requires exactly one to be provided, matching that
    constraint at construction time rather than leaving it to the
    (never-called) network layer to discover. `leverage` is documented as
    a STRING in the sample body ("5"), not an integer, despite the table
    describing it only as "leverage value" -- reproduced as a string here
    to match the actual sample, not the vaguer table description."""
    if bool(pair) == bool(position_id):
        raise ValueError("Exactly one of pair or position_id must be provided (CoinDCX's own docs: use either 'pair' or 'id', not both).")
    body: dict = {"timestamp": timestamp_ms, "leverage": str(leverage), "margin_currency_short_name": margin_currency_short_name}
    if pair:
        body["pair"] = pair
    else:
        body["id"] = position_id
    return body


def build_create_tpsl_request(
    timestamp_ms: int, position_id: str, take_profit_stop_price: float, stop_loss_stop_price: float,
) -> dict:
    """Mirrors POST /exchange/v1/derivatives/futures/positions/create_tpsl.

    IMPORTANT DOCUMENTED LIMITATION (not an AlphaOne restriction): the
    docs explicitly state limit_price is "Ignore this for now. This is not
    supported" for both legs, and order_type is fixed to
    take_profit_market / stop_market -- ONLY those two, not the fuller
    take_profit_limit/stop_limit set the order-creation endpoint itself
    supports. This endpoint attaches exactly ONE take-profit and ONE
    stop-loss per position -- the docs' own sample response shows a
    second create_tpsl call being rejected ("TP already exists"). CoinDCX's
    documented futures API therefore has NO native multi-level TP1/TP2/TP3
    mechanism and NO documented partial-quantity exit parameter anywhere
    (positions/exit takes only {timestamp, id} and closes the ENTIRE
    position) -- see docs/coindcx_futures_contract_audit_v2.md Section 8
    for the full analysis. This builder therefore only ever represents a
    single TP + single SL; multi-level TP support would require placing
    additional standalone reduce-side orders manually, which is a genuinely
    open design question this phase does not resolve (see the audit doc's
    remaining-blockers list)."""
    return {
        "timestamp": timestamp_ms,
        "id": position_id,
        "take_profit": {"stop_price": str(take_profit_stop_price), "order_type": TPSL_TAKE_PROFIT_ORDER_TYPE},
        "stop_loss": {"stop_price": str(stop_loss_stop_price), "order_type": TPSL_STOP_LOSS_ORDER_TYPE},
    }


def build_exit_position_request(timestamp_ms: int, position_id: str) -> dict:
    """Mirrors POST /exchange/v1/derivatives/futures/positions/exit --
    closes the ENTIRE position; no partial-quantity parameter exists in
    the documented contract."""
    return {"timestamp": timestamp_ms, "id": position_id}
