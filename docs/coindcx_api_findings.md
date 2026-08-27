# CoinDCX API research findings (Phase 5)

Fetched directly from the official docs at https://docs.coindcx.com/ on
2026-08-26 (a single long-scroll "Slate" documentation page -- the
Futures End Points section alone is ~85,000 characters of endpoint
definitions and code samples). This is the authoritative source used for
every capability decision in `services/exchange/coindcx.py` -- no
tutorial, blog post, or third-party GitHub repo was used to decide what
the API can or cannot do (per the Phase 5 brief). A few endpoint paths
were cross-checked against web search results pointing at the same
official domain (`api.coindcx.com`) where the docs page content was
ambiguous.

## Symbol format

Futures instruments are named `B-{BASE}_{MARGIN}`, e.g. `B-BTC_USDT`.
This is NOT Binance's `BTC/USDT` format used elsewhere in AlphaOne --
`services/exchange/coindcx.py: normalize_symbol()` converts.

## Authentication

- Headers: `X-AUTH-APIKEY` (raw key), `X-AUTH-SIGNATURE` (hex digest).
- Signature: `HMAC-SHA256(exact_json_body_string, api_secret).hexdigest()`.
  The signature must be computed over the **exact byte string** sent as
  the request body -- re-serializing the dict differently (key order,
  whitespace) produces a different signature and CoinDCX will reject it.
- Every authenticated request includes a `timestamp` field in the JSON
  body. The docs' prose says "EPOCH timestamp in seconds" in some places,
  but every single official code sample uses `int(time.time() * 1000)`
  (milliseconds) -- the code samples were trusted over the possibly-stale
  prose.
- No documented way to scope an API key as read-only vs trading-enabled.
  This means a CoinDCX API key capable of the read calls AlphaOne makes
  is *also* capable of the mutating calls AlphaOne deliberately never
  calls -- the safety boundary here is enforced entirely in AlphaOne's own
  code (no `create_order`/`cancel_order`/etc. method exists anywhere,
  verified by `tests/unit/test_no_order_placement_capability.py`), not by
  the exchange. Document this clearly for the user: a leaked CoinDCX key
  would be as dangerous as any other, regardless of what AlphaOne itself
  does with it.

## Public market data (no auth)

