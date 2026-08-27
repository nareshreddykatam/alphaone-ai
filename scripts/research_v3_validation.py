"""Stage 2 full validation for the 3 candidates that survived Stage 1
screening in scripts/research_v3_discovery.py (all others failed the
cheap full-period PF>=1.0 cut, see reports/STRATEGY_RESEARCH_V3_RIGOROUS_REPORT.txt):

  V3_KAMA_TREND_4H, V3_RANGE_EXPANSION_4H, V3_HMA_TREND_4H

Reuses scripts/research_v2_rigorous.py's exact train/val/OOS/walk-forward/
regime/sensitivity/bootstrap machinery, plus one NEW dimension this task
explicitly asked for that v2 did not have: a cost-robustness stress test
(worse-than-baseline fees/slippage) run on the frozen OOS strategy.
"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.research_v2_rigorous as v2
import scripts.research_v3_discovery as v3
from services.backtester.engine import Backtester, BacktestConfig
from services.backtester.exchange_spec import ExchangeSpec

PARAM_GRIDS_V3 = {
    "V3_KAMA_TREND_4H": {"er_period": [8, 10, 14]},
    "V3_RANGE_EXPANSION_4H": {"tr_ratio_mult": [1.5, 1.75, 2.0]},
    "V3_HMA_TREND_4H": {"period": [15, 20, 25]},
}

FACTORIES = {
    "V3_KAMA_TREND_4H": lambda p: v3.kama_signal(),  # er_period is a precompute-time param
    "V3_RANGE_EXPANSION_4H": lambda p: v3.range_expansion_signal(tr_ratio_mult=p.get("tr_ratio_mult", 1.75)),
    "V3_HMA_TREND_4H": lambda p: v3.hma_signal(),  # period is a precompute-time param
}


def precompute(strategy_id, df, params):
    if strategy_id == "V3_KAMA_TREND_4H":
        return v3.precompute_kama(df, period=params.get("er_period", 10))
    if strategy_id == "V3_HMA_TREND_4H":
        return v3.precompute_hma(df, period=params.get("period", 20))
    return v3.precompute_range_expansion(df)


def run_bt(strategy_id, df, params, config):
    prepared = precompute(strategy_id, df, params)
    fn = FACTORIES[strategy_id](params)
    bt = Backtester(config)
    return bt.run(prepared.reset_index(drop=True), fn)


def select_best(strategy_id, train_df, val_df, config):
    grid = PARAM_GRIDS_V3.get(strategy_id, {})
    combos = v2._param_combos(grid)
    best = None
    best_result = None
    for p in combos:
        res = run_bt(strategy_id, val_df, p, config)
        if res.total_trades < 15:
            continue
        score = (res.profit_factor, -res.max_drawdown_pct)
        if best is None or score > best[0]:
            best = (score, p)
            best_result = res
    if best is None:
        return combos[0], run_bt(strategy_id, val_df, combos[0], config)
    return best[1], best_result


def walk_forward(strategy_id, oos_df, params, config, n_folds=4):
    n = len(oos_df)
    fold_size = n // n_folds
    folds = []
    for i in range(n_folds):
        start = i * fold_size
        end = n if i == n_folds - 1 else (i + 1) * fold_size
        fdf = oos_df.iloc[start:end].reset_index(drop=True)
        if len(fdf) < 60:
            continue
        r = run_bt(strategy_id, fdf, params, config)
        folds.append({"fold": i + 1, "start": str(fdf["timestamp"].iloc[0]), "end": str(fdf["timestamp"].iloc[-1]),
                       "trades": r.total_trades, "pf": r.profit_factor, "return_pct": r.total_pnl_pct,
                       "max_dd_pct": r.max_drawdown_pct, "win_rate": r.win_rate})
    return folds


def cost_stress_test(strategy_id, oos_df, params, base_config):
    """NEW dimension (Phase 12): re-run OOS with worse-than-baseline fees
    and slippage (2x taker fee, 3x slippage) -- the strategy should not
    collapse from a small realistic cost deterioration, especially since
    fees/slippage estimates are themselves approximations, not guarantees
    of the exact real-world cost AlphaOne's user would pay."""
    base_spec = base_config.exchange_spec
    stressed_spec = ExchangeSpec(
        taker_fee=base_spec.taker_fee * 2, maker_fee=base_spec.maker_fee * 2,
        slippage_bps=base_spec.slippage_bps * 3, spread_bps=base_spec.spread_bps * 2,
        funding_interval_hours=base_spec.funding_interval_hours,
    )
    stressed_config = BacktestConfig(exchange_spec=stressed_spec, funding_rate_avg=base_config.funding_rate_avg * 2)
    r = run_bt(strategy_id, oos_df, params, stressed_config)
    return {"taker_fee": stressed_spec.taker_fee, "slippage_rate": stressed_spec.slippage_rate,
            "trades": r.total_trades, "pf": r.profit_factor, "return_pct": r.total_pnl_pct, "max_dd_pct": r.max_drawdown_pct}


