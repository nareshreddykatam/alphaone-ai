# AlphaOne BTC AI

AI-powered BTC/USDT perpetual futures trading intelligence platform.

## Overview

AlphaOne BTC AI is a production-quality trading intelligence system that:

- Analyzes BTC/USDT perpetual futures markets using ML models
- Generates LONG, SHORT, EXIT, and NO TRADE signals
- Manages risk rigorously with configurable limits
- Communicates through a web dashboard and Telegram
- Operates in paper trading mode by default

## Architecture

```
Market Data → Feature Engine → Regime Detector → ML Model → Risk Engine → Signals
                                                                          ↓
                                                          Dashboard + Telegram
```

## Technology Stack

- **Frontend**: Next.js 14, TypeScript, Tailwind CSS
- **Backend**: Python, FastAPI, SQLAlchemy, asyncpg
- **ML**: XGBoost, LightGBM, Scikit-learn
- **Database**: PostgreSQL, Redis
- **Exchange**: CCXT (Binance integration)

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+
- PostgreSQL 16+
- Redis 7+

### Setup

1. Clone and enter the project:
   ```bash
   cd alphaone
   ```

2. Set up Python environment:
   ```bash
   pip install -e ".[dev]"
   ```

3. Configure environment:
   ```bash
   cp .env.example .env
   # Edit .env with your settings
   ```

4. Start infrastructure:
   ```bash
   docker-compose up -d db redis
   ```

5. Run migrations:
   ```bash
   alembic upgrade head
   ```

6. Start the backend:
   ```bash
   uvicorn apps.api.main:app --reload --port 8000
   ```

7. Start the frontend:
   ```bash
   cd apps/web
   npm install
   npm run dev
   ```

8. Open http://localhost:3000

### Run Tests

```bash
pytest tests/
```

### Lint

```bash
ruff check .
ruff format .
```

## Project Structure

```
alphaone/
├── apps/
│   ├── web/                    # Next.js dashboard
│   └── api/                    # FastAPI backend
├── services/
│   ├── market_data/            # Exchange abstraction + ingestion
│   ├── feature_engine/         # All feature computation
│   ├── signal_engine/          # ML inference + signal generation
│   ├── risk_engine/            # Risk management
│   ├── backtester/             # Event-driven backtesting
│   ├── paper_trader/           # Paper trading engine
│   └── telegram/               # Telegram bot + notifications
├── ml/
│   ├── datasets/               # Data loading
│   ├── features/               # Feature pipelines
│   ├── training/               # Model training
│   └── evaluation/             # Metrics, walk-forward
├── database/
│   ├── migrations/             # Alembic
│   └── schema/                 # SQLAlchemy models
├── tests/
├── docs/
└── infrastructure/
```

## Trading Modes

- **Paper Trading** (default): Uses live market data, no real orders
- **Backtest**: Historical simulation with fees, funding, slippage
- **Testnet**: Exchange sandbox (future)
- **Live**: Real trading (disabled by default)

## Risk Configuration

```env
RISK_PER_TRADE_PCT=0.5       # Risk 0.5% per trade
MAX_DAILY_LOSS_PCT=2.0       # Max 2% daily loss
MAX_DRAWDOWN_PCT=10.0        # Max 10% drawdown
MAX_LEVERAGE=5               # Max 5x leverage
MAX_POSITIONS=1              # Max 1 open position
```

## Telegram Commands

- `/start` - Welcome message
- `/status` - Bot status
- `/pause` - Pause paper trading
- `/resume` - Resume paper trading
- `/help` - Available commands

## Development

See [docs/](docs/) for detailed documentation.

## License

MIT
