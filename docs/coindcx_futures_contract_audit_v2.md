# CoinDCX Futures Contract Audit V2 (Live Futures Auto-Trading V1)

Fetched directly from the official docs at https://docs.coindcx.com/ on
2026-08-28 (full page downloaded and parsed locally, not summarized by an
intermediate tool, after an initial summarization pass truncated before
reaching the Futures section). Cross-checked against REAL live GET
requests to `api.coindcx.com` and `public.coindcx.com` the same day for
`B-BTC_USDT`, `B-ETH_USDT`, `B-SOL_USDT`, `B-XRP_USDT` (all genuinely
public endpoints, no credentials, no order placed). This document is the
authoritative source `services/live_execution/order_contract.py`,
`services/live_execution/sizing.py`, `services/live_execution/
timestamp_safety.py`, and `services/exchange/coindcx_instruments.py` were
built from.

## 1. Order creation -- `POST /exchange/v1/derivatives/futures/orders/create`

The request body is **nested**: only `timestamp` is top-level; every
order field lives inside a `"order": {...}` object. A flat body does not
match the documented contract.

```
{
  "timestamp": <epoch ms>,
  "order": {
    "side": "buy" | "sell",
    "pair": "B-ETH_USDT",
    "order_type": "market" | "limit" | "stop_limit" | "stop_market" | "take_profit_limit" | "take_profit_market",
    "price": <number, NULL for market orders>,
    "stop_price": <number, for stop/take-profit variants>,
    "total_quantity": <number>,
    "leverage": <number, OPTIONAL -- must equal the position's existing leverage or the order is rejected>,
    "notification": "no_notification" | "email_notification",
    "time_in_force": "good_till_cancel" | "fill_or_kill" | "immediate_or_cancel"  (OMIT entirely for market orders -- explicit docs NOTE),
    "hidden": false,       // documented as "Ignore this (Not supported at the moment)"
    "post_only": false,    // documented as "Ignore this (Not supported at the moment)"
    "margin_currency_short_name": "USDT" | "INR"  (OPTIONAL, default "USDT"),
    "position_margin_type": "isolated" | "crossed"  (OPTIONAL)
  }
}
```

**Documented discrepancy noted, not silently resolved**: the endpoint's
own official Python/Node code SAMPLE uses `"order_type": "market_order"`,
while the SAME page's "Request Definitions" table lists valid values as
`market, limit, stop_limit, stop_market, take_profit_limit,
take_profit_market` (no `_order` suffix). `order_contract.py`'s
`ORDER_TYPES` uses the table's values (`"market"`), treating the
parameter table as more authoritative than a possibly-copy-pasted-from-
spot-API code sample, per the same judgment call already documented in
`docs/coindcx_api_findings.md` for the timestamp-units discrepancy.

**No client-order-ID / idempotency field exists anywhere in the
documented request** -- confirms AlphaOne's own DB-level
`LiveExecution.idempotency_key` unique constraint (Phase 10-11) is the
*only* duplicate-order protection available; the exchange itself offers
none.

**Leverage tiering**: a real error code, `"Max allowed leverage for
current position size = 5x"`, confirms max usable leverage is tiered by
position notional size, separate from the instrument's flat
`max_leverage_long`/`max_leverage_short` cap. AlphaOne's tiny Rs.200
positions are far below every tier boundary observed in the real
`dynamic_position_leverage_details` responses fetched below, so this
tiering is not currently a practical blocker -- but it is a real
exchange-side rule this system has never modeled, and a future increase
in position size would need to account for it.

## 2. Leverage -- `POST /exchange/v1/derivatives/futures/positions/update_leverage`

```
{
  "timestamp": <epoch ms>,
  "leverage": "10",              // STRING in the real sample, not a number
  "pair": "B-LTC_USDT",          // XOR "id" (position id) -- use exactly one, never both
  "margin_currency_short_name": "USDT"   // documented as YES/mandatory here (unlike the order-create endpoint, where it's optional)
}
```
Response: `{"message": "success", "status": 200, "code": 200}` -- no
leverage value or position data echoed back.

