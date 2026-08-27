# Exchange assumptions (research defaults)

`services/backtester/exchange_spec.py: ExchangeSpec` holds every
exchange-specific number the backtester needs. **These are research
assumptions, not values pulled from a live Binance fee-tier or margin API.**
They are configurable precisely so they can be replaced with real,
account-specific values later without touching the backtester's logic.

| Field | Default | What it approximates |
|---|---|---|
| `maker_fee` | 0.02% | Binance USDT-M futures VIP0 maker fee (approximate, not tier-specific) |
| `taker_fee` | 0.04% | Binance USDT-M futures VIP0 taker fee. The backtester assumes all entries/exits are taker fills. |
| `funding_interval_hours` | 8 | Binance perpetual futures funding interval |
| `slippage_bps` | 1.0 (0.01%) | A flat slippage assumption applied to every fill, long or short |
| `spread_bps` | 0.5 (0.005%) | Defined for future use; not currently applied separately from slippage |
| `tick_size` | 0.1 | Approximate BTC/USDT perp tick size (verify against live exchangeInfo before any real-money use) |
| `qty_precision` | 3 | Approximate quantity decimal precision |
| `min_qty` | 0.001 | Approximate minimum order size |
| `max_leverage` | 5 | A conservative research cap, unrelated to the exchange's actual maximum (up to 125x on BTC/USDT) |
| `maintenance_margin_pct` | 0.5% | Placeholder; not yet used in any liquidation-distance calculation |

`BacktestConfig.funding_rate_avg` (default 0.0001 per interval) is now only
a **fallback** used when no real historical funding series is supplied to
`Backtester.run(...)`. As of Phase 2.5, real historical funding (ingested
into the `funding_rates` table via
`services/market_data/ingestion.py: backfill_funding_rates`) is used by
default in every script that runs a backtest (`scripts/run_backtest.py`,
`scripts/run_baselines.py`) -- see `docs/execution_semantics.md` for the
event-by-event calculation and sign convention. The flat-average path still
exists purely for callers/tests that don't have real funding data on hand.

## Why this matters

Every number in this file directly changes backtest profitability. A
strategy that looks profitable at these assumed costs and unprofitable at
real Binance costs (or vice versa) is telling you the strategy's edge is
thin relative to trading friction, not that the assumptions were wrong.
Before trusting a specific number, check it against the live exchange's
`exchangeInfo` / fee-tier endpoint for the account that would actually
trade.
