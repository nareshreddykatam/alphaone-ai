# Execution semantics

This document is the single source of truth for when a signal becomes an
actionable trade in AlphaOne's backtester, baselines, and walk-forward
validation. Every strategy in this codebase (baselines, the ML-driven
signal engine, walk-forward folds) shares this assumption.

## The rule

A strategy decides its signal using data through candle **T**'s close:
`signal_func(df.iloc[:T+1])` is allowed to see candle T's own open, high,
low, close, and volume.

That decision is **not** filled at candle T's close. It becomes eligible
for execution at candle **T+1**'s open, adjusted for slippage:

```
entry_price = T+1.open * (1 ± slippage_rate)
```

This is deliberate and is the fix for a critical bug found in the Phase 1
audit: the original backtester filled entries at the exact close of the
signal candle, which is unrealistic look-ahead execution -- no real order
can be filled at a price that only becomes known the instant the candle
closes. See `services/backtester/engine.py: Backtester.run` and
`tests/leakage/test_execution_timing.py`.

**Edge case: signal on the last bar of a dataset.** There is no T+1 to fill
at, so the signal is simply never executed. This is intentional, not a bug.

**Position management (stop-loss / take-profit) is different**: once a
position is open, its stop-loss/take-profit are checked against every
subsequent candle's own high/low as that candle is processed -- there is no
similar "wait one more bar" rule for exits, because a resting stop/limit
order sits on the book continuously and can be hit intrabar on the very
candle after entry.

## Simultaneous SL/TP touch

If a single candle's high/low range would satisfy both the stop-loss and
the take-profit, raw OHLC data cannot tell us which was touched first
intrabar. `Backtester._manage_position` resolves this by always checking
the stop-loss first -- i.e. assuming the worse outcome. This is a
conservative simplification, documented here rather than hidden.

## Gap-through-stop (fixed in Phase 2.5)

**Prior behavior (Phase 2, now fixed):** if a candle gapped through the
stop-loss level, the backtester still filled at `stop_loss` itself (plus
slippage) -- a price the market never actually offered once it had gapped
past it. This understated losses on genuine gap events.

**Current behavior:** `Backtester._resolve_stop_fill` compares the bar's own
**open** against the stop level before filling:
- If the open is already beyond the stop (a gap), the stop is a market
  order that can only fill at the worse available price -- it fills at
  the **open**, not the stale stop level.
- If the open is still on the favorable side of the stop, the stop fills
  normally at its own level (the market reached it intrabar via `low`/`high`,
  which is the standard resting-stop-order assumption).

```
LONG,  SL=98,  next candle opens at 94  -> exit ≈ 94  (not 98)
SHORT, SL=102, next candle opens at 106 -> exit ≈ 106 (not 102)
```

Slippage is still applied on top of whichever reference price is used. See
`services/backtester/engine.py: _resolve_stop_fill` and
`tests/unit/test_gap_through_stop.py` / `tests/unit/test_backtest_reconciliation.py`
for hand-verified examples of both directions.

**Still a known simplification:** take-profit fills do not get the
equivalent "fill at the better available price" treatment if the market
gaps *past* a limit-style target -- `take_profit` always fills at its own
level plus slippage. A gap-beyond-target scenario is comparatively rare
(a limit order fills at the requested price or better, so the realistic
"error" here is in the trader's favor, unlike the stop case) and was not
in scope for the Phase 2.5 fix; see `docs/known_limitations.md`.

## Funding (real historical data, Phase 2.5)

**Prior behavior (Phase 2):** funding was a flat-average estimate --
`funding_rate_avg` charged once per elapsed funding interval over the
holding period, regardless of what funding rates actually did historically.

**Current behavior:** when a `funding_rates` DataFrame (real historical
rates, timestamp + rate) is passed into `Backtester.run(...)`, funding is
charged **event-by-event** using the actual rate at each real funding
timestamp the position was open for:

```
Position open
    -> for each funding timestamp T where entry_time < T <= current bar
        -> look up the REAL rate recorded at T (never a future rate)
        -> notional = quantity * mark_price (bar's own open at the time T is processed)
        -> LONG:  cost = notional * rate   (positive rate = long pays)
        -> SHORT: cost = -notional * rate  (positive rate = short receives)
        -> accumulate into trade.funding
```

Sign convention matches real perpetual-futures mechanics: a positive rate
means longs pay shorts; a negative rate means shorts pay longs.
`trade.funding` accumulates in **cost** terms (positive = paid out, negative
= net credit received), and `pnl -= trade.funding` at close nets it
correctly in both directions.

Boundary rules (all point-in-time correct -- no future rate is ever used):
- A position filled **exactly at** a funding timestamp is not charged for
  that event (`entry_time < T`, strict).
- A position already closed before a funding timestamp is not charged.
- A position spanning several funding events is charged for each one.

When no real funding data is supplied (e.g. a synthetic dataset in a test),
`Backtester.run` falls back to the old flat-average estimate so existing
callers keep working. See `tests/unit/test_funding.py` for the full set of
hand-calculated sign-convention and boundary tests.

## Risk-engine gating (see docs/risk_management.md for full detail)

Before a pending order fills, `Backtester.run()` calls
`self.risk_engine.can_trade(now=timestamp)` using the **simulated candle
timestamp**, never the real system clock. Every risk decision -- daily
loss limit, consecutive-loss cooldown, max-drawdown hard kill -- is a pure
function of this `now` plus the risk engine's own state, so a backtest
dated 2023 behaves identically regardless of the real machine date. See
`docs/risk_management.md` for how the three mechanisms differ (two
auto-reset, one requires an explicit manual reset) -- this was the subject
of a critical Phase 2.5 finding and Phase 2.6 fix.

When a trade closes, `record_trade_result()` is given the trade's pnl
**relative to account equity** (not relative to the trade's own notional)
-- see `docs/risk_management.md`'s "Equity tracking" section.

## Multiple signals while a position is open

The backtester is single-position (`max_positions` gates this via the risk
engine). A new signal that fires while a position is already open is
evaluated by `signal_func` but never queued -- if the position is still
open by the time the pending order would be filled, the earlier position's
exit is processed first (same bar), and a fresh signal decided on that same
bar can then queue for the *following* bar. No signal is ever silently
dropped without a reason; it is simply not actionable while a position
exists.