| Endpoint | Purpose |
|---|---|
| `GET /exchange/v1/derivatives/futures/data/active_instruments?margin_currency_short_name[]=USDT` | List active futures instruments |
| `GET /exchange/v1/derivatives/futures/data/instrument?pair=...&margin_currency_short_name=...` | Instrument details |
| `GET /exchange/v1/derivatives/futures/data/trades?pair=...` | Recent trades for an instrument |
| `GET https://public.coindcx.com/market_data/v3/current_prices/futures/rt` | Real-time current/mark prices for ALL futures pairs in one call |
| `GET https://public.coindcx.com/market_data/candlesticks?pair=...&from=...&to=...&resolution=1|5|60|1D&pcode=f` | REST candles (CoinDCX's own docs recommend the WebSocket instead for anything time-sensitive) |
| `GET /api/v1/derivatives/futures/data/stats?pair=...` | Pair stats |

## Authenticated read endpoints (implemented in `CoinDCXReadOnlyAccountProvider`)

| Endpoint | Purpose | Response fields of note |
|---|---|---|
| `GET /exchange/v1/derivatives/futures/wallets` | Futures wallet balance | `balance` (**"Ignore this" per CoinDCX's own docs**), `locked_balance`, `cross_order_margin`, `cross_user_margin`. AlphaOne computes equity/available/used-margin from the latter three, never `balance`. |
| `GET /exchange/v1/derivatives/futures/wallets/transactions` | Wallet credit/debit ledger | `transaction_type`, `amount`, `reason` (`by_universal_wallet` / `by_futures_order` / `by_futures_funding`) |
| `POST /exchange/v1/derivatives/futures/positions` | List open positions (by page, or by `pairs`/`position_ids`) | `active_pos` (signed quantity; negative = short), `avg_price` (entry), `mark_price`, `liquidation_price`, `leverage`, `locked_margin`, `margin_type`. **No unrealized-PnL field** -- AlphaOne computes it as `active_pos * (mark_price - avg_price)`. |
| `POST /exchange/v1/derivatives/futures/positions/transactions` | Position-level realized PnL/funding transactions, filterable by `stage` (`default`/`funding`/`exit`/`tpsl_exit`/`liquidation`) | `amount` (PnL for that transaction), `fee_amount`, `stage` |
| `POST /exchange/v1/derivatives/futures/trades` | Trade fill history for a pair (mandatory `from_date`/`to_date`, format YYYY-MM-DD) | `price`, `quantity`, `side`, `fee_amount`, `order_id`, `timestamp`. **No documented unique trade-fill id** -- idempotent sync derives one from `order_id+timestamp+price+quantity+side`. |
| `POST /exchange/v1/derivatives/futures/orders` | List orders by status/side (side is mandatory, single-valued -- must query "buy" and "sell" separately for a full list) | `id`, `status`, `avg_price`, `remaining_quantity` |

## WebSocket API (`wss://stream.coindcx.com`, Socket.IO protocol)

Requires the `python-socketio` client (NOT a raw websocket client --
CoinDCX's WS layer is Socket.IO-framed). Join the `"coindcx"` channel with
an HMAC signature over `{"channel":"coindcx"}` plus `apiKey` for
authenticated account streams; market-data channels need no auth.

| Channel pattern | Event name | Data |
|---|---|---|
| `coindcx` (authenticated) | `df-position-update` | Same shape as the REST positions response |
| `coindcx` (authenticated) | `df-order-update` | Same shape as the REST orders response |
| `coindcx` (authenticated) | `balance-update` | `balance`, `locked_balance`, `currency_short_name` |
| `[instrument]_{resolution}-futures`, e.g. `B-BTC_USDT_1h-futures` | `candlestick` | OHLCV bar; resolutions: 1m/5m/15m/30m/1h/4h/8h/1d/3d/1w/1M (richer than the REST endpoint's 1/5/60/1D) |
| `[instrument]@orderbook@{depth}-futures` (depth: 10/20/50) | `depth-snapshot` | bids/asks |
| `[instrument]@trades-futures` | `new-trade` | Single trade tick |
| `[instrument]@prices-futures` | `price-change` | Last-traded-price tick |
| `currentPrices@futures@rt` | `currentPrices@futures#update` | Mark price for all pairs |

CoinDCX's sample code sends a `ping` event every 25 seconds --
`services/exchange/coindcx_ws.py` follows this as the heartbeat interval.

### Real wire-format discovery (Live Market Data phase, 2026-08-26)

The docs' own response samples above show only each event's INNER payload
shape. Against the real live socket
(`scripts/coindcx_market_ws_connectivity_test.py`), every event actually
arrives as `{"event": "<name>", "data": "<JSON-encoded STRING>"}` -- i.e.
`data` is a JSON string that must be parsed again, not an already-decoded
object. This was not discoverable from the docs' samples alone and first
surfaced as a live crash
(`AttributeError: 'str' object has no attribute 'get'`) on the very first
real connectivity test run; fixed in
`services/market_data/coindcx_ws.py` via `_extract_payload()`, which
`json.loads()`s the string (while still accepting an already-parsed dict,
for robustness). Confirmed real samples:

```
price-change:  {"event":"price-change","data":"{\"T\":1787763803239,\"p\":\"77979\",\"pr\":\"f\"}"}
currentPrices: {"event":"currentPrices@futures#update","data":"{\"vs\":355251680,\"ts\":...,\"prices\":{...}}"}
```

**Confirmed and fixed in `services/exchange/coindcx_ws.py` too (2026-08-26
follow-up).** That file's AUTHENTICATED account channel
(`df-position-update`, `df-order-update`, `balance-update`) used the
identical `response.get("data", response)` pattern and had only ever been
exercised against mocks. A real, bounded, read-only verification test
(`scripts/coindcx_account_ws_verification_test.py`, joining the real
`"coindcx"` channel with real credentials, no order-placement anywhere)
confirmed the exact same crash: 142/142 real `price-change` events raised
`AttributeError: 'str' object has no attribute 'get'`
(`df-position-update`/`balance-update` never fired during the test window
since the real account had zero position/balance activity -- but the
docs' own response shape for those events is a JSON ARRAY, wrapped the
same string-encoded way, so the fix covers all three). Fixed via a
mirrored `_extract_payload()` in that file (a separate copy, not a shared
import, keeping the two WebSocket modules decoupled as designed) that
returns whatever `json.loads()` decodes to (dict or list, since the inner
shape differs by event). Re-ran the same real verification test after the
fix: 80/80 real `price-change` events processed cleanly, `market_state.price`
updated to a real value (78216.9), zero crashes.

## Mutating endpoints that exist -- documented here so AlphaOne explicitly
## never implements them (verified by `test_no_order_placement_capability.py`)

`POST /exchange/v1/derivatives/futures/orders/create`,
`.../orders/cancel`, `.../orders/edit`,
`.../positions/update_leverage`, `.../positions/add_margin`,
`.../positions/remove_margin`, `.../positions/cancel_all_open_orders`,
`.../positions/cancel_all_open_orders_for_position`,
`.../positions/exit`, `.../positions/create_tpsl`,
`.../positions/margin_type`, `.../wallets/transfer`.

## Rate limits

Only spot-market rate limits are documented explicitly (e.g. 2000
requests/60s for most order-status/order-creation spot endpoints); no
futures-specific rate-limit table was found on the docs page. AlphaOne's
sync/circuit-breaker logic (`services/scheduler/`) therefore treats HTTP
429 generically (exponential backoff) rather than hard-coding a specific
requests-per-minute budget that isn't actually documented for futures.

## Error codes

400 (bad request), 401 (bad API key), 404, 429 (rate limited), 500, 503.
