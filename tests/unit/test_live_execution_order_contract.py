"""services/live_execution/order_contract.py -- exact CoinDCX futures
request-payload builders (Contract Audit V2, Phases 4-8). Every expected
value here is copied verbatim from the official docs
(https://docs.coindcx.com/, fetched directly 2026-08-28). These builders
are NEVER called by anything capable of sending a real HTTP request --
see tests/unit/test_no_order_placement_capability.py's coverage of this
module for that proof; this file only tests that the payload SHAPE is
correct.
"""
import pytest

from services.live_execution.order_contract import (
    build_create_order_request, build_update_leverage_request, build_create_tpsl_request,
    build_exit_position_request, ORDER_TIMESTAMP_MAX_AGE_SECONDS,
)


def test_create_order_market_long_matches_the_documented_nested_shape():
    """The docs' own sample nests every order field inside body["order"],
    with only "timestamp" at the top level -- a flat body would not match
    the real contract."""
    body = build_create_order_request(
        timestamp_ms=1705647376759, side="buy", pair="B-BTC_USDT", total_quantity=0.001, leverage=10,
    )
    assert body == {
        "timestamp": 1705647376759,
        "order": {
            "side": "buy", "pair": "B-BTC_USDT", "order_type": "market", "price": None,
            "total_quantity": 0.001, "notification": "no_notification", "hidden": False, "post_only": False,
            "margin_currency_short_name": "USDT", "leverage": 10,
        },
    }


def test_create_order_market_short():
    body = build_create_order_request(timestamp_ms=1, side="sell", pair="B-BTC_USDT", total_quantity=0.001, leverage=10)
    assert body["order"]["side"] == "sell"
    assert body["order"]["order_type"] == "market"


def test_create_order_market_never_includes_time_in_force():
    """The docs' own explicit NOTE: 'Do not include time_in_force parameter
    for market orders.'"""
    body = build_create_order_request(
        timestamp_ms=1, side="buy", pair="B-BTC_USDT", total_quantity=0.001, time_in_force="good_till_cancel",
    )
    assert "time_in_force" not in body["order"]


def test_create_order_market_price_is_explicitly_null():
    """The docs: 'Keep this NULL for market orders' -- an explicit null,
    not an omitted key (unlike time_in_force)."""
    body = build_create_order_request(timestamp_ms=1, side="buy", pair="B-BTC_USDT", total_quantity=0.001)
    assert "price" in body["order"]
    assert body["order"]["price"] is None


def test_create_order_limit_includes_price_and_time_in_force():
    body = build_create_order_request(
        timestamp_ms=1, side="buy", pair="B-BTC_USDT", total_quantity=0.001, order_type="limit",
        price=80000.0, time_in_force="good_till_cancel",
    )
    assert body["order"]["price"] == 80000.0
    assert body["order"]["time_in_force"] == "good_till_cancel"


def test_create_order_stop_market_includes_stop_price():
    body = build_create_order_request(
        timestamp_ms=1, side="sell", pair="B-BTC_USDT", total_quantity=0.001, order_type="stop_market", stop_price=79000.0,
    )
    assert body["order"]["stop_price"] == 79000.0


def test_create_order_rejects_an_unknown_side():
    with pytest.raises(ValueError):
        build_create_order_request(timestamp_ms=1, side="hold", pair="B-BTC_USDT", total_quantity=0.001)


def test_create_order_rejects_an_unknown_order_type():
    with pytest.raises(ValueError):
        build_create_order_request(timestamp_ms=1, side="buy", pair="B-BTC_USDT", total_quantity=0.001, order_type="fantasy")


def test_create_order_rejects_an_unknown_time_in_force():
    with pytest.raises(ValueError):
        build_create_order_request(
            timestamp_ms=1, side="buy", pair="B-BTC_USDT", total_quantity=0.001, order_type="limit",
            price=1.0, time_in_force="whenever",
        )


def test_create_order_rejects_an_unknown_position_margin_type():
    with pytest.raises(ValueError):
        build_create_order_request(
            timestamp_ms=1, side="buy", pair="B-BTC_USDT", total_quantity=0.001, position_margin_type="floating",
        )


def test_update_leverage_matches_the_documented_shape_with_pair():
    """The docs' own sample body shows leverage as a STRING ("5"), not an
    integer -- reproduced exactly, not "corrected" to an int."""
    body = build_update_leverage_request(timestamp_ms=1, leverage=10, pair="B-BTC_USDT")
    assert body == {"timestamp": 1, "leverage": "10", "margin_currency_short_name": "USDT", "pair": "B-BTC_USDT"}


def test_update_leverage_matches_the_documented_shape_with_position_id():
    body = build_update_leverage_request(timestamp_ms=1, leverage=10, position_id="pos-123")
    assert body == {"timestamp": 1, "leverage": "10", "margin_currency_short_name": "USDT", "id": "pos-123"}


def test_update_leverage_requires_exactly_one_of_pair_or_position_id():
    with pytest.raises(ValueError):
        build_update_leverage_request(timestamp_ms=1, leverage=10)
    with pytest.raises(ValueError):
        build_update_leverage_request(timestamp_ms=1, leverage=10, pair="B-BTC_USDT", position_id="pos-123")


def test_create_tpsl_matches_the_documented_shape():
    """The docs' own docs: order_type is fixed to take_profit_market /
    stop_market ONLY (limit_price is explicitly unsupported) -- this
    builder never exposes a limit_price parameter at all."""
    body = build_create_tpsl_request(timestamp_ms=1, position_id="pos-123", take_profit_stop_price=83000.0, stop_loss_stop_price=79000.0)
    assert body == {
        "timestamp": 1, "id": "pos-123",
        "take_profit": {"stop_price": "83000.0", "order_type": "take_profit_market"},
        "stop_loss": {"stop_price": "79000.0", "order_type": "stop_market"},
    }


def test_create_tpsl_stop_prices_are_strings_matching_the_documented_sample():
    body = build_create_tpsl_request(timestamp_ms=1, position_id="pos-123", take_profit_stop_price=1.0, stop_loss_stop_price=0.271)
    assert isinstance(body["take_profit"]["stop_price"], str)
    assert isinstance(body["stop_loss"]["stop_price"], str)


def test_exit_position_matches_the_documented_shape():
    """The documented contract takes ONLY timestamp + id -- no quantity
    parameter exists, confirming the exchange has no native partial-exit
    mechanism for futures positions."""
    body = build_exit_position_request(timestamp_ms=1, position_id="pos-123")
    assert body == {"timestamp": 1, "id": "pos-123"}


def test_documented_order_timestamp_rejection_window_is_ten_seconds():
    assert ORDER_TIMESTAMP_MAX_AGE_SECONDS == 10
