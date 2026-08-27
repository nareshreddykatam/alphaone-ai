# Known limitations (Phase 2 + Phase 2.5 + Phase 2.6 + Phase 3 + Phase 4 + Phase 5 + INR-only UI + Live Market Data + Live Breakout Signals + Live Price/Signal audit)

Documented explicitly rather than left to be discovered later.

## Multi-strategy research (v3, robust strategy discovery pass)

A wider search (23 genuinely distinct mechanisms, ~3 years of real BTC/USDT
data, the same train/val/OOS methodology V2 used plus a new cost-robustness
stress test) found: **zero** of 14 fifteen-minute mechanisms tested cleared
even a full-period screen (combined with V2's five, 18 distinct 15m
mechanisms have now been tried with zero survivors -- treat "no 15m edge
under this cost model" as a settled finding, not an open question). Of 9
new 4h mechanisms, two are new PRODUCTION_ELIGIBLE strategies
(V3_KAMA_TREND_4H, OOS PF 1.85; V3_RANGE_EXPANSION_4H, OOS PF 1.42, regime-
dependent -- bear/high-vol only) and one was rejected after full rigor
(V3_HMA_TREND_4H -- OOS PF exactly 1.00 with a negative return). S06 was
independently re-evaluated on the current dataset (not assumed to remain
eligible from V2) and remains PRODUCTION_ELIGIBLE with the same modest,
LONG-carries profile V2 found. S05 was not touched. See
`reports/STRATEGY_RESEARCH_V3_RIGOROUS_REPORT.txt` for full detail.

## Live Price + Live Signal audit (USDT primary / INR secondary, Live Chart)

- **UI reversed from "INR-only" to "USDT primary, INR secondary."** The
  actual CoinDCX trading instrument (BTC/USDT Perpetual Futures) is quoted
  in USDT -- the dashboard's BTC price card, the Live Chart's price axis,
  Signal entry/SL/TP1/TP2/TP3, the Signals table, and Telegram alerts now
  all show the USDT level as the primary value with "≈ ₹..." INR shown
  underneath as the converted representation, never the reverse. Account
  equity/available balance/used margin/unrealized P&L/daily P&L and the
  manual Trade Journal remain INR-only, unchanged -- those are genuinely
  INR-native (the CoinDCX account itself is INR-margined), not a USDT
  price being displayed. `services/common/currency.py: format_usdt` and
  `apps/web/lib/currency.ts: formatUSDT` are the new formatters backing
  this; both return `"N/A"` for `None`/`NaN` rather than inventing a value.
- **Root cause re-confirmed with fresh evidence: `B-BTC_USDT` (not
  `B-BTC_INR`) remains the correct live-price instrument.** A direct real
  call to `https://public.coindcx.com/market_data/v3/current_prices/
  futures/rt` during this audit returned a full live payload for
  `B-BTC_USDT` and `None` for `B-BTC_INR` -- matching an earlier session's
  finding (a real WS connection received 0 ticks for `B-BTC_INR` in 25s
  vs. 80-142 for `B-BTC_USDT`). Switching the live tick to `B-BTC_INR`
  would not just be a cosmetic cross-venue basis difference -- it would
  create a literal currency-unit mismatch against the USDT-denominated
  Binance historical Donchian channel the forming candle is spliced onto.
- **Real bug found and fixed: the Live Chart's `time` field silently
  applied the server's LOCAL timezone instead of UTC.**
  `apps/api/routers/market.py` used `int(dt.timestamp())` on values that
  are always naive UTC (every `DateTime` column in
  `database/schema/models.py` is naive, populated via `datetime.utcnow()`)
  -- but `datetime.timestamp()` on a naive value assumes the *local*
  system timezone. On this dev machine (IST, UTC+5:30, confirmed via
  `time.timezone`), a genuine `08:00:00 UTC` candle bucket was serialized
  as epoch `1787797800`, which decodes to `02:30:00 UTC` -- every bar's
  reported chart position was silently shifted 5.5 hours earlier than
  its real time. Fixed via a new `_epoch_seconds()` helper
  (`calendar.timegm(dt.timetuple())`, which reads the naive value's
  fields as UTC directly with no local-timezone conversion), applied to
  all three call sites (historical candles, the live forming candle,
  signal markers). Regression-tested independent of the test runner's own
  timezone in `tests/unit/test_market_epoch_seconds.py`. This bug predates
  this session's changes (it also affected the pre-existing historical
  `"time"` field) but was only surfaced now because building the forming-
  candle feature required reasoning precisely about bucket alignment.
  Whether this manifests in production depends on the deployed
  container's OS timezone (many default to UTC, in which case it was
  silent there) -- not independently verified for the current Railway
  deployment.
