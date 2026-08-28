"""AI TRADING V1 -- model research (Phases 1, 4, 5, 6 of the task spec).

Re-runs Phase 3's exact rigorous methodology (chronological split with
embargo, train-only scaling, validation-only calibration, real cost-aware
backtesting via the shared Backtester -- see docs/ml_methodology.md and
ml/evaluation/ml_pipeline.py, all REUSED here, not reimplemented) on the
current, larger dataset, adding ONE new ablation this pass contributes:

  E_technical_structure_regime_strategies -- C plus each PRODUCTION_ELIGIBLE
  rule-based strategy's own signal as a feature (ml/features/strategy_features.py).
  This did not exist in Phase 3 (the V3 strategies it depends on postdate it).

Ablation D (+ derivatives) is re-attempted but expected to remain
constrained by the same ~1-month OI history Phase 3 found (see
docs/known_limitations.md) -- reported honestly either way, not hidden.

Stage 1: single embargoed train/val/test split, every (ablation x model)
combo, screened on test-split trading performance (PF, trades, return).
Stage 2: only combos that clear PF > 1.0 with >= 20 test trades advance to
a real rolling walk-forward + a cost-sensitivity ladder -- exactly Phase 3's
own two-stage discipline (a single split is not proof, walk-forward and
cost stress are what separate a real edge from a lucky split).

Real data only (alphaone_research.db). No synthetic labels, no fabricated
results. Ensemble is skipped -- documented, pre-existing bug (VotingClassifier
can't refit LightGBM with early stopping, see docs/known_limitations.md),
not something this pass attempts to fix.
"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ml.evaluation.ml_pipeline import (
    run_single_combo, run_ml_walk_forward, format_ablation_table,
    format_ml_walk_forward_table, build_model_metadata, MODEL_NAMES,
)
from ml.evaluation.cost_sensitivity import run_cost_sensitivity, format_cost_sensitivity_table
from ml.features.feature_groups import assemble_features, select_features, ABLATION_CONFIGS
from ml.labeling import compute_triple_barrier_labels, TripleBarrierConfig, label_distribution
from ml.signal import make_ml_signal_func, MLSignalConfig
from ml.training.trainer import ModelTrainer
from services.backtester.engine import BacktestConfig

DB_PATH = str(Path(__file__).resolve().parent.parent / "alphaone_research.db")
SCREEN_MODELS = [m for m in MODEL_NAMES if m != "ensemble"]  # ensemble excluded, see module docstring


def load_candles(conn, timeframe: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT timestamp, open, high, low, close, volume FROM candles "
        "WHERE symbol = 'BTC/USDT' AND timeframe = ? ORDER BY timestamp ASC",
        conn, params=(timeframe,), parse_dates=["timestamp"],
    )


def load_funding(conn) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT timestamp, rate FROM funding_rates WHERE symbol = 'BTC/USDT' ORDER BY timestamp ASC",
        conn, parse_dates=["timestamp"],
    )


def load_open_interest(conn) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT timestamp, value FROM open_interest WHERE symbol = 'BTC/USDT' ORDER BY timestamp ASC",
        conn, parse_dates=["timestamp"],
    )


def main():
    conn = sqlite3.connect(DB_PATH)
    df_4h = load_candles(conn, "4h")
    df_1h = load_candles(conn, "1h")
    funding = load_funding(conn)
    oi = load_open_interest(conn)
    conn.close()

    print(f"4h candles: {len(df_4h)} ({df_4h['timestamp'].min()} -> {df_4h['timestamp'].max()})")
    print(f"1h candles: {len(df_1h)}, funding: {len(funding)} rows, open_interest: {len(oi)} rows")

    barrier_config = TripleBarrierConfig()  # 12-bar horizon, 2:1 ATR TP:SL, min RR 1.5
    print(f"\nLabel design: triple-barrier, horizon={barrier_config.horizon_bars} bars "
          f"(~{barrier_config.horizon_bars * 4}h), TP={barrier_config.tp_atr_multiple}x ATR, "
          f"SL={barrier_config.sl_atr_multiple}x ATR, min R:R={barrier_config.min_risk_reward}")

    print("\nAssembling features (all groups, incl. strategy-signal features)...")
    assembled, _all_features = assemble_features(
        df_4h, df_context=df_1h, funding_rates=funding, open_interest=oi, include_strategy_signals=True,
    )
    labeled = compute_triple_barrier_labels(assembled, barrier_config)
    print(f"Labeled rows: {len(labeled)} (dropped {len(assembled) - len(labeled)} -- insufficient forward data)")
    dist = label_distribution(labeled)
    print(f"Label distribution: {dist}")

    backtest_config = BacktestConfig()

    print(f"\n{'=' * 70}\nSTAGE 1: ABLATION x MODEL SCREEN (single embargoed split)\n{'=' * 70}")
    screen_results = []
    for ablation_name in ABLATION_CONFIGS:
        feature_cols = select_features(labeled, ablation_name)
        for model_name in SCREEN_MODELS:
            try:
                result = run_single_combo(
                    labeled, feature_cols, model_name, ablation_name, barrier_config, backtest_config,
                    funding_rates=funding, model_path="./ml/models/_scratch_ai_v1",
                )
            except Exception as e:
                print(f"  ERROR {ablation_name}/{model_name}: {e}")
                result = None
            if result is None:
                print(f"  SKIP {ablation_name}/{model_name}: insufficient rows after split/dropna")
                continue
            screen_results.append(result)
            tr = result.trading_result
            print(f"  {ablation_name:45s} {model_name:20s} n_feat={result.n_features:3d} "
                  f"acc={result.classification_metrics['accuracy']:.3f} "
                  f"auc={result.classification_metrics['auc_roc']:.3f} "
                  f"brier={result.classification_metrics['brier_score']:.3f} "
                  f"trades={tr.total_trades:4d} pf={tr.profit_factor:.2f} "
                  f"return={tr.total_pnl_pct:+.2f}% dd={tr.max_drawdown_pct:.2f}%")

    print(f"\n{'=' * 70}\nSTAGE 1 SUMMARY (sorted by profit factor)\n{'=' * 70}")
    table = format_ablation_table(screen_results)
    if not table.empty:
        print(table.sort_values("profit_factor", ascending=False).to_string(index=False))

    survivors = [r for r in screen_results if r.trading_result.profit_factor > 1.0 and r.trading_result.total_trades >= 20]
    print(f"\n{len(survivors)} combo(s) clear Stage 1 (PF>1.0, >=20 test trades): "
          f"{[(r.ablation_name, r.model_name) for r in survivors]}")

    print(f"\n{'=' * 70}\nSTAGE 2: WALK-FORWARD + COST SENSITIVITY (Stage 1 survivors only)\n{'=' * 70}")
    stage2_summaries = []
    for r in survivors:
        print(f"\n--- {r.ablation_name} / {r.model_name} ---")
        feature_cols = select_features(labeled, r.ablation_name)

        wf_folds = run_ml_walk_forward(
            labeled, feature_cols, r.model_name, barrier_config, backtest_config,
            funding_rates=funding, train_window=1500, val_window=300, test_window=300, step=300,
        )
        wf_table = format_ml_walk_forward_table(wf_folds)
        profitable_folds = int((wf_table["net_return_pct"] > 0).sum()) if not wf_table.empty else 0
        print(f"  Walk-forward: {len(wf_folds)} folds, {profitable_folds} profitable")
        if not wf_table.empty:
            print(wf_table.to_string(index=False))

        # Cost sensitivity on the SAME single-split model/test data already trained above.
        trainer = ModelTrainer(model_path="./ml/models/_scratch_ai_v1")
        embargo = barrier_config.horizon_bars + 2
        n = len(labeled)
        train_end = int(n * 0.6)
        val_start = train_end + embargo
        val_end = val_start + int(n * 0.2)
        test_start = val_end + embargo
        train_df = labeled.iloc[:train_end].reset_index(drop=True)
        val_df = labeled.iloc[val_start:val_end].reset_index(drop=True)
        test_df = labeled.iloc[test_start:].reset_index(drop=True)

        X_train, y_train = trainer.prepare_data(train_df, feature_cols, "label")
        X_val, y_val = trainer.prepare_data(val_df, feature_cols, "label")
        scaler = None
        if r.model_name == "logistic_regression":
            from ml.features.scaling import fit_transform_train_only
            train_feat_df = pd.DataFrame(X_train, columns=feature_cols)
            val_feat_df = pd.DataFrame(X_val, columns=feature_cols)
            scaled_train, (scaled_val,), scaler = fit_transform_train_only(train_feat_df, [val_feat_df], feature_cols)
            X_train, X_val = scaled_train.values, scaled_val.values

        from ml.evaluation.ml_pipeline import train_model, best_calibration
        model = train_model(r.model_name, X_train, y_train, X_val, y_val, feature_cols, trainer)
        calibrated, _, _ = best_calibration(trainer, model, X_val, y_val)
        signal_func = make_ml_signal_func(calibrated, feature_cols, barrier_config, MLSignalConfig(), scaler=scaler)

        fold_funding = funding[
            (funding["timestamp"] >= test_df["timestamp"].iloc[0]) & (funding["timestamp"] <= test_df["timestamp"].iloc[-1])
        ] if len(test_df) > 0 else None
        cost_results = run_cost_sensitivity(test_df.reset_index(drop=True), signal_func, backtest_config, funding_rates=fold_funding)
        cost_table = format_cost_sensitivity_table(cost_results)
        print("  Cost sensitivity:")
        print(cost_table.to_string(index=False))

        base_pf = r.trading_result.profit_factor
        wf_survives = len(wf_folds) > 0 and profitable_folds >= len(wf_folds) / 2
        cost_2x_pf = cost_table[cost_table["scenario"] == "base_plus_100pct"]["profit_factor"].iloc[0] if not cost_table.empty else 0
        cost_survives = cost_2x_pf > 1.0

        stage2_summaries.append({
            "ablation": r.ablation_name, "model": r.model_name, "screen_pf": base_pf,
            "wf_folds": len(wf_folds), "wf_profitable_folds": profitable_folds, "wf_survives": wf_survives,
            "cost_2x_pf": cost_2x_pf, "cost_survives": cost_survives,
            "verdict": "CANDIDATE (survives WF + 2x cost)" if (wf_survives and cost_survives) else "RESEARCH_ONLY (fails deeper validation)",
        })

    print(f"\n{'=' * 70}\nFINAL VERDICT PER STAGE-1 SURVIVOR\n{'=' * 70}")
    for s in stage2_summaries:
        print(f"  {s['ablation']}/{s['model']}: screen_pf={s['screen_pf']:.2f} "
              f"wf={s['wf_profitable_folds']}/{s['wf_folds']} folds profitable "
              f"cost_2x_pf={s['cost_2x_pf']:.2f} -> {s['verdict']}")
    if not stage2_summaries:
        print("  No combo cleared even the Stage 1 screen (PF>1.0, >=20 trades). No AI model to deploy.")

    print("\nDone.")


if __name__ == "__main__":
    main()
