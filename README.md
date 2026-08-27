# AlphaOne AI

A manual-execution BTC/USDT perpetual futures monitoring, alerting, and
trade-journaling platform for CoinDCX. AlphaOne watches the market and
your CoinDCX account and tells you what it sees — it never places,
cancels, modifies, or closes a trade on your behalf.

## What this is

- **Read-only CoinDCX account monitoring**: real balance, open positions,
  and trade history via CoinDCX's REST API and a persistent WebSocket
  connection to CoinDCX's authenticated account channel.
- **Live public market data**: a separate WebSocket connection to
  CoinDCX's public BTC/USDT futures feed, with automatic reconnect,
  exponential backoff on startup, and honest LIVE/STALE/DISCONNECTED/
  UNAVAILABLE freshness states — never silently shown as live when it
  isn't.
- **A rule-based research signal** (Donchian channel breakout + ADX
  trend-strength filter) evaluated against completed historical candles,
  shown as a categorical LOW/MEDIUM/HIGH quality indicator, never a
  fabricated accuracy percentage.
- **Manual trade journal**: you log your own entries/exits (or they're
  detected from your real CoinDCX positions); AlphaOne matches them
  against its own signals for after-the-fact comparison.
- **Three separate, never-blended performance views**: historical
  backtest results, "what every signal would have earned had you taken
  it," and your own actual logged trades.
- **Telegram alerts**: new signals, exit conditions on an open position,
  and market-data connection state changes — event-driven, deduplicated,
  never a message per tick or per poll.
- **INR-only dashboard**: every monetary value is displayed in INR, with
  a real, disclosed CoinDCX USDT/INR conversion rate (and an honest "INR
  conversion unavailable" state rather than a guessed number) for the
  USDT-denominated pieces of the pipeline.
- **A backtesting/research foundation** (Phases 2-3 of this project) that
  tested several baseline and ML approaches against real historical data
  and found **no baseline or ML model demonstrated a robust,
  cost-surviving out-of-sample edge**. The rule-based signal shown today
  is a research heuristic, reported as such — not a validated trading
  strategy.

## What this is not

- **Not an automatic trading bot.** No code path anywhere in this
  repository can place, cancel, modify, or close an order, or change
  leverage or margin, on any exchange. This is enforced structurally
  (every exchange-facing class exposes only `get_*` read methods) and
  verified by an automated test
  (`tests/unit/test_no_order_placement_capability.py`) that fails loudly
  if that ever stops being true.
- **Not a source of guaranteed, accurate, or profitable signals.** The
  research behind the current signal found no demonstrated edge (see
  above). Signal quality is a categorical label, never a precision
  percentage or a promise.
- **Not production-hardened against real capital at scale.** This is a
  personal/student project: real CoinDCX and Telegram connectivity have
  been verified against a real account and a real bot, but it has not
  been operated unattended in production over an extended period.

## Architecture

```
CoinDCX (public WS)  ──▶  live market-data state  ──┐
CoinDCX (account WS + REST) ──▶ position/balance sync ──┼──▶ Dashboard (Next.js, INR-only)
Binance-sourced historical candles ──▶ signal engine ───┤
                                                          └──▶ Telegram alerts (event-driven)

Backend: FastAPI process running the scheduler, both WebSocket
clients, and the Telegram outbound path together in one persistent
service (never split across serverless functions).
```

## Technology stack

- **Backend**: Python, FastAPI, SQLAlchemy (async), Alembic, PostgreSQL
  in production (SQLite for local development)
- **Real-time**: `python-socketio` (CoinDCX WebSocket channels),
  `python-telegram-bot`
- **Frontend**: Next.js 14 (App Router), TypeScript, Tailwind CSS
- **Research/backtesting**: pandas, scikit-learn, XGBoost, LightGBM (used
  during the Phase 2-3 research described above; no model is deployed to
  the live signal path today)

## Quick start (local development)

### Prerequisites

- Python 3.11+
- Node.js 18+
- PostgreSQL (or use the bundled SQLite default for local dev)

### Setup

```bash
# Backend
pip install -e ".[dev]"
cp .env.example .env        # fill in your own values; never commit .env
alembic upgrade head
uvicorn apps.api.main:app --reload --port 8000

# Frontend (separate terminal)
cd apps/web
npm install
npm run dev
```

Open http://localhost:3000.

By default, the background scheduler, the live market-data WebSocket, and
Telegram are all **disabled** (`SCHEDULER_ENABLED=false`,
`MARKET_DATA_WS_ENABLED=false`, `TELEGRAM_ENABLED=false`) — enable each
explicitly once you've configured real credentials and want live
monitoring.

### Tests

```bash
pytest tests/ -v                 # backend
cd apps/web && npm test          # frontend
cd apps/web && npm run type-check
```

## Deployment

- `infrastructure/Dockerfile.api` builds the backend as a single
  persistent container (FastAPI + scheduler + both WebSocket clients +
  Telegram, all in one process — this cannot run as a serverless
  function without losing the persistent connections).
- `railway.json` configures Railway to build from that Dockerfile
  explicitly and start it bound to Railway's assigned port.
- The frontend deploys separately (e.g. Vercel) and talks to the backend
  over `NEXT_PUBLIC_API_URL`.
- See `docs/deployment.md` for the full environment-variable reference
  and a step-by-step deployment runbook.

## Project structure

```
apps/
├── web/                 # Next.js dashboard (INR-only)
└── api/                 # FastAPI app, routers, config
services/
├── exchange/            # CoinDCX REST + authenticated WebSocket (read-only)
├── market_data/         # Public live market-data WebSocket, Binance historical ingestion
├── signal_engine/       # Rule-based research signal + notification
├── risk_engine/         # Informational risk-status tracking
├── scheduler/           # Background account-sync/signal/exit-alert loops
├── trade_journal/       # Manual trade logging + P&L
├── signal_matching/     # Matches logged trades to signals
├── portfolio/           # Three-way performance views
└── telegram/            # Outbound alerts + read-only bot commands
database/
├── migrations/          # Alembic
└── schema/              # SQLAlchemy models
ml/                      # Phase 2-3 research: features, training, evaluation
docs/                    # Architecture notes, API research, known limitations
tests/
infrastructure/          # Dockerfile
```

## Security

- CoinDCX and Telegram credentials are read from server-side environment
  variables only — never sent to the frontend, never logged, never
  returned in an API response.
- No exchange-facing code path can place, cancel, or modify an order, or
  change leverage/margin — verified by an automated test, not just a
  convention.
- See `docs/known_limitations.md` for an explicit, running record of
  every real limitation and edge case found during development.

## License

MIT