- **A related, NOT-fixed risk found during the same investigation:
  `DataIngestionService.backfill()` (`services/market_data/ingestion.py`)
  does not filter out a still-forming (not yet closed) candle that ccxt/
  Binance can return as the last page of `fetch_ohlcv` when the requested
  window reaches "now."** Observed live: the most recently stored 4h
  `Candle` row and the independently-computed live `forming_candle` for
  the same open-time bucket disagreed (the stored row's `close` was a
  mid-formation snapshot, not the bucket's true final close). This is a
  pre-existing characteristic of the original Phase 1-3 ingestion
  pipeline, not introduced by this session -- backtesting/walk-forward
  research was never exposed to it (research always queries fully-closed
  historical ranges, never `end=now()`), but `signal_generation_job`'s
  15-minute closed-candle evaluation could in principle read such a row
  before its true close. Not fixed here: doing so safely requires
  auditing/adjusting the core candle-ingestion pipeline that the validated
  strategy's data depends on, which is out of scope for a live-price/
  live-chart display task and risks the "do not silently change anything
  that could affect the validated strategy" instruction. The Live Chart's
  own `forming_candle` field is unaffected in practice: the frontend
  (`apps/web/app/chart/page.tsx`) detects when the live forming candle and
  the last stored historical row share the same bucket time and displays
  the live one in that bar's place, rather than erroring (lightweight-
  charts requires strictly ascending, unique bar times) or showing the
  possibly-incomplete stored value as if it were final.

## Live Breakout Signals (intrabar detection, still 4h Donchian+ADX)

- **Real multi-timeframe research found no credible edge below 4h --
  the strategy was deliberately NOT changed.** Walk-forward tested (9
  out-of-sample folds each, real fees/slippage/spread via the existing
  backtester) the existing `trend_following` baseline standalone at
  15m/1h/4h, plus two multi-timeframe variants (15m and 1h breakout each
  gated by a 4h ADX+trend filter). Result: a clean, monotonic degradation
  as trade frequency increased -- 4h (18.6 trades/fold) had the best and
  only roughly-breakeven profile (PF 1.02, 4/9 folds profitable), while
  15m (99.9 trades/fold) was uniformly unprofitable across all 9 folds
  (PF 0.69, 0/9 profitable). The 4h higher-timeframe filter measurably
  helped both lower timeframes (e.g. 1h alone: PF 0.85 -> 1h+4h-filter:
  PF 0.90) but never closed the gap to a real edge. Per the explicit
  instruction to retain 4h rather than invent an unvalidated strategy,
  `services/signal_engine/strategy.py`'s `BaselineStrategy` (Donchian 20 +
  ADX 25, 4h) was not touched at all by this phase -- "more frequent"
  signals come from detecting the SAME breakout intrabar (see below), not
  from a faster, unvalidated strategy.