Real documented error: `"Max allowed leverage for current position size
= 5x"` -- confirms leverage limits are dynamic/tiered, not just the flat
instrument cap.

## 3. TP/SL -- `POST /exchange/v1/derivatives/futures/positions/create_tpsl`

```
{
  "timestamp": <epoch ms>,
  "id": "<position id>",          // REQUIRES an existing position -- cannot be attached at order-creation time
  "take_profit": {"stop_price": "1", "order_type": "take_profit_market"},   // ONLY take_profit_market is supported -- limit_price is documented "Ignore this for now. Not supported"
  "stop_loss":   {"stop_price": "0.271", "order_type": "stop_market"}       // ONLY stop_market is supported
}
```

**Critical, genuinely important finding**: the real documented sample
response shows a SECOND call to this endpoint being rejected --
`"take_profit": {"success": false, "error": "TP already exists"}` -- this
endpoint attaches exactly **ONE** take-profit and **ONE** stop-loss per
position, ever. **CoinDCX's documented futures API has no native
multi-level TP1/TP2/TP3 mechanism.**

## 4. Exit / partial close -- `POST /exchange/v1/derivatives/futures/positions/exit`

```
{"timestamp": <epoch ms>, "id": "<position id>"}
```
That is the ENTIRE documented request -- no quantity field. This endpoint
closes the WHOLE position. **CoinDCX's documented futures API has no
partial-exit / reduce-only quantity parameter anywhere** -- confirmed by
also checking every other futures endpoint on the docs page for a
`reduce_only` or equivalent flag; none exists.

**Consequence for Sections 18/24 (TP1/TP2/TP3, partial exits)**: given (3)
and (4) together, achieving a genuine TP1/TP2/TP3 partial-exit ladder on
real CoinDCX futures would require placing separate, manually-managed
opposite-side orders at each level and tracking the remaining quantity
entirely in AlphaOne's own state -- there is no reduce-only flag to make
this safe against accidentally flipping or over-closing a position. This
is a real, unresolved design gap, listed as a remaining blocker, not
something this phase invents an answer for.

## 5. Instrument metadata -- `GET /exchange/v1/derivatives/futures/data/instrument`

Public, unauthenticated (confirmed: the docs' own code sample sends no
`X-AUTH-*` headers). Real, live response for `B-BTC_USDT` (2026-08-28):

```json
{"instrument": {
  "pair": "B-BTC_USDT", "status": "active", "kind": "perpetual",
  "max_leverage_long": 20.0, "max_leverage_short": 20.0,
  "price_increment": 0.1, "quantity_increment": 0.001, "min_trade_size": 0.001,
  "min_price": 584.64, "max_price": 791341.0, "min_quantity": 0.001, "max_quantity": 950.0,
  "min_notional": 60.0, "exit_only": false, "margin_currency_short_name": "USDT",
  "order_types": ["limit_order","market_order","stop_limit","take_profit_limit","stop_market","take_profit_market"]
}}
```

**This directly resolves the prior phase's open question** ("no
per-instrument quantity-step/min-quantity/min-notional/price-precision
data is captured anywhere") -- that data genuinely IS available via this
real, documented, public endpoint; it simply had never been fetched by
this codebase before. `services/exchange/coindcx_instruments.py` now
implements it.

## 6. USDT/INR conversion -- `POST /api/v1/derivatives/futures/data/conversions`

**Different base path** (`/api/v1/`, not `/exchange/v1/` like every other
endpoint) -- easy to get wrong by pattern-matching the others.
Authenticated (signed, despite the docs' own sample code oddly using
`requests.get(url, data=json_body, headers=headers)` — a GET call with a
body — while the page's "HTTP Request" line says `POST`; implemented here
as `POST`, matching the explicit HTTP Request line and this provider's
existing `_post` convention, and documented as a discrepancy rather than
silently picked).

