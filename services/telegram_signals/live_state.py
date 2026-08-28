"""Process-wide paper-trading book for the Multi-Coin AI Futures System's
fixed-margin trades (AI-sourced AND Telegram-sourced), separate from AI
Trading V1's original single-BTC `services/paper_trader/live_state.py`
singleton -- that one's RiskConfig(max_positions=1) is correct for its own
single-symbol, percent-of-equity design, but WRONG here: Phase 20
requires independent concurrent positions across multiple coins (BTC
LONG + ETH SHORT + SOL LONG simultaneously), which a max_positions=1 gate
would silently block.

Reuses the exact same, already-tested PaperTrader class (TP1/TP2/TP3
partial-exit slicing, trade-level PnL) -- only the RiskConfig differs.
The REAL per-day entry limit for this book is enforced separately and
correctly by services/risk_engine/fixed_margin.check_fixed_margin_trade
(a real DB count of today's entries, 15/day hard max), so
max_positions here is set generously high rather than =1 -- it exists
only as a sanity ceiling, not the system's actual daily-volume control.
"""
from services.paper_trader.engine import PaperTrader
from services.risk_engine.engine import RiskConfig

MAX_CONCURRENT_POSITIONS = 20  # generous ceiling; the real daily-entry limit is fixed_margin.DAILY_TRADE_MAX

multi_coin_paper_trader = PaperTrader(
    risk_config=RiskConfig(max_positions=MAX_CONCURRENT_POSITIONS), initial_equity=10000,
)