- **Cross-venue splice, disclosed rather than hidden.** The historical
  closed candles the live evaluation reads are Binance-sourced
  (`services/market_data/binance.py`, unchanged); the live tick spliced
  onto them to build the currently-forming candle
  (`services/signal_engine/live_breakout.py: LiveCandleAggregator`) comes
  from CoinDCX's public B-BTC_USDT WebSocket
  (`services/market_data/live_state.py`'s `market_ws`, reused rather than
  building a second live feed). Both are liquid BTC/USDT perpetual markets
  that track closely, but this is a real simplification: a small,
  transient Binance/CoinDCX price basis could in principle cause the
  live-detected breakout level to differ very slightly from what the
  closed-candle (Binance-only) evaluation would compute once the candle
  actually closes -- which is exactly why the live path is a genuinely
  separate, additional detection (not a replacement for the closed-candle
  `signal_generation_job`), and why both share the same DB-backed
  per-candle dedup rather than trusting only one source.
- **`market_regime` field, Phase 2.6 research construct, unchanged.** The
  live path calls the same `MarketRegimeDetector` the closed-candle path
  already uses -- no new regime logic was added.
- **No frontend change was made.** The `Signal` schema is unchanged (no
  new columns), so the existing Signals page already renders
  live-detected signals correctly through the exact same INR-conversion
  and table-rendering code path; a live-detected signal's `reasoning`
  text is simply annotated with `[LIVE/INTRABAR: ...]`, visible in the
  existing Reasoning column without any UI code change.

## Live Market Data (CoinDCX public WebSocket)

- **Real wire format required a fix the docs didn't warn about.** CoinDCX
  wraps every WebSocket event's payload as a JSON-encoded STRING under
  "data", not an already-decoded object -- see
  docs/coindcx_api_findings.md's "Real wire-format discovery" section.
  Fixed via `_extract_payload()` in `services/market_data/coindcx_ws.py`,
  confirmed against a real connection
  (`scripts/coindcx_market_ws_connectivity_test.py`). Phase 5's
  authenticated account WebSocket (`services/exchange/coindcx_ws.py`) had
  the identical bug -- confirmed via a real, bounded, read-only
  authenticated connection test
  (`scripts/coindcx_account_ws_verification_test.py`: 142/142 real
  `price-change` events crashed before the fix, 0/80 after) -- and has
  since been fixed the same way, as a follow-up in the same session.
- **Deliberately subscribes to B-BTC_USDT, not B-BTC_INR.** The real
  CoinDCX account is INR-margined and trades B-BTC_INR (see the Phase 5
  findings below); this new public client instead tracks B-BTC_USDT --
  a genuinely USDT-denominated market matching AlphaOne's canonical
  "BTC/USDT" symbol and the Binance-sourced strategy's own price terms,
  so it plugs into the existing INR-only UI's USDT->INR conversion
  pipeline (`services/exchange/fx.py`). It never feeds the real
  position's own PnL math -- that stays on its separate, already-correct,
  already-INR-native 30-second REST poll (Phase 5), unchanged.
- **No "index price" exists in CoinDCX's documented futures WebSocket
  API.** `MarketTick.index_price_usdt` is always `None` -- a documented
  absence, not a bug.
- **The Donchian+ADX strategy still runs on Binance-sourced historical
  candles, unchanged.** CoinDCX does provide a documented candlestick
  WebSocket channel (`{instrument}_{resolution}-futures`), but switching
  the strategy to it would mean adopting a new, unvalidated data source
  (different exchange, no leak-free/quality testing done against it) --
  out of scope here per explicit instruction not to rewrite the strategy.
  The new live WebSocket feeds only dashboard display and monitoring
  freshness; signal generation remains entirely candle-completion-driven.
- **Position/exit monitoring already used a live (30s REST-polled) mark
  price before this phase** (Phase 5) -- this phase's "use live price,
  don't wait for the next candle" requirement was already satisfied and
  needed no new code; verified by a test asserting the new public
  WebSocket module is never imported by position/exit-monitoring code.
- **`MARKET_DATA_WS_ENABLED` defaults to `false`.** The live feed must be
  explicitly enabled, matching the `SCHEDULER_ENABLED`/`TELEGRAM_ENABLED`
  pattern; with it disabled (or before the first real tick arrives), the
  dashboard falls back to the pre-existing Binance-candle price path
  exactly as before this phase.

