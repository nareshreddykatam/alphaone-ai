"""Extra fine-grained parameter sensitivity check for the two strongest
V3 Stage-2 survivors, requested by Phase 11 ("look for a stable region,
not one magic parameter"). The original 3-point grids used to SELECT the
frozen parameter (scripts/research_v3_validation.py) showed a possible
sharp peak (KAMA er_period=10) or a parameter sitting at the tested
grid's edge (Range Expansion tr_ratio_mult=2.0) -- this re-runs OOS at
denser/extended neighbor values, still frozen everywhere else, to see
whether performance holds in a neighborhood or collapses/keeps climbing.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.research_v2_rigorous as v2
import scripts.research_v3_discovery as v3
from scripts.research_v3_validation import run_bt
from services.backtester.engine import BacktestConfig


def main():
    conn = sqlite3.connect(v2.DB_PATH)
    df_4h = v2.load_candles(conn, "4h")
    conn.close()
    _, _, oos = v2.chronological_split(df_4h)
    config = BacktestConfig()

    print("V3_KAMA_TREND_4H fine sensitivity (frozen er_period=10):")
    for er in [9, 10, 11, 12]:
        r = run_bt("V3_KAMA_TREND_4H", oos, {"er_period": er}, config)
        marker = " <== frozen" if er == 10 else ""
        print(f"  er_period={er}: trades={r.total_trades} pf={r.profit_factor:.2f} return={r.total_pnl_pct:.2f}% max_dd={r.max_drawdown_pct:.2f}%{marker}")

    print("\nV3_RANGE_EXPANSION_4H extended sensitivity (frozen tr_ratio_mult=2.0, grid edge):")
    for tr in [2.0, 2.25, 2.5, 2.75]:
        r = run_bt("V3_RANGE_EXPANSION_4H", oos, {"tr_ratio_mult": tr}, config)
        marker = " <== frozen" if tr == 2.0 else ""
        print(f"  tr_ratio_mult={tr}: trades={r.total_trades} pf={r.profit_factor:.2f} return={r.total_pnl_pct:.2f}% max_dd={r.max_drawdown_pct:.2f}%{marker}")


if __name__ == "__main__":
    main()
