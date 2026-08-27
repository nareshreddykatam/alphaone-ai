"""Phase 4, section 15: categorical signal quality (LOW/MEDIUM/HIGH), not a
raw confidence percentage. Phase 3 found calibration was never validated as
genuinely predictive out-of-sample (see docs/ml_methodology.md and the
Phase 3 report) -- showing "73.2% confidence" on a dashboard would imply a
precision that was explicitly disproven. These thresholds are a coarse,
documented display tier, not a claim of accuracy.
"""

HIGH_THRESHOLD = 0.70
MEDIUM_THRESHOLD = 0.55


def bucket_signal_quality(confidence: float) -> str:
    if confidence >= HIGH_THRESHOLD:
        return "HIGH"
    if confidence >= MEDIUM_THRESHOLD:
        return "MEDIUM"
    return "LOW"