## INR-only UI (currency conversion findings)

- **Conversion source is CoinDCX's own USDT/INR spot ticker, not a
  USDT->USD->INR chain.** The spec's suggested architecture included a
  "USDT/USD reference" hop; that hop was deliberately dropped
  (`services/exchange/fx.py`) because there is no real, documented data
  source for it -- USDT is already a USD-pegged stablecoin, and CoinDCX's
  `USDTINR` market (`https://public.coindcx.com/exchange/ticker`) is a
  real, directly-tradeable market, making it a more defensible single-hop
  conversion than a fabricated intermediate USD reference would be.
- **Only the Binance-sourced signal-engine prices are converted.** The
  real CoinDCX account is INR-margined (`DEFAULT_MARGIN_CURRENCY = "INR"`,
  see the Phase 5 findings above), so account balance, open-position
  prices/PnL, and trade-journal entries are already native INR and are
  displayed as-is -- never run through the USDT conversion. Only
  `btc_price` (from ingested Binance BTC/USDT candles), `Signal`
  entry/SL/TP levels, and chart OHLC values are USDT-denominated and
  therefore converted.
- **Manually-entered trades are assumed to be INR-denominated.** A trade
  logged via the Trade Journal has no currency field of its own; since the
  user manually trades their INR-margined CoinDCX account, manual entries
  are treated as INR and displayed without conversion. If AlphaOne is ever
  used against a differently-margined account, this assumption would need
  revisiting.
- **Chart INR values use a single current conversion rate applied
  uniformly across the whole candle series**, not a historical daily
  USDT/INR rate per candle -- CoinDCX does not expose a documented
  historical USDT/INR rate history. This means older candles' INR values
  are only as accurate as today's rate, which is disclosed in the chart
  page's subtitle (conversion source + status) rather than hidden.
- **The Risk page's "Current Equity" is the RiskEngine's own internal
  notional equity tracker** (a Phase 2.6 research construct, currently
  defaulted to 10,000 and never fed real CoinDCX balances), not a real
  money amount. It is still displayed with the ₹ formatter for UI
  consistency, since it is a unitless internal notional figure rather than
  a second real currency needing conversion.

## Phase 5 findings

- **No real CoinDCX credentials were available this session.** Every
  endpoint in `services/exchange/coindcx.py` and the sync/WebSocket layers
  built on it (`services/exchange/coindcx_sync.py`,
  `services/exchange/coindcx_ws.py`) were verified against the exact
  documented request/response schemas (`docs/coindcx_api_findings.md`)
  using mocked HTTP/transport layers -- never a live account. Before
  relying on this in production, follow the Phase 5 spec's own section 51
  ("Real Account Acceptance Test"): connect once with real read-only
  credentials and manually verify the wallet/position/trade responses
  actually match what this code expects, since CoinDCX's real production
  responses could differ from the documented examples in ways docs alone
  can't catch.
- **No API-key permission scoping is documented by CoinDCX.** There is no
  way to generate a CoinDCX key that is read-only at the exchange's own
  level -- any key capable of the reads AlphaOne makes is technically also
  capable of the mutating endpoints AlphaOne never calls. The safety
  boundary here is entirely in AlphaOne's own code (enforced by
  `tests/unit/test_no_order_placement_capability.py`), not the exchange.
  A leaked CoinDCX key is exactly as dangerous as any other API key.
- **The WebSocket client's live connection was never exercised against a
  real server.** `CoinDCXWebSocketClient`'s message-parsing and freshness
  logic (`handle_price_change`, `handle_position_update`,
  `handle_balance_update`, `market_data_state`, `account_data_state`) are
  fully unit-tested by calling them directly with synthetic payloads
  shaped exactly like CoinDCX's documented examples
  (`tests/unit/test_coindcx_ws.py`), but `connect()`/reconnect behavior
  under a real flaky network was not exercised -- python-socketio's own
  built-in reconnection (configured, not reimplemented) is relied on here.
