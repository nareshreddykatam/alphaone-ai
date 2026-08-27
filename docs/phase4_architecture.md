# Phase 4 architecture: manual trading intelligence + tracking

Source of truth for the Phase 4 product architecture. See
`docs/known_limitations.md` for what's NOT built yet, and
`docs/ml_methodology.md`/`docs/risk_management.md` for the Phase 3/2.6
systems this phase reuses rather than replaces.

## Product shift

Phases 1-3 built a research pipeline to evaluate whether AlphaOne could
find a trading edge. It could not (Phase 3's conclusion, unchanged).
Phase 4 does not change that research finding -- it changes the product:
AlphaOne is decision support + trade tracking + analytics. The user
executes every trade manually on SunCrypto. **AlphaOne cannot place,
cancel, or modify an order** -- this is enforced architecturally, not just
by policy: no class in `services/exchange/` has a callable that could do
so (`tests/unit/test_no_order_placement_capability.py`).

## The three-way performance separation

The single most load-bearing design rule in this phase
(`services/portfolio/service.py`):

| View | Source | Meaning |
|---|---|---|
| Backtest | `BacktestRun`/`BacktestMetric` (Phase 2/3) | What a strategy would have earned historically, in simulation |
| AlphaOne Signal Performance | `SignalOutcome` | What following every generated signal, hypothetically, would have earned -- whether or not the user took it |
| User Actual Performance | `Trade` (`is_manual_entry=True`) | What the user actually earned executing manually |

`GET /api/v1/portfolio/performance` returns all three as separate keys.
No code path anywhere sums or blends them into one number.

## Exchange integration

`services/exchange/base.py` defines two exchange-agnostic interfaces:
- `ExchangeMarketDataProvider` -- public market data (no auth needed).
- `ExchangeAccountProvider` -- read-only account access ONLY. Every
  method name must start with `get_` (enforced by
  `test_no_order_placement_capability.py`'s whitelist check, alongside a
  blacklist of order/leverage/margin-mutation terms).

`services/exchange/suncrypto.py` implements both: `SunCryptoMarketDataProvider`
makes real calls to SunCrypto's 3 documented public endpoints;
`SunCryptoReadOnlyAccountProvider` is an honest stub (every method reports
`UNAVAILABLE`) because no authenticated account API exists (see
`docs/known_limitations.md`). `services/exchange/sync.py` is the
audit-trailed sync scaffold that would activate a real provider without
any other code changing.

## Signal generation -- pluggable, never overclaiming

`services/signal_engine/strategy.py` defines `SignalStrategy` with three
implementations:
- `BaselineStrategy` -- the real, working Donchian+ADX rule-based signal
  (Phase 2.6's strongest, still-unverified baseline). Quality
  (`LOW`/`MEDIUM`/`HIGH`) is derived from the real ADX margin above its
  firing threshold, never a fabricated probability.
- `MLStrategy` -- wraps a Phase 3 calibrated model. Not wired into any
  default live endpoint, because Phase 3 found no model with a validated
  edge; exists as a ready slot.
- `FutureStrategy` -- raises `NotImplementedError`; a placeholder, not a
  silent fallback.

`services/signal_engine/live_signal.py` generates and persists a signal
on demand (`POST /api/v1/signals/generate`) from the latest real candles,
respecting a pause flag toggled by the Telegram `/pause`/`/resume`
commands. `services/signal_engine/outcome_evaluator.py` later resolves a
`PENDING` `SignalOutcome` to `WIN`/`LOSS`/`EXPIRED` against real
subsequent candles (`POST /api/v1/signals/evaluate-outcomes`), using the
same stop-wins-ties convention as the backtester.

## Manual trade tracking

`services/trade_journal/` is the write path: `open_trade` /
`record_exit` (supports partial exits via `TradeExecution` rows,
`Trade.status` moves OPEN -> PARTIALLY_CLOSED -> CLOSED) / `cancel_trade`
(only before any exit is recorded) / `set_signal_match`. PnL/fee/
R-multiple math lives in `services/trade_journal/pnl.py`, shared with (and
extracted from) `services/paper_trader/engine.py` so the two never drift.

`services/signal_matching/matcher.py` scores candidate signals by
symbol+direction+time-proximity+price-proximity; a confident single match
auto-links on trade open, an ambiguous tie is returned as
`match_candidates` for the API caller to surface for manual confirmation
(`POST /journal/{trade_id}/confirm-match`) -- never auto-picked.

`services/position_monitor/monitor.py` recommends (never executes) an
exit when an open position's own stop/target would have been hit, using
the latest real candle price; alerts dedup via `NotificationLog` so a
standing breach isn't re-alerted forever.

## Risk dashboard

Reuses the Phase 2.6 `RiskEngine`/`RiskStatus` verbatim --
`services/risk_engine/state_store.py` persists its state to `BotState` so
it survives across stateless API requests. In Phase 4 it is purely
informational (nothing it decides can block a manual trade); it is fed
the equity-relative PnL of each closed manual trade
(`apps/api/routers/journal.py`'s exit handler) so `GET /api/v1/risk/`
reflects real trading history, exactly like the backtester feeds it in
Phase 2.6.

## Telegram bot

`services/telegram/bot.py` -- every inbound command reads real DB state
through the same services the API uses (never a second source of truth).
Outbound alerts (`send_signal`, `send_exit`, `send_exit_alert`,
`send_kill_switch`) are all no-ops unless `TELEGRAM_ENABLED=true` and a
real token/chat ID are configured. See `docs/known_limitations.md` for
what "tested against mocks only" means operationally.
