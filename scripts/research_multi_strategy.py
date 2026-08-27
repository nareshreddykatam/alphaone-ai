"""Research + backtest + walk-forward validation for the 9 NEW candidate
strategies (S01-S04 at 15m, S06-S10 at 4h). Reads real historical BTC/USDT
candles directly from the local research SQLite DB (alphaone_research.db)
-- no pyarrow (blocked on this machine, see memory), plain sqlite3 + pandas.

Every result printed here is real: this script performs no synthetic data
generation and no result fabrication. Run with:

    .venv/Scripts/python.exe scripts/research_multi_strategy.py
"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.backtester.engine import Backtester, BacktestConfig
from ml.evaluation.multi_strategy_signals import MULTI_STRATEGIES, precompute_mtf_trend, mtf_trend_signal_func

DB_PATH = str(Path(__file__).resolve().parent.parent / "alphaone_research.db")


def load_candles(conn, timeframe: str) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT timestamp, open, high, low, close, volume FROM candles "
        "WHERE symbol = 'BTC/USDT' AND timeframe = ? ORDER BY timestamp ASC",
        conn, params=(timeframe,), parse_dates=["timestamp"],
    )
    return df


def create_walk_forward_splits(df: pd.DataFrame, train_window: int, test_window: int, step: int, embargo: int):
    splits = []
    start = 0
    while start + train_window + embargo + test_window <= len(df):
        train = df.iloc[start:start + train_window].copy()
        test_start = start + train_window + embargo
        test = df.iloc[test_start:test_start + test_window].copy()
        splits.append((train, test))
        start += step
    return splits


def run_full_and_walk_forward(strategy_id: str, spec: dict, df_15m, df_4h, df_1d, config: BacktestConfig):
    timeframe = spec["timeframe"]
    base_df = df_15m if timeframe == "15m" else df_4h

    if strategy_id == "S10_MTF_TREND_4H":
        prepared_full = precompute_mtf_trend(base_df, df_1d)
    else:
        prepared_full = spec["precompute"](base_df)

    signal_func = spec["factory"]()
    bt = Backtester(config)
    full_result = bt.run(prepared_full.reset_index(drop=True), signal_func)

    if timeframe == "15m":
        train_window, test_window, step, embargo = 32000, 8000, 8000, 800
    else:
        train_window, test_window, step, embargo = 2000, 500, 500, 50

    splits = create_walk_forward_splits(base_df, train_window, test_window, step, embargo)
    fold_results = []
    for i, (_, test_df) in enumerate(splits):
        if strategy_id == "S10_MTF_TREND_4H":
            prepared_test = precompute_mtf_trend(test_df, df_1d)
        else:
            prepared_test = spec["precompute"](test_df)
        signal_func_fold = spec["factory"]()
        bt_fold = Backtester(config)
        result = bt_fold.run(prepared_test.reset_index(drop=True), signal_func_fold)
        fold_results.append((i + 1, test_df["timestamp"].iloc[0], test_df["timestamp"].iloc[-1], result))

    return full_result, fold_results


def format_result(label: str, result) -> str:
    return (
        f"  {label}: trades={result.total_trades} win_rate={result.win_rate:.1f}% "
        f"pf={result.profit_factor:.2f} expectancy={result.expectancy:.2f} "
        f"return={result.total_pnl_pct:.2f}% max_dd={result.max_drawdown_pct:.2f}% "
        f"sharpe={result.sharpe_ratio:.2f} avg_r={result.average_r:.2f} "
        f"consec_losses={result.consecutive_losses}"
    )


def main():
    conn = sqlite3.connect(DB_PATH)
    print(f"Loading real historical BTC/USDT candles from {DB_PATH} ...")
    df_15m = load_candles(conn, "15m")
    df_4h = load_candles(conn, "4h")
    df_1d = load_candles(conn, "1d")
    conn.close()
    print(f"15m: {len(df_15m)} candles ({df_15m['timestamp'].min()} -> {df_15m['timestamp'].max()})")
    print(f"4h:  {len(df_4h)} candles ({df_4h['timestamp'].min()} -> {df_4h['timestamp'].max()})")
    print(f"1d:  {len(df_1d)} candles ({df_1d['timestamp'].min()} -> {df_1d['timestamp'].max()})")
    print()

    config = BacktestConfig()  # real fees/slippage/spread via the default ExchangeSpec, same as every other backtest in this project
    print(f"Cost model: taker_fee={config.fee_rate}, slippage={config.slippage_rate}, funding_avg={config.funding_rate_avg}")
    print()

    report_lines = []
    for strategy_id, spec in MULTI_STRATEGIES.items():
        print(f"=== {strategy_id} ({spec['display_name']}, {spec['timeframe']}) ===")
        report_lines.append(f"=== {strategy_id} ({spec['display_name']}, {spec['timeframe']}) ===")
        try:
            full_result, fold_results = run_full_and_walk_forward(strategy_id, spec, df_15m, df_4h, df_1d, config)
        except Exception as e:
            msg = f"  ERROR during backtest: {e}"
            print(msg)
            report_lines.append(msg)
            print()
            continue

        full_line = format_result("FULL PERIOD", full_result)
        print(full_line)
        report_lines.append(full_line)

        profitable_folds = sum(1 for _, _, _, r in fold_results if r.total_pnl_pct > 0 and r.total_trades > 0)
        folds_with_trades = sum(1 for _, _, _, r in fold_results if r.total_trades > 0)
        fold_summary = f"  WALK-FORWARD: {len(fold_results)} folds, {folds_with_trades} with trades, {profitable_folds} profitable"
        print(fold_summary)
        report_lines.append(fold_summary)

        for fold_num, start, end, r in fold_results:
            line = format_result(f"    Fold {fold_num} [{start.date()} -> {end.date()}]", r)
            print(line)
            report_lines.append(line)

        print()
        report_lines.append("")

    out_path = Path(__file__).resolve().parent.parent / "reports" / "MULTI_STRATEGY_RESEARCH_RESULTS.txt"
    out_path.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"Full report written to {out_path}")


if __name__ == "__main__":
    main()