- **The scheduler (`services/scheduler/`) is disabled by default**
  (`SCHEDULER_ENABLED=false`) and was never run continuously against a
  live CoinDCX account in this session -- each job tick
  (`run_once_account_sync`, etc.) is unit-tested individually with a fake
  provider and a real circuit breaker, and the full detect-track-alert-close
  pipeline is proven end-to-end in
  `tests/integration/test_phase5_final_acceptance.py`, but the actual
  30-second/15-minute intervals have not been observed running for real.
- **Position "opened_at" is approximated as sync time, not the real
  exchange fill time.** CoinDCX's position object has no "position opened"
  timestamp, only `updated_at` (last-modified). `_create_trade_from_position`
  uses the moment AlphaOne first detects the position as `entry_time` --
  for signal-matching purposes this is usually within the same sync cycle
  as the real fill, but it is not the exchange's own fill timestamp.
- **Closing a disappeared position uses volume-weighted-average of the
  real trade fills found in the closing window, not necessarily an exact
  1:1 fill match.** If CoinDCX processed the close as multiple partial
  fills, `_close_disappeared_position` sums them into a single closing
  execution at their VWAP price rather than modeling each partial fill as
  its own `TradeExecution` row. Real money math (VWAP, total quantity) is
  correct; the execution-level audit trail is coarser than the exchange's
  own.
- **`sync_trade_fills`'s raw-fill ingestion only attaches to the most
  recently-opened Trade for a symbol**, since CoinDCX documents no fill-id
  ->position-id linkage. This is adequate for the common case (one open
  position per symbol, matching this platform's single-position risk
  model) but would misattribute fills if multiple concurrent positions on
  the same symbol ever existed.
- **The ambiguous signal-match confirmation UI is now built** (Phase 4
  left this as a gap; Phase 5's trades page adds a "Needs confirmation"
  control that fetches and displays `POSSIBLE SIGNAL MATCHES` via the
  existing `GET /journal/{trade_id}/match-candidates` endpoint) -- this
  closes the Phase 4 known-limitation entry about it.
- **Two real bugs found only by manual browser verification, both fixed
  same-session:** (1) `apps/api/routers/dashboard.py` trusted the stored
  `Account.connection_status` value directly to decide `NOT_CONFIGURED` vs
  `DISCONNECTED`, which showed "Disconnected" for a pre-Phase-5 account
  row whose stored status predated the new vocabulary -- fixed by checking
  `CoinDCXReadOnlyAccountProvider.is_configured` directly rather than
  trusting a possibly-stale stored field. (2) `apps/api/routers/trades.py`
  and `journal.py`'s `_serialize()` omitted `match_status` and every live
  sync field entirely, so the new Trades-page "Match" column silently
  rendered "--" for every trade regardless of its real match state --
  fixed by adding the missing fields, with a regression test
  (`test_trade_serialization_includes_phase5_match_and_sync_fields`) to
  catch a future recurrence.

## Phase 4 findings

- **SunCrypto has no authenticated account API.** Confirmed by fetching
  both `docs.suncrypto.in` and `help.suncrypto.in`: the only documented API
  surface is 3 unauthenticated **public spot** endpoints (`GET
  /public/pairs`, `GET /public/tickers`, `GET /public/historical_trades`).
  No futures API, no order endpoints, no authenticated account access, no
  documented API-key permission granularity anywhere. Per the Phase 4
  spec's own section 5, this means AlphaOne implements **manual trade
  tracking**, not a live read-only account sync --
  `services/exchange/suncrypto.py: SunCryptoReadOnlyAccountProvider` is an
  honest stub whose every method reports `UNAVAILABLE`. If SunCrypto ever
  publishes a real authenticated read-only API, that is the one class to
  replace; `services/exchange/sync.py` already proves the sync
  scaffolding (audit trail, staleness check) works correctly against the
  `ExchangeAccountProvider` interface once a real provider exists.
