# Walk-forward validation methodology

Implemented in `ml/datasets/loader.py: DatasetLoader.create_walk_forward_splits`
and `ml/evaluation/validator.py: WalkForwardValidator`.

## Why walk-forward instead of a single train/val/test split

A single chronological 70/15/15 split (`split_chronological`, also
available and used for quick iteration) tells you how a model trained once
on the past performs on one held-out future window. Markets are
non-stationary, so a single split can be lucky or unlucky. Walk-forward
validation retrains across many rolling windows and reports the
distribution of out-of-sample results, which is a much stronger signal
about whether an approach generalizes.

## Window layout

```
Train Window --> [ EMBARGO ] --> Test Window
                                    |
                                    v  (advance by `step`)
      Train Window --> [ EMBARGO ] --> Test Window
                                          |
                                          v
                                              ...
```

Configurable parameters (`create_walk_forward_splits`):
- `train_window` -- number of bars in each training window
- `test_window` -- number of bars in each out-of-sample test window
- `step` -- how far the whole window slides forward between folds
- `embargo` -- bars deliberately skipped between the end of train and the
  start of test

## Why the embargo exists

Rolling-window features (EMAs, RSI, ATR, realized volatility, etc.) computed
near the train/test boundary would otherwise let information "bleed" across
it: a feature value in the first few test rows could be influenced by
training-window data through its rolling window, giving the model a
subtle, unrealistic edge on those specific rows. The embargo (default 100
bars) guarantees a genuine, feature-window-sized gap between the two.
`tests/leakage/test_split_no_shuffle.py` asserts this gap programmatically
for every fold.

## Per-fold evaluation

For each fold, Phase 2 reports **two independent kinds of metric**, not
just classification accuracy:

1. Classification: accuracy, AUC-ROC on the held-out test window.
2. Trading performance: the trained model's predictions are run through
   `SignalEngine` (so regime-gating and SL/TP construction apply exactly as
   they would live) and then through the same `Backtester` every baseline
   uses, producing real `total_return`, `sharpe`, `max_drawdown`, and
   `total_trades` for that fold (`ml/evaluation/validator.py:
   _make_model_signal_func`). Before this fix, these fields were hardcoded
   to zero and the walk-forward validator only ever reported classification
   metrics -- see the Phase 1 audit.

## What is NOT done

- No hyperparameter tuning across folds. Phase 2's goal is a trustworthy,
  leak-free harness -- not a tuned model. Tuning against walk-forward
  results without a final untouched holdout would itself reintroduce a
  subtle form of overfitting.
- The final, most-recent out-of-sample test period is not used to make any
  modeling decisions. It exists to report a number, not to select a model.
- No chronological shuffling anywhere in this pipeline
  (`tests/leakage/test_split_no_shuffle.py` enforces this).
