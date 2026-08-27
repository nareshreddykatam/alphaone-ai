# Phase 3 ML methodology

Source of truth for the label design, feature groups, calibration, and
walk-forward methodology used in Phase 3. See `docs/known_limitations.md`
for what this does NOT cover (OI/liquidation data constraints, the
kill-switch caveat inherited from Phase 2.6, etc).

## Primary timeframe

**4h**, per the Phase 3 brief -- the strongest simple baseline (Donchian +
ADX, Phase 2.6) was found there. 1h features are merged in as context (see
below); this is a choice made explicit and testable, not an unexamined
assumption -- `ml/evaluation/ml_pipeline.py`'s ablation screen runs the
same feature/model combinations that could, in principle, be re-run on 1h
or 15m primary timeframes in a future phase to check whether 4h is
actually best.

## Label design: triple barrier, not "did price go up"

`ml/labeling.py: compute_triple_barrier_labels`. At each bar T:

1. A hypothetical LONG and a hypothetical SHORT are both entered at bar
   T+1's open (matching the backtester's own next-bar execution --
   `docs/execution_semantics.md`).
2. Each gets a stop and target sized off **ATR(T)** (known at T, not
   future) -- default 2x ATR target, 1x ATR stop (2:1 reward:risk).
3. Both hypothetical trades are simulated forward up to `horizon_bars`
   bars (default 12 = 2 days on 4h) to see which barrier is hit first.
4. Label = LONG if the long hypothetical would hit its target before its
   stop; SHORT for the mirrored case; **NO_TRADE** if neither resolves
   favorably, if both do (an ambiguous whipsaw), or if the configured
   minimum risk:reward isn't met.

This directly answers "was there a favorable risk-adjusted trading
opportunity," not "did the next candle close up." A row with insufficient
forward data to evaluate the full horizon is dropped (same as any forward-
looking label near the end of a dataset).

**Strict feature/label separation** (Phase 3 section 9): the label
function only ever adds `label*` columns; it never touches or computes a
feature. The barrier *width* is read from `atr_14` at bar T (already
proven causal -- `tests/leakage/test_no_lookahead_features.py`); only the
barrier *outcome* looks forward, which is what makes it a label.
`tests/leakage/test_label_leakage.py` and
`tests/unit/test_labeling.py::test_label_only_uses_the_atr_at_time_t_not_a_future_atr`
enforce this.

## Feature groups (ablation axes)

`ml/features/feature_groups.py`. Five information-type groups plus a
timeframe-context group, filtered to whichever columns actually exist in
a given dataset:

| Group | Contents |
|---|---|
| trend | EMAs/SMAs, price-vs-MA, EMA-relationship flags, `trend_slope_20`, returns |
| momentum | RSI, MACD, ROC, momentum, ADX, +DI/-DI |
| volatility | ATR, ATR%, realized vol, BB width, Garman-Klass |
| volume | relative volume, OBV, volume spikes, buy/sell ratio |
| structure | higher-high/low, break-of-structure, near S/R, consolidation, Donchian position |
| regime | one-hot `MarketRegimeDetector` output (8 regimes) |
| derivatives | funding rate features, OI features (**where OI is actually available** -- see below) |
| context_1h | RSI/ADX/EMA-cross/trend/realized-vol pulled from 1h via a backward as-of merge, included in every ablation model as a timeframe-architecture layer, not one of the compared information axes |

**Liquidation features are deliberately excluded** from the derivatives
group -- there is no historical liquidation dataset (see
`docs/known_limitations.md`), so a `liq_spike`/`long_liquidations` column
in training data would be all zeros, i.e. a fabricated feature. This is
excluded rather than filled with zeros that only take real values live.

Ablation configurations tested:
- **A**: trend + momentum + volatility + volume ("technical only")
- **B**: A + structure
- **C**: B + regime
- **D**: C + derivatives

## Multi-timeframe context, causally

