"""Process-wide PaperTrader singleton (AI Trading V1), mirroring
services/market_data/live_state.py's `market_ws` pattern -- so
services/scheduler/jobs.py's ai_paper_trading_job and any router reading
live paper-trading status always see the exact same in-memory state,
never two independently-drifting copies.

Equity/risk-engine state is in-memory only and resets on a process
restart -- the same documented limitation RiskEngine's own notional
equity tracker already carries (docs/known_limitations.md). Trade
HISTORY does not reset: every open/partial-exit/close event is mirrored
into the Trade/TradeExecution tables by services/paper_trader/persistence.py,
which is the durable source of truth for reporting.
"""
from services.paper_trader.engine import PaperTrader
from services.risk_engine.engine import RiskConfig

# Same default risk posture as the live (unused) production RiskConfig --
# single position, 0.5% risk/trade, 5x max leverage -- see apps/api/config.py.
paper_trader = PaperTrader(risk_config=RiskConfig(), initial_equity=10000)
