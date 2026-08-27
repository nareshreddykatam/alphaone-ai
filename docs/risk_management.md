# Risk management

`services/risk_engine/engine.py`. This document is the source of truth for
how the three risk mechanisms behave and reset -- written after Phase 2.5
found that two of them were incorrectly sharing one permanent lockout flag.

## The three mechanisms

| Mechanism | Trigger | Reset |
|---|---|---|
| **Daily loss limit** | `daily_pnl_pct <= -max_daily_loss_pct` | Automatic, at the next UTC calendar day |
| **Consecutive-loss cooldown** | N losses in a row (`cooldown_consecutive_losses`) | Automatic, after `cooldown_minutes` elapsed (simulated or real) |
| **Max-drawdown hard kill** | `current_drawdown_pct >= max_drawdown_pct` | **Manual only** -- `RiskEngine.reset_hard_kill()` |

These are deliberately independent. A blocked trade's reason string always
names which one fired (`RiskStatus.DAILY_LIMIT`, `RiskStatus.COOLDOWN`, or
`RiskStatus.HARD_KILL`), and `RiskEngine.get_risk_status(now)` returns the
same classification as an enum for programmatic use.

## The Phase 2.5 bug this fixes

Both the daily-loss and max-drawdown branches used to set the same
`state.kill_switch` flag, and nothing ever cleared it once set. A single
bad **day** therefore permanently ended a multi-year backtest after only a
few weeks -- confirmed on all four Phase 2.5 baselines. The fix: the
daily-loss branch no longer touches `kill_switch` at all. It doesn't need
to -- `can_trade()` already blocks on `daily_pnl_pct`, which `reset_daily()`
already correctly zeroes on the next UTC day. Only a genuine max-drawdown
breach arms `kill_switch` now.

## Daily loss limit

- Enforced live in `can_trade()`: `daily_pnl_pct <= -max_daily_loss_pct`.
- `reset_daily(now)` resets `daily_pnl_pct` and `daily_trades` to zero the
  moment `now.date()` differs from the last-seen trade date. "Day" means a
  **UTC calendar day** boundary (`00:00:00 UTC`), not a rolling 24 hours.
- Does **not** reset: `consecutive_losses`, `cooldown_until`, `kill_switch`,
  `peak_equity`/drawdown history, or any lifetime statistic.
- `now` must be the caller's own clock: the simulated candle timestamp
  during a backtest, real UTC time for paper/live trading (see "Clock
  sources" below).

## Consecutive-loss cooldown

- A **loss** (`pnl_pct < 0`) increments `consecutive_losses`.
- A **win** (`pnl_pct > 0`) resets it to zero and clears any pending
  cooldown.
- A **breakeven** trade (`pnl_pct == 0`) is treated as a non-loss and also
  resets the streak, under the default policy
  (`RiskConfig.breakeven_resets_consecutive_losses = True`). Set that flag
  to `False` if a future strategy needs breakeven to leave the streak
  unchanged instead. This is an explicit, configurable, documented choice
  -- not an accident of `if pnl_pct < 0` / `else` branching.
- The moment `consecutive_losses` reaches `cooldown_consecutive_losses`,
  `cooldown_until = now + timedelta(minutes=cooldown_minutes)` is computed
  **once** and stored (`RiskState.cooldown_until`), replacing the Phase 2
  approach of recomputing elapsed time against `last_loss_time` on every
  call. `can_trade()`'s boundary check is exact: `now < cooldown_until` ->
  blocked, `now >= cooldown_until` -> allowed (see
  `tests/unit/test_risk_engine_reset_semantics.py::test_cooldown_boundary_exact_second`).
- Both `cooldown_consecutive_losses` and `cooldown_minutes` are
  `RiskConfig` fields -- configurable per run, never hard-coded.
- A further loss after the cooldown window has already expired re-arms a
  fresh cooldown (consecutive_losses keeps incrementing, so the threshold
  condition is met again) -- this is intentional: repeated losses should
  keep extending caution, not get a free pass after the first cooldown ends.

## Max-drawdown hard kill

- Computed from `peak_equity` vs `current_equity` (both tracked in real
  dollar terms -- see "Equity tracking" below), checked once per trade
  close in `record_trade_result()`.
- The **only** place that sets `state.kill_switch = True` is the
  max-drawdown branch of `record_trade_result()` (plus the manual
  `activate_kill_switch()` for an operator's emergency stop).
- Once set, it survives daily resets, cooldown expiry, and the passage of
  any amount of simulated or real time. The only way to clear it is an
  explicit, auditable call to `RiskEngine.reset_hard_kill()` (which also
  clears `consecutive_losses`/`cooldown_until`, treating a manual reset as
  a deliberate fresh start). `deactivate_kill_switch()` remains as a
  backward-compatible alias for the same method.
- `can_trade()` deliberately does **not** re-derive a block from the raw
  `current_drawdown_pct` percentage as a second, independent check --
  only the `kill_switch` flag gates trading. Re-checking the percentage
  directly would make `reset_hard_kill()` a no-op whenever equity hasn't
  yet recovered above the threshold, defeating the entire purpose of an
  explicit manual override.

## Equity tracking (found and fixed alongside the reset-semantics work)

`RiskEngine.record_trade_result(pnl_pct, now)` expects `pnl_pct` to be the
trade's return **relative to account equity**, not relative to the trade's
own notional. `Backtester.run()` computes this correctly:
`equity_relative_pnl_pct = trade.pnl / equity_before_trade * 100`. Passing
a trade's notional-relative `pnl_pct` (as Phase 2/2.5 did) instead wildly
overstates daily-loss/drawdown tracking for any risk-sized position (the
normal case -- only a 100%-of-equity `position_pct=1.0` trade makes the two
numbers equal), and -- since `calculate_position_size()` reads
`state.current_equity` -- cascades into wrong position sizing for every
subsequent trade. See `tests/unit/test_risk_engine_equity_tracking.py`.

## Clock sources

| Mode | Clock passed as `now` |
|---|---|
| Backtest | The simulated candle timestamp, from `Backtester.run()`'s own loop |
| Paper trading | Real UTC time (the default when `now` is omitted) |
| Live (future, not implemented) | Real UTC time, same as paper |

The risk engine itself never calls `datetime.now()`/`date.today()` to make
a decision -- every method that needs "now" accepts it as a parameter,
defaulting to `datetime.utcnow()` only when the caller doesn't supply one
(paper/live callers correctly rely on this default; the backtester always
passes the simulated timestamp explicitly). This is verified directly in
`tests/unit/test_risk_engine_reset_semantics.py::test_risk_decisions_use_only_the_provided_simulated_timestamp`,
which runs a scenario dated 2023 and confirms the result is identical
regardless of the real machine date.
