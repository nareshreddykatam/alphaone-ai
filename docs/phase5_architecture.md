# Phase 5 architecture: CoinDCX read-only integration + live monitoring

Source of truth for the Phase 5 additions. See `docs/phase4_architecture.md`
for the manual-trading platform this extends, `docs/coindcx_api_findings.md`
for the exact CoinDCX API research this is built against, and
`docs/known_limitations.md` for what's not yet exercised against a real
account/connection.

## Exchange swap, not a rebuild

The Phase 4 exchange-agnostic interfaces (`services/exchange/base.py`)
were designed for exactly this: a new exchange plugs in without touching
anything else. `SunCryptoMarketDataProvider`/`SunCryptoReadOnlyAccountProvider`
are kept, unmodified, as historical/reference code (SunCrypto genuinely
has no authenticated API, so there was nothing to migrate); CoinDCX is now
the active exchange everywhere a default is needed
(`services/portfolio/account.py`, migration 004's one-time data fix of
existing `accounts.exchange` rows).

## CoinDCX provider (`services/exchange/coindcx.py`)

- `CoinDCXMarketDataProvider` -- real calls to CoinDCX's public futures
  endpoints (instruments, real-time prices, historical trades, REST
  candles). `normalize_symbol`/`denormalize_symbol` convert between
  AlphaOne's `BTC/USDT` and CoinDCX's `B-BTC_USDT` instrument format.
- `CoinDCXReadOnlyAccountProvider` -- implements the shared
  `ExchangeAccountProvider` interface (`get_balance`, `get_open_positions`,
  `get_trade_history`, `get_connection_status`) plus CoinDCX-specific
  extras (`get_transactions`, `get_orders`), all HMAC-signed per CoinDCX's
  documented scheme. Every method reports `NOT_CONFIGURED`/`UNAVAILABLE`/
  `AUTH_FAILURE`/`CONNECTION_LOST` rather than raising when credentials
  are absent or a call fails -- callers (routers, sync jobs, the Telegram
  bot) never need a try/except around it.
- Computed, not exchange-reported: `unrealized_pnl` (CoinDCX's position
  object has no such field) and `total_equity`/`used_margin` (CoinDCX's
  own docs say to ignore the wallet's `balance` field; the real number
  comes from `locked_balance + cross_order_margin + cross_user_margin`).

## Sync (`services/exchange/coindcx_sync.py`)

- `sync_balance` -- pulls wallet balance, records an `AccountSnapshot`,
  updates `Account.connection_status`/`last_synced_at`, logs a `SyncEvent`.
- `sync_positions` -- diffs live CoinDCX positions against known open
  `Trade` rows (`source=COINDCX_SYNC`): new positions are matched to
  signals via the Phase 4 matcher (confident match/ambiguous/unmatched,
  see `SignalMatchStatus`), existing ones get their live mark
  price/unrealized PnL/margin refreshed, and positions that disappeared
  are closed using the real closing fill(s) from `get_trade_history`
  (volume-weighted average price) via the same `record_exit` Phase 4
  already uses for manual exits -- never a guessed close.
- `sync_trade_fills` -- idempotently ingests raw fills as `TradeExecution`
  audit rows, keyed by a deterministic `exchange_transaction_id` derived
  from `order_id+timestamp+price+quantity+side` (CoinDCX documents no
  single unique fill id).

## Live data (`services/exchange/coindcx_ws.py`)

Socket.IO client (CoinDCX's WS layer is Socket.IO-framed, not raw
websockets) against `wss://stream.coindcx.com`. Message handlers
(`handle_price_change`, `handle_position_update`, `handle_balance_update`)
are plain, synchronous, and independently unit-tested with synthetic
payloads -- the socket.io event adapters just extract `.data` and delegate
to them. `market_data_state()`/`account_data_state()` compute
LIVE/STALE/DISCONNECTED/UNAVAILABLE/NOT_CONFIGURED from connection state +
last-update recency, feeding the frontend's freshness indicators (section
36). Reconnection is python-socketio's own built-in support, not
reimplemented.

## Scheduler (`services/scheduler/`)

- `circuit_breaker.py` -- CLOSED/OPEN/HALF_OPEN state machine, clock-
  injectable like the Phase 2.6 RiskEngine's `now` parameter, so it's
  fully testable without real sleeps. Opens after `failure_threshold`
  consecutive failures, backs off exponentially (capped), allows one
  trial call once the window elapses.
- `jobs.py` -- plain async functions (`account_sync_job`,
  `exit_alert_job`, `signal_generation_job`, `outcome_evaluation_job`)
  independently testable with a fake provider.
- `runner.py` -- `SchedulerRunner` wraps each job in its own circuit
  breaker and its own `while True: tick(); sleep()` loop, started from
  `apps/api/main.py`'s lifespan only when `SCHEDULER_ENABLED=true` (never
  during tests, never in a serverless deployment).

## Position detection & signal matching

Reuses `services/signal_matching/matcher.py` verbatim -- a new CoinDCX
position calls `find_candidate_signals`/`pick_confident_match` exactly
like a manually-entered trade did in Phase 4. `Trade.match_status`
(`AUTO_MATCHED`/`AMBIGUOUS`/`UNMATCHED`/`MANUAL`/`CONFIRMED`) makes the
state explicit. The trades page now renders a "Needs confirmation"
control for `AMBIGUOUS` trades that fetches candidates from the existing
`GET /journal/{trade_id}/match-candidates` endpoint and confirms via the
existing `POST /journal/{trade_id}/confirm-match` -- both endpoints
already existed from Phase 4, only the frontend picker was missing.

## Dashboard & Telegram

`GET /api/v1/dashboard/` now returns `exchange: "COINDCX"`,
`account_data_source` (computed from whether credentials are configured,
not trusted blindly from a possibly-stale stored value -- see
`docs/known_limitations.md` for the bug this fixed), `open_positions`
(count of live `Trade` rows with `source=COINDCX_SYNC`), and
`unrealized_pnl` summed from those same rows' periodically-synced
`unrealized_pnl` field (read from the DB, not a live API call on every
dashboard poll). The Telegram bot's `/account` command and
`send_position_detected`/`send_exit_alert` templates were updated to
CoinDCX branding and real balance/position numbers.
