from services.signal_engine.quality import bucket_signal_quality


def test_high_medium_low_thresholds():
    assert bucket_signal_quality(0.9) == "HIGH"
    assert bucket_signal_quality(0.70) == "HIGH"
    assert bucket_signal_quality(0.69) == "MEDIUM"
    assert bucket_signal_quality(0.55) == "MEDIUM"
    assert bucket_signal_quality(0.54) == "LOW"
    assert bucket_signal_quality(0.0) == "LOW"
