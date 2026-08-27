"""Tests for services/common/currency.py: format_inr -- the centralized
Indian-numbering INR formatter used in Telegram output."""
from services.common.currency import format_inr


def test_format_inr_basic():
    assert format_inr(1600) == "₹1,600.00"


def test_format_inr_lakh():
    assert format_inr(100000) == "₹1,00,000.00"


def test_format_inr_ten_lakh():
    assert format_inr(1000000) == "₹10,00,000.00"


def test_format_inr_crore():
    assert format_inr(12345678.9) == "₹1,23,45,678.90"


def test_format_inr_small_value_no_grouping():
    assert format_inr(42.5) == "₹42.50"


def test_format_inr_none_is_na():
    assert format_inr(None) == "N/A"


def test_format_inr_negative():
    assert format_inr(-125) == "-₹125.00"


def test_format_inr_show_sign_positive():
    assert format_inr(250.5, show_sign=True) == "+₹250.50"


def test_format_inr_show_sign_negative_still_shows_minus_not_double_sign():
    assert format_inr(-125, show_sign=True) == "-₹125.00"


def test_format_inr_show_sign_zero_has_no_sign():
    assert format_inr(0, show_sign=True) == "₹0.00"


def test_format_inr_rounds_paise_carry():
    assert format_inr(1.999) == "₹2.00"