```json
[{"symbol": "USDTINR", "margin_currency_short_name": "INR",
  "target_currency_short_name": "USDT", "conversion_price": 89.0,
  "last_updated_at": 1728460492399}]
```

**Important architectural correction this audit makes**: the existing
`services/exchange/fx.py` module's own docstring explicitly says it is
"for display purposes only -- never for account math", using CoinDCX's
public USDT/INR SPOT ticker. This conversion-price endpoint is
CoinDCX's own INTERNAL futures-margin conversion rate (the account's real
futures wallet is INR-margined -- see `docs/coindcx_api_findings.md`) and
is the correct source for real-money margin sizing. Implemented as
`CoinDCXReadOnlyAccountProvider.get_futures_conversion_rate()` --
authenticated, read-only, distinct from `fx.py`.

Even using this authoritative rate immediately before sizing, exact
Rs.200 margin is only ever *approximate*: there is an inherent TOCTOU gap
between AlphaOne fetching the rate and CoinDCX applying its own
(possibly-updated) rate at real order-execution time, on top of quantity
rounding. `services/live_execution/sizing.py`'s
`MAX_MARGIN_DEVIATION_PCT` (10%) is the explicit, documented tolerance
this system uses instead of claiming false precision.

## 7. Real multi-coin feasibility snapshot (2026-08-28, live data)

Real mark prices and real USDT/INR rate (99.89) fetched live the same
day, run through `services/live_execution/sizing.py`'s actual sizing
logic (see `tests/unit/test_live_execution_sizing.py` for the executable
proof):

| Symbol | Real max leverage | Real min_notional | Real min_quantity | Rs.200/10x feasible today? |
|---|---|---|---|---|
| BTC/USDT | 20x | $60 | 0.001 | **NO** -- 0.001 BTC alone implies ~Rs.790 margin at 10x, ~4x over target |
| ETH/USDT | 20x | $24 | 0.001 | **NO** -- $24 min_notional alone implies far more than Rs.200 margin |
| SOL/USDT | **5x** | $6 | 0.01 | **NO** -- max leverage is 5x; the required 10x is not supported at all |
| XRP/USDT | 10x | $6 | 0.1 | **YES** -- realized margin lands within ~1% of Rs.200 (quantity_increment is coarse relative to price, keeping rounding error small) |

**This is a genuinely important, previously-undiscovered finding**: of
the four coins the task names, only XRP/USDT can currently represent the
user's exact Rs.200/10x rule within a reasonable tolerance on real
CoinDCX futures. BTC and ETH are blocked by their own min_notional/
min_quantity floors (not a leverage problem); SOL is blocked by its
leverage cap (not a sizing problem). Prices, leverage caps, and
min_notional values can and do change over time -- this table is a dated
snapshot, and `sizing.py`'s logic (not this table) is what a real
candidate would actually be evaluated against.

## 8. Order-request timestamp window

Documented explicitly: *"Orders with a delay of more than 10 seconds will
be rejected."* `services/live_execution/timestamp_safety.py` implements a
tighter internal safety margin (7s) rather than relying on the exchange's
own 10s cutoff as a target to approach.

## 9. Remaining genuinely open questions (not resolved by this audit)

- No documented reduce-only/partial-exit mechanism (Section 4 above) --
  TP1/TP2/TP3 and partial position management would need a manually-
  tracked design this audit does not invent.
- `dynamic_position_leverage_details`/`dynamic_safety_margin_details`
  (the tiered leverage-vs-position-size and margin-vs-position-size
  tables in the real instrument response) are captured in
  `InstrumentMetadata`'s raw source but not yet modeled/enforced anywhere
  -- irrelevant at Rs.200 position sizes today, but a real gap if position
  sizing ever changes.
- The order-creation code sample's own inconsistency (`market_order` vs
  the table's `market`) means the TRUE accepted value is still not
  100% certain without a real (never-to-be-attempted-in-this-phase) test
  order -- `order_contract.py` documents the choice made and why.