- **No live price/tick feed.** The dashboard's BTC price and the position
  monitor's exit-alert checks both read the latest already-ingested candle
  from the research database, not a live market feed -- there is no
  scheduled ingestion job running continuously in Phase 4 (running
  `scripts/download_data.py` periodically, or building a proper scheduler,
  is future work). `btc_price_source` on the dashboard honestly reports
  `STALE`/`UNAVAILABLE` rather than implying a live price.
- **No unrealized (mark-to-market) P&L for open manual positions.** Without
  a live price feed, `GET /dashboard/`'s `unrealized_pnl` is always `null`
  ("N/A" in the UI) rather than computed against a stale price and
  presented as current.
- **No live scheduler generates signals or checks exit alerts
  automatically.** `POST /signals/generate`, `POST
  /signals/evaluate-outcomes`, and `GET /journal/exit-alerts` are all
  on-demand endpoints in this phase, not driven by a background job. The
  Telegram `/pause` and `/resume` commands toggle a real flag
  (`services/signal_engine/live_signal.py: is_signal_generation_paused`)
  that a future scheduler would need to honor before generating a signal --
  wiring an actual periodic scheduler (cron, APScheduler, or a persistent
  worker process) is explicitly out of scope for this phase but the state
  it would need to check already exists and is tested.
- **Telegram bot was built and tested against mocks only, per the user's
  explicit choice** (not a real bot token). Every command handler
  (`services/telegram/bot.py`) is tested with a mocked `Update`/`Bot`
  against a real in-memory DB (`tests/unit/test_telegram_bot.py`). Going
  live requires only setting `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`/
  `TELEGRAM_ENABLED=true` in `.env` and running
  `python -m services.telegram.bot` (or wiring `TelegramBot().build_app().run_polling()`
  into a persistent process) -- no code changes needed.
- **Signal-to-trade match confirmation has no dedicated frontend UI.**
  The backend contract is real and tested (`services/signal_matching/`,
  `GET/POST /journal/{trade_id}/match-candidates` and `/confirm-match`) --
  a manual trade auto-links to a confident single-candidate signal and
  returns `match_candidates` for ambiguous cases, but the trades page
  (`apps/web/app/trades/page.tsx`) does not yet render a picker for the
  ambiguous case. Low-severity: ambiguous matches are rare (two signals
  within the same narrow time+price window) and the trade still saves
  correctly with `signal_id=null`.
- **No dedicated audit-log table.** The spec calls for "a full audit log of
  key lifecycle events." Phase 4 reuses `SyncEvent` (sync attempts) and
  `NotificationLog` (Telegram sends, exit-alert dedup) rather than adding a
  fourth overlapping table -- together they cover the events that actually
  occur in this phase (sync attempts, notifications, exit-alert dedup).
  Trade/signal lifecycle changes themselves are inherently audited via
  their own `created_at`/`entry_time`/`exit_time`/`TradeExecution` rows.
- **No production deployment was performed.** `docs/deployment.md` records
  guidance (Vercel frontend, a persistent non-serverless backend, Postgres,
  Redis, and the exact env vars that must never be `NEXT_PUBLIC_*`) but
  nothing was actually deployed -- this remains a local-dev SQLite setup.

## Phase 3 findings

- **No ML model tested demonstrated a genuine, robust out-of-sample edge.**
  Logistic Regression, Random Forest, XGBoost, and LightGBM were screened
  across three feature-group ablations (A/B/C) on a single chronological
  split; 11 of 12 combinations were net-negative. The one positive result
  (feature set B + LightGBM, +2.58% net return, PF 1.18) did **not**
  replicate under 19-fold walk-forward validation (only 6/19 folds
  profitable, mean -0.95% per fold) and lost 85% of its single-split
  return under a 2x cost-stress test. This is the expected signature of a
  single lucky split among many screened combinations (a multiple-
  comparisons/data-snooping artifact), not a real edge -- see
  `docs/ml_methodology.md` and the Phase 3 report for full detail.
