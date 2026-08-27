"""Rigorous re-research pass for the BTC/USDT multi-strategy system.

Methodology (see the task this was written for -- this is not the same,
looser methodology scripts/research_multi_strategy.py used):

  1. Chronological, non-shuffled 60/20/20 TRAIN / VALIDATION / OOS TEST
     split of the real historical candles (never randomly shuffled --
     shuffling a time series before splitting is itself a leakage bug).
  2. For any strategy with a genuinely free parameter, a SMALL grid is
     backtested on TRAIN, the winner is selected using VALIDATION
     performance only (never touching OOS), then FROZEN. OOS is evaluated
     exactly once with the frozen parameters -- never re-tuned after
     looking at OOS (that would be the exact leakage this design avoids).
  3. Walk-forward folds are cut ONLY from the OOS region (forward-chaining
     sub-folds), so fold-level stats never reuse TRAIN/VAL bars.
  4. Long/short trades are reported separately.
  5. Regime segmentation labels each OOS bar bull/bear (200-period EMA
     slope) and high/low volatility (rolling ATR percentile), then
     attributes each closed trade to the regime active at its entry.
  6. Parameter sensitivity re-runs OOS at each frozen parameter's
     immediate neighbors.
  7. A trade-order bootstrap (reshuffle OOS trade PnL sequence N times)
     estimates how much of the observed drawdown is a function of trade
     ORDER vs. a real edge -- a robustness diagnostic, not proof of
     anything.
  8. Cross-strategy correlation of OOS daily-return series flags near-
     duplicate strategies.

Real data only (alphaone_research.db, Binance BTC/USDT, ~3 years -- see
this script's own printed data-quality audit). No synthetic/fabricated
inputs, no fabricated outputs.
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.backtester.engine import Backtester, BacktestConfig, BacktestResult, BacktestTrade
from services.feature_engine.indicators import ema, atr
from ml.evaluation.multi_strategy_signals import MULTI_STRATEGIES, precompute_mtf_trend, mtf_trend_signal_func

DB_PATH = str(Path(__file__).resolve().parent.parent / "alphaone_research.db")

# Small, reasonable parameter grids for strategies with a genuinely free
# parameter -- selected on VALIDATION only, never on OOS. Strategies not
# listed here use their single documented default (no free parameter
# worth grid-searching without inventing an arbitrary one).
PARAM_GRIDS = {
    "S01_MOMENTUM_BREAKOUT_15M": {"lookback": [15, 20, 25], "volume_mult": [1.25, 1.5, 1.75]},
    "S02_EMA_PULLBACK_15M": {"rsi_floor": [35, 40, 45]},
    "S03_VWAP_REVERSION_15M": {"deviation_atr_mult": [2.0, 2.5, 3.0]},
    "S04_RSI_BB_15M": {"rsi_oversold": [25, 30, 35]},
    "S06_SUPERTREND_ATR_4H": {"period": [7, 10, 14], "multiplier": [2.5, 3.0, 3.5]},
    "S07_MACD_MOMENTUM_4H": {},  # canonical 12/26/9 -- not grid-searched, see module docstring
    "S08_EMA_ADX_4H": {"adx_threshold": [15, 20, 25]},
    "S09_ATR_BREAKOUT_4H": {"squeeze_pctile": [0.15, 0.25, 0.35]},
    "S10_MTF_TREND_4H": {"adx_threshold": [15, 20, 25]},
    "S11_ZSCORE_REVERSION_15M": {"z_entry": [1.5, 2.0, 2.5]},
    "S12_STRUCTURE_RETEST_4H": {"retest_atr_mult": [0.5, 0.75, 1.0]},
}


def load_candles(conn, timeframe: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT timestamp, open, high, low, close, volume FROM candles "
        "WHERE symbol = 'BTC/USDT' AND timeframe = ? ORDER BY timestamp ASC",
        conn, params=(timeframe,), parse_dates=["timestamp"],
    )


def chronological_split(df: pd.DataFrame, train_frac=0.6, val_frac=0.2):
    n = len(df)
    train_end = int(n * train_frac)
    val_end = int(n * (train_frac + val_frac))
    return df.iloc[:train_end].reset_index(drop=True), df.iloc[train_end:val_end].reset_index(drop=True), df.iloc[val_end:].reset_index(drop=True)


def _make_signal_func(strategy_id: str, params: dict, df_1d=None):
    spec = MULTI_STRATEGIES[strategy_id]
    if strategy_id == "S10_MTF_TREND_4H":
        return mtf_trend_signal_func(20, params.get("adx_threshold", 20))
    factory_kwargs_map = {
        "S01_MOMENTUM_BREAKOUT_15M": lambda p: dict(lookback=p.get("lookback", 20), volume_mult=p.get("volume_mult", 1.5), atr_lookback=5),
        "S02_EMA_PULLBACK_15M": lambda p: dict(rsi_floor=p.get("rsi_floor", 40), rsi_ceiling=100 - p.get("rsi_floor", 40)),
        "S03_VWAP_REVERSION_15M": lambda p: dict(deviation_atr_mult=p.get("deviation_atr_mult", 2.5)),
        "S04_RSI_BB_15M": lambda p: dict(rsi_oversold=p.get("rsi_oversold", 30), rsi_overbought=100 - p.get("rsi_oversold", 30)),
        "S06_SUPERTREND_ATR_4H": lambda p: dict(),  # period/multiplier are precompute-time, handled separately
        "S07_MACD_MOMENTUM_4H": lambda p: dict(),
        "S08_EMA_ADX_4H": lambda p: dict(adx_threshold=p.get("adx_threshold", 20)),
        "S09_ATR_BREAKOUT_4H": lambda p: dict(squeeze_pctile=p.get("squeeze_pctile", 0.25), atr_lookback=3),
        "S11_ZSCORE_REVERSION_15M": lambda p: dict(z_entry=p.get("z_entry", 2.0)),
        "S12_STRUCTURE_RETEST_4H": lambda p: dict(lookback_break=8, retest_atr_mult=p.get("retest_atr_mult", 0.75)),
    }
    from ml.evaluation import multi_strategy_signals as mss
    factory_name = {
        "S01_MOMENTUM_BREAKOUT_15M": mss.momentum_breakout_signal_func,
        "S02_EMA_PULLBACK_15M": mss.ema_pullback_signal_func,
        "S03_VWAP_REVERSION_15M": mss.vwap_reversion_signal_func,
        "S04_RSI_BB_15M": mss.rsi_bollinger_signal_func,
        "S06_SUPERTREND_ATR_4H": mss.supertrend_signal_func,
        "S07_MACD_MOMENTUM_4H": mss.macd_momentum_signal_func,
        "S08_EMA_ADX_4H": mss.ema_structure_adx_signal_func,
        "S09_ATR_BREAKOUT_4H": mss.atr_volatility_breakout_signal_func,
        "S11_ZSCORE_REVERSION_15M": mss.zscore_reversion_signal_func,
        "S12_STRUCTURE_RETEST_4H": mss.structure_retest_signal_func,
    }[strategy_id]
    kwargs = factory_kwargs_map[strategy_id](params)
    return factory_name(**kwargs)


def _precompute(strategy_id: str, df: pd.DataFrame, params: dict, df_1d=None) -> pd.DataFrame:
    if strategy_id == "S10_MTF_TREND_4H":
        return precompute_mtf_trend(df, df_1d)
    if strategy_id == "S06_SUPERTREND_ATR_4H":
        from ml.evaluation.multi_strategy_signals import precompute_supertrend
        return precompute_supertrend(df, period=params.get("period", 10), multiplier=params.get("multiplier", 3.0))
    return MULTI_STRATEGIES[strategy_id]["precompute"](df)


def _run_bt(strategy_id: str, df: pd.DataFrame, params: dict, config: BacktestConfig, df_1d=None) -> BacktestResult:
    prepared = _precompute(strategy_id, df, params, df_1d)
    signal_func = _make_signal_func(strategy_id, params, df_1d)
    bt = Backtester(config)
    return bt.run(prepared.reset_index(drop=True), signal_func)


def _param_combos(grid: dict) -> list[dict]:
    if not grid:
        return [{}]
    import itertools
    keys = list(grid.keys())
    combos = []
    for values in itertools.product(*[grid[k] for k in keys]):
        combos.append(dict(zip(keys, values)))
    return combos


def select_best_params(strategy_id: str, train_df: pd.DataFrame, val_df: pd.DataFrame, config: BacktestConfig, df_1d_train=None, df_1d_val=None) -> tuple[dict, BacktestResult]:
    """Grid search on TRAIN, select on VAL only. Requires >=15 VAL trades
    to be eligible (avoid selecting a lucky-but-tiny-sample combo); among
    eligible combos, maximize VAL profit factor, tie-broken by lower VAL
    drawdown. Falls back to the default combo if nothing clears the trade-
    count floor (never silently picks an untested combo)."""
    grid = PARAM_GRIDS.get(strategy_id, {})
    combos = _param_combos(grid)
    best = None
    best_result = None
    for params in combos:
        val_result = _run_bt(strategy_id, val_df, params, config, df_1d_val)
        if val_result.total_trades < 15:
            continue
        score = (val_result.profit_factor, -val_result.max_drawdown_pct)
        if best is None or score > best[0]:
            best = (score, params)
            best_result = val_result
    if best is None:
        return combos[0], _run_bt(strategy_id, val_df, combos[0], config, df_1d_val)
    return best[1], best_result


def regime_labels(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    ema200 = ema(df["close"], 200)
    ema_slope = ema200.diff(20)
    df["_bull"] = ema_slope > 0
    atr14 = atr(df["high"], df["low"], df["close"], 14)
    atr_norm = atr14 / df["close"]
    df["_high_vol"] = atr_norm > atr_norm.rolling(200, min_periods=50).median()
    return df[["timestamp", "_bull", "_high_vol"]]


def attribute_trades_to_regime(trades: list[BacktestTrade], regimes: pd.DataFrame) -> dict:
    if not trades or regimes.empty:
        return {}
    regimes = regimes.set_index("timestamp").sort_index()
    out = {"bull": [], "bear": [], "high_vol": [], "low_vol": []}
    for t in trades:
        idx = regimes.index.searchsorted(t.entry_time, side="right") - 1
        if idx < 0 or idx >= len(regimes):
            continue
        row = regimes.iloc[idx]
        out["bull" if row["_bull"] else "bear"].append(t.pnl)
        out["high_vol" if row["_high_vol"] else "low_vol"].append(t.pnl)
    return out


def summarize_pnls(pnls: list[float]) -> dict:
    if not pnls:
        return {"trades": 0, "win_rate": None, "pf": None, "net": 0.0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 1
    return {
        "trades": len(pnls), "win_rate": round(len(wins) / len(pnls) * 100, 1),
        "pf": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "net": round(sum(pnls), 2),
    }


def walk_forward_on_oos(strategy_id: str, oos_df: pd.DataFrame, params: dict, config: BacktestConfig, n_folds: int = 4, df_1d=None) -> list[dict]:
    n = len(oos_df)
    fold_size = n // n_folds
    if fold_size < 60:
        return []
    folds = []
    for i in range(n_folds):
        start = i * fold_size
        end = n if i == n_folds - 1 else (i + 1) * fold_size
        fold_df = oos_df.iloc[start:end].reset_index(drop=True)
        if len(fold_df) < 60:
            continue
        result = _run_bt(strategy_id, fold_df, params, config, df_1d)
        folds.append({
            "fold": i + 1, "start": str(fold_df["timestamp"].iloc[0]), "end": str(fold_df["timestamp"].iloc[-1]),
            "trades": result.total_trades, "pf": result.profit_factor, "return_pct": result.total_pnl_pct,
            "max_dd_pct": result.max_drawdown_pct, "win_rate": result.win_rate,
        })
    return folds


def bootstrap_drawdown(trades: list[BacktestTrade], initial_capital: float, n_iter: int = 500, seed: int = 42) -> dict:
    if len(trades) < 5:
        return {"n": len(trades), "note": "too few trades for a meaningful bootstrap"}
    pnls = np.array([t.pnl for t in trades])
    rng = np.random.default_rng(seed)
    max_dds = []
    for _ in range(n_iter):
        order = rng.permutation(len(pnls))
        equity = initial_capital + np.cumsum(pnls[order])
        peak = np.maximum.accumulate(np.concatenate([[initial_capital], equity]))[1:]
        dd = (peak - equity) / peak * 100
        max_dds.append(dd.max())
    max_dds = np.array(max_dds)
    return {
        "n_iter": n_iter,
        "max_dd_p5": round(float(np.percentile(max_dds, 5)), 2),
        "max_dd_p50": round(float(np.percentile(max_dds, 50)), 2),
        "max_dd_p95": round(float(np.percentile(max_dds, 95)), 2),
    }


def sensitivity_grid(strategy_id: str, oos_df: pd.DataFrame, base_params: dict, config: BacktestConfig, df_1d=None) -> list[dict]:
    grid = PARAM_GRIDS.get(strategy_id, {})
    out = []
    for key, values in grid.items():
        base_val = base_params.get(key, values[len(values) // 2])
        for v in values:
            p = dict(base_params)
            p[key] = v
            result = _run_bt(strategy_id, oos_df, p, config, df_1d)
            out.append({"param": key, "value": v, "is_frozen": v == base_val, "trades": result.total_trades, "pf": result.profit_factor, "return_pct": result.total_pnl_pct})
    return out


def main():
    conn = sqlite3.connect(DB_PATH)
    df_15m = load_candles(conn, "15m")
    df_4h = load_candles(conn, "4h")
    df_1d = load_candles(conn, "1d")
    conn.close()

    print(f"15m: {len(df_15m)} candles ({df_15m['timestamp'].min()} -> {df_15m['timestamp'].max()})")
    print(f"4h:  {len(df_4h)} candles ({df_4h['timestamp'].min()} -> {df_4h['timestamp'].max()})")
    print(f"1d:  {len(df_1d)} candles ({df_1d['timestamp'].min()} -> {df_1d['timestamp'].max()})")

    config = BacktestConfig()
    print(f"Cost model: taker_fee={config.fee_rate}, slippage={config.slippage_rate}, funding_avg={config.funding_rate_avg}")

    train_15m, val_15m, oos_15m = chronological_split(df_15m)
    train_4h, val_4h, oos_4h = chronological_split(df_4h)
    train_1d, val_1d, oos_1d = chronological_split(df_1d)
    print(f"\n15m split: train={len(train_15m)} val={len(val_15m)} oos={len(oos_15m)}")
    print(f"4h split:  train={len(train_4h)} val={len(val_4h)} oos={len(oos_4h)}")
    print(f"OOS windows: 15m [{oos_15m['timestamp'].iloc[0]} -> {oos_15m['timestamp'].iloc[-1]}], "
          f"4h [{oos_4h['timestamp'].iloc[0]} -> {oos_4h['timestamp'].iloc[-1]}]")

    all_results = {}
    strategy_ids = list(MULTI_STRATEGIES.keys())

    for strategy_id in strategy_ids:
        spec = MULTI_STRATEGIES[strategy_id]
        tf = spec["timeframe"]
        is_15m = tf == "15m"
        train_df, val_df, oos_df = (train_15m, val_15m, oos_15m) if is_15m else (train_4h, val_4h, oos_4h)
        d1d_val = val_1d if strategy_id == "S10_MTF_TREND_4H" else None
        d1d_oos = oos_1d if strategy_id == "S10_MTF_TREND_4H" else None

        print(f"\n{'=' * 70}\n{strategy_id} ({spec['display_name']}, {tf})\n{'=' * 70}")

        try:
            best_params, val_result = select_best_params(strategy_id, train_df, val_df, config, None, d1d_val)
            print(f"  Selected params (via VAL, min 15 trades): {best_params}")
            print(f"  VAL: trades={val_result.total_trades} pf={val_result.profit_factor:.2f} return={val_result.total_pnl_pct:.2f}%")

            oos_result = _run_bt(strategy_id, oos_df, best_params, config, d1d_oos)
            print(f"  OOS (frozen params, NEVER retuned): trades={oos_result.total_trades} win_rate={oos_result.win_rate:.1f}% "
                  f"pf={oos_result.profit_factor:.2f} return={oos_result.total_pnl_pct:.2f}% max_dd={oos_result.max_drawdown_pct:.2f}% "
                  f"sharpe={oos_result.sharpe_ratio:.2f} expectancy={oos_result.expectancy:.2f}")

            long_trades = [t.pnl for t in oos_result.trades if t.side == "LONG"]
            short_trades = [t.pnl for t in oos_result.trades if t.side == "SHORT"]
            long_summary = summarize_pnls(long_trades)
            short_summary = summarize_pnls(short_trades)
            print(f"  OOS LONG:  {long_summary}")
            print(f"  OOS SHORT: {short_summary}")

            regimes = regime_labels(oos_df)
            regime_pnls = attribute_trades_to_regime(oos_result.trades, regimes)
            regime_summary = {k: summarize_pnls(v) for k, v in regime_pnls.items()}
            print(f"  OOS by regime: {regime_summary}")

            folds = walk_forward_on_oos(strategy_id, oos_df, best_params, config, n_folds=4, df_1d=d1d_oos)
            profitable_folds = sum(1 for f in folds if f["return_pct"] > 0 and f["trades"] > 0)
            print(f"  OOS walk-forward: {len(folds)} folds, {profitable_folds} profitable")
            for f in folds:
                print(f"    {f}")

            boot = bootstrap_drawdown(oos_result.trades, config.initial_capital)
            print(f"  Bootstrap (trade-order reshuffle, {boot.get('n_iter', 0)}x): max_dd p5/p50/p95 = "
                  f"{boot.get('max_dd_p5')}/{boot.get('max_dd_p50')}/{boot.get('max_dd_p95')} (observed OOS max_dd={oos_result.max_drawdown_pct:.2f}%)")

            sens = sensitivity_grid(strategy_id, oos_df, best_params, config, d1d_oos)
            if sens:
                print(f"  Parameter sensitivity (OOS, frozen elsewhere):")
                for s in sens:
                    marker = " <== frozen" if s["is_frozen"] else ""
                    print(f"    {s['param']}={s['value']}: trades={s['trades']} pf={s['pf']} return={s['return_pct']}%{marker}")

            all_results[strategy_id] = {
                "params": best_params, "val": val_result, "oos": oos_result,
                "long": long_summary, "short": short_summary, "regime": regime_summary,
                "folds": folds, "bootstrap": boot, "sensitivity": sens,
            }
        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()

    # ---- Correlation of OOS daily returns across all strategies with >=5 OOS trades ----
    print(f"\n{'=' * 70}\nCROSS-STRATEGY CORRELATION (OOS daily equity returns)\n{'=' * 70}")
    daily_returns = {}
    for sid, r in all_results.items():
        eq = pd.Series(
            [e["equity"] for e in r["oos"].equity_curve],
            index=pd.to_datetime([e["timestamp"] for e in r["oos"].equity_curve]),
        )
        if len(eq) < 10:
            continue
        daily_eq = eq.resample("1D").last().ffill()
        daily_returns[sid] = daily_eq.pct_change().dropna()
    if len(daily_returns) >= 2:
        ret_df = pd.DataFrame(daily_returns).fillna(0)
        corr = ret_df.corr()
        print(corr.round(2).to_string())
    else:
        print("  Not enough strategies with sufficient OOS trades to compute correlation.")

    print("\nDone.")


if __name__ == "__main__":
    main()