def main():
    conn = sqlite3.connect(v2.DB_PATH)
    df_4h = v2.load_candles(conn, "4h")
    conn.close()

    config = BacktestConfig()
    train, val, oos = v2.chronological_split(df_4h)
    print(f"4h split: train={len(train)} val={len(val)} oos={len(oos)}")
    print(f"OOS window: {oos['timestamp'].iloc[0]} -> {oos['timestamp'].iloc[-1]}")

    for strategy_id in PARAM_GRIDS_V3:
        print(f"\n{'=' * 70}\n{strategy_id}\n{'=' * 70}")
        best_params, val_result = select_best(strategy_id, train, val, config)
        print(f"  Selected params (VAL, min 15 trades): {best_params}")
        print(f"  VAL: trades={val_result.total_trades} pf={val_result.profit_factor:.2f} return={val_result.total_pnl_pct:.2f}%")

        oos_result = run_bt(strategy_id, oos, best_params, config)
        print(f"  OOS: trades={oos_result.total_trades} win_rate={oos_result.win_rate:.1f}% pf={oos_result.profit_factor:.2f} "
              f"return={oos_result.total_pnl_pct:.2f}% max_dd={oos_result.max_drawdown_pct:.2f}% sharpe={oos_result.sharpe_ratio:.2f} "
              f"expectancy={oos_result.expectancy:.2f}")

        long_pnls = [t.pnl for t in oos_result.trades if t.side == "LONG"]
        short_pnls = [t.pnl for t in oos_result.trades if t.side == "SHORT"]
        print(f"  LONG:  {v2.summarize_pnls(long_pnls)}")
        print(f"  SHORT: {v2.summarize_pnls(short_pnls)}")

        regimes = v2.regime_labels(oos)
        regime_pnls = v2.attribute_trades_to_regime(oos_result.trades, regimes)
        print(f"  Regime: {{k: v2.summarize_pnls(v) for k, v in regime_pnls.items()}}" if False else
              {k: v2.summarize_pnls(vv) for k, vv in regime_pnls.items()})

        folds = walk_forward(strategy_id, oos, best_params, config, 4)
        profitable = sum(1 for f in folds if f["return_pct"] > 0 and f["trades"] > 0)
        print(f"  OOS walk-forward: {len(folds)} folds, {profitable} profitable")
        for f in folds:
            print(f"    {f}")

        boot = v2.bootstrap_drawdown(oos_result.trades, config.initial_capital)
        print(f"  Bootstrap: max_dd p5/p50/p95 = {boot.get('max_dd_p5')}/{boot.get('max_dd_p50')}/{boot.get('max_dd_p95')} "
              f"(observed {oos_result.max_drawdown_pct:.2f}%)")

        grid = PARAM_GRIDS_V3.get(strategy_id, {})
        if grid:
            print("  Parameter sensitivity (OOS, frozen elsewhere):")
            for key, values in grid.items():
                for val_ in values:
                    p2 = dict(best_params)
                    p2[key] = val_
                    r2 = run_bt(strategy_id, oos, p2, config)
                    marker = " <== frozen" if val_ == best_params.get(key) else ""
                    print(f"    {key}={val_}: trades={r2.total_trades} pf={r2.profit_factor:.2f} return={r2.total_pnl_pct:.2f}%{marker}")

        stress = cost_stress_test(strategy_id, oos, best_params, config)
        print(f"  Cost stress test (2x fee, 3x slippage, 2x funding): {stress}")


if __name__ == "__main__":
    main()