- **Ablation D (+ derivatives) could not be evaluated** -- including the
  Open Interest features drops all but the trailing ~30 days of the
  3-year dataset to non-null (Binance's OI history limit, unchanged since
  Phase 2), leaving too few rows after the train/val/test split for any
  model. This is a data-availability constraint, not a bug -- extending
  ablation D would require either a much shorter overall study window or
  training on OI-present rows only.
- **`ModelTrainer.train_ensemble` (Phase 1/2 scaffolding) cannot currently
  train when LightGBM is one of its base models**: `VotingClassifier`
  re-fits internal clones of each estimator without an eval set, and
  LightGBM's early-stopping callback requires one
  (`ValueError: Must have at least 1 validation dataset for early stopping`).
  Not fixed in Phase 3 -- the ensemble is the last, most-complex rung of
  the model ladder, and none of the four working models showed enough
  promise to justify debugging it this phase.

## Fixed in Phase 2.6 (see docs/risk_management.md for full detail)

- **The Phase 2.5 permanent-lockout bug is fixed.** The daily-loss limit
  and max-drawdown hard kill no longer share one flag. Daily loss now
  auto-resets at the next UTC day; the hard kill requires an explicit
  `reset_hard_kill()` call and nothing else clears it. Re-running the
  continuous 3-year baselines confirmed trades now continue across most or
  all of the 3-year window (4h: all three non-buy-and-hold baselines
  traded from Sep 2023 through Aug 2026, zero hard-kill events; 1h: all
  three traded 6-14 months before a **genuine** ~10% drawdown correctly
  and permanently halted them -- a real risk-limit breach this time, not a
  false trigger).
- **Found alongside the reset-semantics fix:** `RiskEngine`'s equity/
  drawdown tracking was using each trade's return-on-its-own-notional as
  if it were return-on-account-equity, which only happens to be correct
  for a 100%-of-equity (`position_pct=1.0`) trade. For normal risk-sized
  trades this overstated the risk engine's internal drawdown far beyond
  the real equity curve, and skewed every subsequent trade's position
  size (since sizing reads `state.current_equity`). Fixed in
  `Backtester.run()`: the risk engine is now given the pnl converted to a
  percentage of equity *before* that trade, not percentage of the trade's
  own notional.
- A related gap: the end-of-data forced position close (when a position is
  still open at the last candle) never updated the risk engine's state or
  the final equity-curve point. Fixed for consistency, though it has zero
  effect on any trading decision (the backtest has already ended by then).

## Fixed in Phase 2.5 (see docs/execution_semantics.md for detail)

- Gap-through-stop now fills at the true gapped-through price, not the
  stale stop level.
- Funding now uses real historical per-event rates (point-in-time correct,
  correct LONG/SHORT sign convention) wherever a funding dataset is
  supplied, falling back to the old flat-average estimate only when it isn't.

## Data

- **Open interest history is capped at ~30 days** by Binance's public API,
  regardless of how far back a backfill requests. A 365-day dataset will
  only ever have a ~30-day tail of OI data. Derivative features that
  depend on OI (`oi_change`, `price_oi_corr`, etc.) are only meaningful
  over that recent window.
- **No historical liquidation backfill exists.** Binance provides no public
  endpoint for historical liquidation events; only a recent/live snapshot
  is obtainable. Liquidation-based features (`long_liquidations`,
  `liq_spike`) should be treated as recent-only, not as a dense feature
  over the full backtest period.
- **Single exchange (Binance).** The `ExchangeBase` abstraction supports
  adding another exchange without touching the feature engine or
  backtester, but no second exchange is implemented in Phase 2.
- Candle timestamps are stored as **naive UTC** (converted explicitly from
  exchange epoch-ms, see `services/market_data/binance.py: _ms_to_utc_naive`)
  -- this fixed a real bug where the original code used
  `datetime.fromtimestamp()` without a timezone, which silently used the
  local machine's timezone instead of UTC.

## Backtesting

- **Simultaneous SL/TP touch within one candle is resolved by assuming the
  stop-loss triggers first** (a conservative choice, not something OHLC
  data can actually disambiguate).
