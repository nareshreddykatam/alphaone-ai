"""Multi-Coin AI Futures System, Phases 24-25: the external Telegram
signal parser is a pure function -- every case here is a real example
message shape, never a fabricated "received" message. A vague message
("BTC LONG", "Buy BTC", "BTC long soon") must NEVER become a trade."""
import pytest

from services.telegram_signals.parser import parse_external_signal, normalize_symbol_string

SUPPORTED = {"BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"}


def test_valid_long_colon_format():
    msg = "BTCUSDT LONG\nEntry: 81300\nSL: 79000\nTP1: 83000\nTP2: 84500"
    r = parse_external_signal(msg, SUPPORTED)
    assert r.status == "VALID"
    assert r.symbol == "BTC/USDT"
    assert r.direction == "LONG"
    assert r.entry_price == 81300.0
    assert r.stop_loss == 79000.0
    assert r.take_profit_1 == 83000.0
    assert r.take_profit_2 == 84500.0
    assert r.take_profit_3 is None


def test_valid_short_word_format():
    msg = "BTC/USDT SHORT\nEntry 81300\nStop Loss 82000\nTarget 1 80000\nTarget 2 79000"
    r = parse_external_signal(msg, SUPPORTED)
    assert r.status == "VALID"
    assert r.direction == "SHORT"
    assert r.stop_loss == 82000.0
    assert r.take_profit_1 == 80000.0
    assert r.take_profit_2 == 79000.0


@pytest.mark.parametrize("vague", ["BTC LONG", "Buy BTC", "BTC long soon", "going long soon on btc", ""])
def test_vague_messages_never_become_trades(vague):
    r = parse_external_signal(vague, SUPPORTED)
    assert r.status != "VALID"


def test_missing_entry_is_incomplete_never_guessed():
    msg = "BTC/USDT LONG\nSL: 79000\nTP1: 83000"
    r = parse_external_signal(msg, SUPPORTED)
    assert r.status == "INCOMPLETE"
    assert "entry" in r.rejection_reason.lower()


def test_missing_sl_is_incomplete_never_guessed():
    msg = "BTC/USDT LONG\nEntry: 81300\nTP1: 83000"
    r = parse_external_signal(msg, SUPPORTED)
    assert r.status == "INCOMPLETE"
    assert "stop_loss" in r.rejection_reason.lower()


def test_missing_tp_is_incomplete_never_guessed():
    msg = "BTC/USDT LONG\nEntry: 81300\nSL: 79000"
    r = parse_external_signal(msg, SUPPORTED)
    assert r.status == "INCOMPLETE"


def test_malformed_numbers_do_not_crash_and_are_incomplete():
    msg = "BTC/USDT LONG\nEntry: not-a-number\nSL: 79000\nTP1: 83000"
    r = parse_external_signal(msg, SUPPORTED)
    assert r.status == "INCOMPLETE"  # entry failed to parse -> treated as missing, never guessed


def test_invalid_long_sl_above_entry_is_rejected():
    """A LONG needs SL below entry -- an SL above entry is a structurally
    broken signal, never silently accepted."""
    msg = "BTC/USDT LONG\nEntry: 80000\nSL: 81000\nTP1: 83000"
    r = parse_external_signal(msg, SUPPORTED)
    assert r.status == "INVALID"


def test_invalid_short_sl_below_entry_is_rejected():
    msg = "BTC/USDT SHORT\nEntry: 80000\nSL: 79000\nTP1: 78000"
    r = parse_external_signal(msg, SUPPORTED)
    assert r.status == "INVALID"


def test_invalid_tp_ordering_long_tp_below_entry_is_rejected():
    msg = "BTC/USDT LONG\nEntry: 80000\nSL: 79000\nTP1: 79500"
    r = parse_external_signal(msg, SUPPORTED)
    assert r.status == "INVALID"


def test_invalid_tp_ordering_short_tp_above_entry_is_rejected():
    msg = "BTC/USDT SHORT\nEntry: 80000\nSL: 81000\nTP1: 80500"
    r = parse_external_signal(msg, SUPPORTED)
    assert r.status == "INVALID"


def test_ambiguous_conflicting_directions_rejected():
    msg = "BTC/USDT LONG SHORT\nEntry: 80000\nSL: 79000\nTP1: 83000"
    r = parse_external_signal(msg, SUPPORTED)
    assert r.status == "AMBIGUOUS"


def test_unsupported_symbol_not_in_whitelist_never_traded():
    msg = "DOGEUSDT LONG\nEntry: 0.1\nSL: 0.09\nTP1: 0.12"
    r = parse_external_signal(msg, SUPPORTED)
    assert r.status == "UNSUPPORTED_SYMBOL"
    assert r.symbol == "DOGE/USDT"


@pytest.mark.parametrize("raw,expected", [
    ("BTCUSDT", "BTC/USDT"), ("BTC/USDT", "BTC/USDT"), ("BTC-USDT", "BTC/USDT"),
    ("btc usdt", "BTC/USDT"), ("ETHUSDT", "ETH/USDT"),
])
def test_symbol_normalization_variants(raw, expected):
    assert normalize_symbol_string(raw) == expected


def test_buy_and_sell_keywords_normalize_to_long_short():
    buy_msg = "BTC/USDT BUY\nEntry: 80000\nSL: 79000\nTP1: 83000"
    sell_msg = "BTC/USDT SELL\nEntry: 80000\nSL: 81000\nTP1: 78000"
    assert parse_external_signal(buy_msg, SUPPORTED).direction == "LONG"
    assert parse_external_signal(sell_msg, SUPPORTED).direction == "SHORT"


def test_leverage_and_timeframe_are_extracted_when_present():
    msg = "BTC/USDT LONG 10x 4h\nEntry: 80000\nSL: 79000\nTP1: 83000"
    r = parse_external_signal(msg, SUPPORTED)
    assert r.leverage_stated == 10
    assert r.timeframe_stated == "4h"


def test_three_targets_all_extracted():
    msg = "BTC/USDT LONG\nEntry: 80000\nSL: 79000\nTP1: 81000\nTP2: 82000\nTP3: 83000"
    r = parse_external_signal(msg, SUPPORTED)
    assert r.take_profit_1 == 81000.0
    assert r.take_profit_2 == 82000.0
    assert r.take_profit_3 == 83000.0


def test_bare_tp_label_treated_as_tp1_when_no_numbered_tp_present():
    msg = "BTC/USDT LONG\nEntry: 80000\nSL: 79000\nTP: 83000"
    r = parse_external_signal(msg, SUPPORTED)
    assert r.status == "VALID"
    assert r.take_profit_1 == 83000.0