`ml/features/feature_groups.py: _merge_context_timeframe` uses
`pd.merge_asof(..., direction="backward")`: each 4h bar gets the most
recent 1h feature reading known at or before its own timestamp, never
after. Verified explicitly in
`tests/leakage/test_feature_groups_no_lookahead.py` (a real bug was found
and fixed here during development: `merge_asof`'s automatic column
suffixing silently collided with the primary dataframe's own same-named
columns, e.g. `rsi_14` existing on both the 4h and 1h sides -- fixed by
renaming context columns before merging instead of relying on `suffixes`).

## Models (the ladder, built incrementally)

`ml/training/trainer.py`. In order: Logistic Regression -> Random Forest
-> XGBoost -> LightGBM -> Ensemble (soft-voting over RF/XGB/LGBM).
Logistic Regression is the only model requiring feature scaling, fit only
on the training split (`ml/features/scaling.fit_transform_train_only`) --
tree models are scale-invariant and use raw features.

**Known gap**: the existing `ModelTrainer.train_ensemble` (Phase 1/2
scaffolding, unchanged in Phase 3) uses `sklearn.VotingClassifier`, which
re-fits clones of each base estimator internally without an eval set --
this breaks LightGBM's early-stopping callback
(`ValueError: Must have at least 1 validation dataset for early stopping`).
The ensemble model could not be evaluated in this phase's ablation screen
for that reason. See `docs/known_limitations.md`.

## Calibration

`ModelTrainer.calibrate_model` wraps an already-fit model in
`CalibratedClassifierCV` with `FrozenEstimator` (scikit-learn 1.6+'s
replacement for the deprecated `cv="prefit"`), fitting the calibration
mapping on the **validation split only**. Both `sigmoid` (Platt) and
`isotonic` are fit on validation and compared by Brier score on that same
validation set; the better one is used for the fold's test evaluation.
Reported per model: Brier score, log loss, and a reliability curve
(`ModelTrainer.calibration_curve_data`).

## Train/validation/test with purge/embargo

`ml/evaluation/ml_pipeline.py: chronological_split_with_embargo` (single
split, used for the ablation screen) and `rolling_walk_forward_windows`
(walk-forward). Both insert an **embargo gap of `horizon_bars + 2` bars**
at every train/val and val/test boundary -- large enough that no training
or validation row's forward-looking label window can extend into the next
split's data. This is strictly larger than the label horizon by
construction; see
`tests/unit/test_ml_pipeline.py::test_embargo_at_least_as_large_as_label_horizon_prevents_overlap`.

No random shuffling occurs anywhere in this pipeline.

## Signal generation: threshold + expected value, never raw probability

`ml/signal.py`. A trade requires **both**:
1. The predicted side's calibrated probability clears
   `MLSignalConfig.probability_threshold` (default 0.45), and
2. The expected value in R-multiples, `P*reward_R - (1-P)*risk_R -
   cost_haircut_R`, clears `min_expected_value_r` (default 0.15) -- using
   the SAME reward:risk ratio the label barriers were built from, so the
   simulated trade actually tests what the model was trained to predict.

NO_TRADE is the default output whenever either condition fails -- it is
not a fallback bug, it is the expected majority outcome (see class
distribution in the Phase 3 report). The threshold/EV parameters are
fixed constants chosen ahead of time, not tuned per fold or on the test
set.

**The model never sees or sets position size, leverage limits, or risk
state** -- `ml/signal.py`'s output dict carries only
`signal_type`/`stop_loss`/`take_profit_1`/`leverage` (a hint, capped by
`RiskConfig.max_leverage` same as every baseline), exactly the same
contract `ml.evaluation.baselines` signal functions use. Sizing is
`RiskEngine.calculate_position_size`'s job, unchanged.
`tests/unit/test_ml_signal.py::test_signal_never_specifies_position_size_or_max_risk`
enforces this at the contract level.

## Cost sensitivity

`ml/evaluation/cost_sensitivity.py` re-runs a candidate at 1x/1.25x/1.5x/2x
the base fee+slippage+funding assumptions -- a fixed, pre-declared ladder,
never the assumption that produces the best-looking number.