- **Take-profit does not get the same gap-aware fill treatment as
  stop-loss** (see Phase 2.5 fix above) -- a take-profit always fills at
  its own level plus slippage even if the market gapped past it. This is
  a smaller-impact asymmetry than the stop case (a limit order filling at
  its requested price or better is the realistic assumption when price
  gaps beyond it, so the "error" here favors realism less but doesn't
  overstate performance the way the old stop bug did).
- **No partial exits or TP2/TP3 modeling** in the backtester, even though
  the DB schema and signal engine carry three take-profit levels. Only a
  single take-profit is simulated (`BacktestTrade.take_profit`). This is a
  scope limitation carried over from Phase 1, not fixed in Phase 2.5.
- Exchange parameters (fees, slippage, tick size, min quantity, max
  leverage) are labeled research assumptions -- see
  `docs/exchange_assumptions.md` -- not pulled from a live account's actual
  fee tier.

## Minor rough edges (not fixed, low impact)

- `garman_klass_vol` (`services/feature_engine/volatility.py`) applies the
  Garman-Klass formula to a single bar rather than as a rolling average
  over several bars (its usual form), which can go negative under the
  square root for some bars and silently produce `NaN` (handled correctly
  downstream via `notna` masks, so this does not affect correctness of any
  signal, but the column will have more `NaN`s than a properly-averaged GK
  estimator would).

## ML / walk-forward

- No hyperparameter tuning was performed. Per the Phase 2 brief, the goal
  is a trustworthy, leak-free foundation -- not a tuned or optimized model.
- No scaler exists in the current pipeline (tree-based models don't need
  one); `ml/features/scaling.py` exists as tested infrastructure for if
  one is added later, not because one is used today.

## Not built in Phase 2 (by design)

- No live trading, no order placement, no exchange credentials beyond
  public market-data access.
- The FastAPI routers and Next.js dashboard remain stubs; wiring them to
  real data is out of scope for this phase.
- No standing/production ingestion scheduler -- `scripts/download_data.py`
  is run manually or via an external scheduler of the operator's choosing.

## Multi-strategy research (v2, rigorous pass) -- fresh S05 finding

A stricter re-research of the whole strategy registry (chronological
train/validation/OOS split, parameters frozen on VALIDATION before ever
touching OOS -- see `scripts/research_v2_rigorous.py` and
`reports/STRATEGY_RESEARCH_V2_RIGOROUS_REPORT.txt` for full, real,
unedited output) re-examined the EXISTING, protected S05 (Donchian+ADX)
baseline for a fair comparison against every new candidate. On the
~7-month out-of-sample window this pass used, S05 did not clear a
"robust OOS edge" bar either: profit factor 0.82, only 1 of 4 OOS
walk-forward folds profitable, and a max drawdown near the unlucky end
of its own trade-order bootstrap distribution.

This is **not** a new problem discovered in S05 -- it is consistent with,
and reinforces, the disclaimer every S05 signal has always carried
("never confirmed as a robust, cost-surviving edge... treat as a
research heuristic, not a guaranteed outcome," `services/signal_engine/
strategy.py`). S05's implementation and production status were
**deliberately not changed** by this finding, per the explicit
instruction protecting it from being silently replaced or retuned
because a stricter test (or another candidate) looked different. Anyone
considering a future change to S05 should read the full section in
`reports/STRATEGY_RESEARCH_V2_RIGOROUS_REPORT.txt` first.

Of the 9 new/replacement candidates examined in that same pass, only
S06 (Supertrend + ATR, 4h) cleared the project's evidence bar --
modestly, not decisively (OOS PF 1.10, 2 of 4 walk-forward folds
profitable, low absolute drawdown, LONG-side-carries-the-result
asymmetry disclosed). All five 15m candidates tested (including one new
replacement, Z-Score Mean Reversion) failed decisively -- zero out of
four OOS walk-forward folds profitable, across every one of them. As of
this pass, AlphaOne has no defensible 15m production strategy.
