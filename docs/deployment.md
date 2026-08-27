# Deployment guidance (Phase 4 -- Production Deployment Preparation)

Nothing in this document has been deployed -- this repo has only ever run
against local SQLite (dev) in every environment used so far, plus one
real, local, throwaway PostgreSQL connectivity check during the
production-readiness audit (see "PostgreSQL" below). This is guidance for
standing up a real deployment, not a record of one.

## Topology

- **Frontend (`apps/web`)**: Vercel. Next.js App Router, no server-side
  secrets live here -- the only env var it needs is `NEXT_PUBLIC_API_URL`.
- **Backend (`apps/api`)**: a **persistent, non-serverless** process (a
  small VM, Railway, Render, Fly.io, or a container on any host that keeps
  a process running). Serverless is a poor fit because AlphaOne needs, all
  running inside the same long-lived process (`apps/api/main.py`'s
  `lifespan`):
  - the background scheduler (`services/scheduler/runner.py`, gated by
    `SCHEDULER_ENABLED=true`) -- account sync, exit-alert checks, signal
    generation, and signal-outcome evaluation, all periodic `asyncio`
    loops, not separate cron invocations;
  - the live CoinDCX public market-data WebSocket
    (`services/market_data/coindcx_ws.py` + `live_state.py`, gated by
    `MARKET_DATA_WS_ENABLED=true`), including its startup-retry
    supervisor;
  - a Telegram bot, if `run_polling()` is ever wired up as its own
    process/thread (currently outbound-alert-only via `TelegramBot._send`,
    called from the scheduler and signal-notification paths -- see
    `services/telegram/bot.py`'s `if __name__ == "__main__"` block for the
    inbound-command polling entrypoint, not currently started by
    `apps/api/main.py`).
  Running any of the above inside a serverless function would kill the
  loops/connection the moment the function instance is recycled.
- **Database**: PostgreSQL in production. `database/schema/types.py`'s
  `GUID` type already supports both SQLite and Postgres transparently, and
  every migration's partial unique indexes
  (`exchange_trade_id`/`exchange_transaction_id`, migration 004) are
  written with both `sqlite_where` and `postgresql_where` -- no schema
  changes are needed to switch. Point `DATABASE_URL` / `DATABASE_URL_SYNC`
  at the Postgres instance and run `alembic upgrade head`.
- **Redis**: not required for the feature set built so far to function
  correctly -- `apps/api/deps.py: get_redis` exists as a dependency stub
  for future use (e.g. higher-volume Telegram dedup/rate-limiting). Do not
  provision it just because `docker-compose.yml` includes it for local
  dev convenience.

## Environment variables -- server-side only

See `PRODUCTION_DEPLOYMENT_READINESS_REPORT.txt` (in `reports/`) for the
full variable-by-variable matrix (purpose, required/optional,
frontend/backend, secret/non-secret). Summary: every variable except
`NEXT_PUBLIC_API_URL` must be set as a server-side environment variable on
the backend process only, **never** as `NEXT_PUBLIC_*` (which Next.js
inlines into client-side JavaScript, exposing it to every visitor's
browser).

**CoinDCX key handling**: CoinDCX documents no way to scope an API key as
read-only at the exchange's own level (see
`docs/coindcx_api_findings.md`) -- any key capable of the reads AlphaOne
makes is technically also capable of order placement, even though no code
path in this repo ever calls those endpoints
(`tests/unit/test_no_order_placement_capability.py` enforces this at the
code level, and now also covers the market-data WebSocket and its startup
retry supervisor). Treat a CoinDCX key exactly as sensitively as a
trading-enabled key: server-side env var only, never logged
(`tests/unit/test_coindcx_no_credential_logging.py` verifies this), never
returned in an API response (including `/health` and `/ready` -- see
below), never passed to the frontend or into a Telegram message.

## CORS

`apps/api/config.py`'s `api_cors_origins` (env var `API_CORS_ORIGINS`)
defaults to `http://localhost:3000` -- **never** `*` in this codebase.
Before deploying, set it to the real Vercel production URL (and any
preview-deployment domain pattern you want to allow), comma-separated,
e.g. `API_CORS_ORIGINS=https://alphaone.vercel.app`. `main.py` splits on
commas via the `cors_origins` property -- no code change needed, only the
env var.

## Health checks

Two endpoints already exist, with a deliberate liveness/readiness split:

- `GET /health` -- liveness only. No dependency checks, cheap and fast,
  safe to call very frequently. Point an orchestrator's
  restart-on-failure probe here. Returns `{"status": "ok", "service":
  "alphaone", "version": "0.1.0"}` -- no secrets, ever.
- `GET /ready` (and the equivalent `GET /api/v1/health/`) -- checks real
  DB connectivity (`SELECT 1`) and CoinDCX reachability
  (`CoinDCXReadOnlyAccountProvider.get_connection_status()`), returning
  only coarse status strings (`"ok"` / `"error"` / `"NOT_CONFIGURED"` /
  etc.) via `apps/api/routers/health.py: compute_readiness` -- never a
  credential, connection string, or account balance. Point a load
  balancer's traffic-routing probe here; a `"degraded"` response should
  stop new traffic without necessarily restarting the process.

## Before deploying

- Run the full test suite (`pytest tests/ -v`) and the frontend suite
  (`npm test` / `npm run type-check` / `npm run build` in `apps/web`) --
  see `reports/PRODUCTION_DEPLOYMENT_READINESS_REPORT.txt` for the exact
  count as of this audit.
- Confirm `tests/unit/test_no_order_placement_capability.py`,
  `tests/unit/test_coindcx_no_credential_logging.py`, and
  `tests/unit/test_no_forbidden_financial_claims.py` all pass -- these are
  the tests directly enforcing the platform's safety and honesty
  constraints.
- Run `alembic upgrade head` against the target Postgres database before
  first deploy. This audit could not run a live migration against a real
  reachable Postgres in the sandbox it ran in (no Docker available, and
  the one local Postgres instance found required a password not available
  to this session) -- verified by static review only (dialect-agnostic
  `GUID` type, dual `sqlite_where`/`postgresql_where` indexes, `asyncpg` +
  `psycopg2-binary` already in `pyproject.toml`). Run this for real, and
  watch it succeed, before the first production deploy.
- Note: `infrastructure/Dockerfile.api` pins `python:3.11-slim`, but every
  test in this project has only ever actually been run against a local
  Python 3.14 virtualenv. Both satisfy `pyproject.toml`'s
  `requires-python = ">=3.11"`, but the exact 3.11 combination has never
  been executed -- if deploying via that Dockerfile, build and run the
  full test suite inside the built image at least once before relying on
  it in production.
- Before enabling `SCHEDULER_ENABLED=true` or `MARKET_DATA_WS_ENABLED=true`
  against a real CoinDCX account in production, both have already been
  verified against the real account and the real CoinDCX WebSocket in
  this development environment (see `reports/LIVE_MARKET_DATA_FINAL_
  VALIDATION.txt` and `reports/LIVE_MARKET_DATA_PRODUCTION_HARDENING_
  REPORT.txt`) -- re-verify once more in the actual production
  environment/network before trusting it unattended there too.
