"""services/live_execution/sizing.py -- exact-precision Rs.200/10x
position sizing (Contract Audit V2, Phase 3). The instrument fixtures
below are REAL snapshots fetched live from
https://api.coindcx.com/exchange/v1/derivatives/futures/data/instrument
for B-BTC_USDT, B-ETH_USDT, B-SOL_USDT, B-XRP_USDT (all USDT-margined)
during this audit on 2026-08-28, and the USDT/INR rate (99.89) is the
real live CoinDCX spot rate fetched the same day -- not invented,
representative values. Prices and leverage caps DO change over time, so
these are a dated real-world snapshot, not a hardcoded permanent claim
about CoinDCX; see docs/coindcx_futures_contract_audit_v2.md for the full
write-up and methodology. The sizing LOGIC these tests exercise is
real and general-purpose; only the fixture data is a point-in-time
snapshot.
"""
from dataclasses import replace

from services.exchange.coindcx_instruments import InstrumentMetadata
from services.live_execution.sizing import calculate_precision_sized_quantity, MAX_MARGIN_DEVIATION_PCT

REAL_USDT_INR_RATE_20260828 = 99.89

# Real live snapshot, 2026-08-28.
BTC_USDT = InstrumentMetadata(
    pair="B-BTC_USDT", status="active", kind="perpetual",
    settle_currency_short_name="USDT", quote_currency_short_name="USDT",
    position_currency_short_name="BTC", underlying_currency_short_name="BTC", margin_currency_short_name="USDT",
    max_leverage_long=20.0, max_leverage_short=20.0, price_increment=0.1, quantity_increment=0.001,
    min_trade_size=0.001, min_price=584.64, max_price=791341.0, min_quantity=0.001, max_quantity=950.0,
    min_notional=60.0, max_notional=0.0, exit_only=False, order_types=[], time_in_force_options=[], fetched_at=0.0,
)
ETH_USDT = InstrumentMetadata(
    pair="B-ETH_USDT", status="active", kind="perpetual",
    settle_currency_short_name="USDT", quote_currency_short_name="USDT",
    position_currency_short_name="ETH", underlying_currency_short_name="ETH", margin_currency_short_name="USDT",
    max_leverage_long=20.0, max_leverage_short=20.0, price_increment=0.01, quantity_increment=0.001,
    min_trade_size=0.001, min_price=41.853, max_price=25013.6, min_quantity=0.001, max_quantity=9500.0,
    min_notional=24.0, max_notional=0.0, exit_only=False, order_types=[], time_in_force_options=[], fetched_at=0.0,
)
SOL_USDT = InstrumentMetadata(
    pair="B-SOL_USDT", status="active", kind="perpetual",
    settle_currency_short_name="USDT", quote_currency_short_name="USDT",
    position_currency_short_name="SOL", underlying_currency_short_name="SOL", margin_currency_short_name="USDT",
    max_leverage_long=5.0, max_leverage_short=5.0, price_increment=0.01, quantity_increment=0.01,
    min_trade_size=0.01, min_price=0.441, max_price=1063.8, min_quantity=0.01, max_quantity=950000.0,
    min_notional=6.0, max_notional=0.0, exit_only=False, order_types=[], time_in_force_options=[], fetched_at=0.0,
)
XRP_USDT = InstrumentMetadata(
    pair="B-XRP_USDT", status="active", kind="perpetual",
    settle_currency_short_name="USDT", quote_currency_short_name="USDT",
    position_currency_short_name="XRP", underlying_currency_short_name="XRP", margin_currency_short_name="USDT",
    max_leverage_long=10.0, max_leverage_short=10.0, price_increment=0.0001, quantity_increment=0.1,
    min_trade_size=0.1, min_price=0.015015, max_price=14.185, min_quantity=0.1, max_quantity=9500000.0,
    min_notional=6.0, max_notional=2400000.0, exit_only=False, order_types=[], time_in_force_options=[], fetched_at=0.0,
)

# Real live mark prices, 2026-08-28.
REAL_PRICES_20260828 = {"BTC": 79133.49, "ETH": 2503.19, "SOL": 106.42, "XRP": 1.4203}


def test_btc_at_real_2026_08_28_price_cannot_hit_exact_rs200_margin_and_is_rejected():
    """REAL FINDING: BTC/USDT's own min_quantity (0.001) alone implies a
    minimum notional far larger than Rs.200/10x can represent at BTC's
    real price -- 0.001 BTC * ~79133 USDT = ~79 USDT notional, i.e.
    ~Rs.790 margin at 10x, nearly 4x the Rs.200 target. This is a real,
    structural infeasibility, not a bug -- BTC/USDT is not currently a
    valid instrument for this system's exact Rs.200 rule."""
    result = calculate_precision_sized_quantity(REAL_PRICES_20260828["BTC"], REAL_USDT_INR_RATE_20260828, BTC_USDT)
    assert result.approved is False


def test_eth_at_real_2026_08_28_price_cannot_hit_exact_rs200_margin_and_is_rejected():
    """REAL FINDING: ETH/USDT's min_notional (24 USDT) alone requires
    ~Rs.2400 margin at 10x (24 USDT * ~99.89 INR/USDT / leverage-adjusted),
    far above the Rs.200 target -- also structurally infeasible today."""
    result = calculate_precision_sized_quantity(REAL_PRICES_20260828["ETH"], REAL_USDT_INR_RATE_20260828, ETH_USDT)
    assert result.approved is False


def test_sol_is_rejected_on_leverage_before_sizing_is_even_attempted():
    """REAL FINDING: SOL/USDT's real max_leverage_long/short is 5.0 today
    -- the required 10x is not supported at all, independent of sizing."""
    result = calculate_precision_sized_quantity(REAL_PRICES_20260828["SOL"], REAL_USDT_INR_RATE_20260828, SOL_USDT, leverage=10)
    assert result.approved is False
    assert "leverage" in result.reason.lower()


def test_xrp_at_real_2026_08_28_price_achieves_close_to_exact_rs200_margin():
    """REAL FINDING: XRP/USDT is the one instrument, of these four, where
    Rs.200/10x is genuinely close to representable -- its quantity_increment
    (0.1) is coarse relative to its low price, so the rounding error stays
    small. This is what the sizing module is FOR: telling BTC/ETH/SOL
    apart from XRP using real constraints, not a hand-wavy "multi-coin
    supported" claim."""
    result = calculate_precision_sized_quantity(REAL_PRICES_20260828["XRP"], REAL_USDT_INR_RATE_20260828, XRP_USDT)
    assert result.approved is True
    assert result.margin_deviation_pct <= MAX_MARGIN_DEVIATION_PCT
    assert abs(result.realized_margin_inr - 200.0) < 20.0  # within the 10% tolerance band


def test_no_instrument_metadata_rejects_rather_than_guessing():
    result = calculate_precision_sized_quantity(80000.0, 88.0, None)
    assert result.approved is False
    assert result.reason


def test_no_usdt_inr_rate_rejects_rather_than_guessing():
    result = calculate_precision_sized_quantity(80000.0, None, BTC_USDT)
    assert result.approved is False
    assert "conversion rate" in result.reason.lower()


def test_zero_usdt_inr_rate_rejects():
    result = calculate_precision_sized_quantity(80000.0, 0.0, BTC_USDT)
    assert result.approved is False


def test_invalid_entry_price_rejects():
    result = calculate_precision_sized_quantity(0.0, 88.0, BTC_USDT)
    assert result.approved is False


def test_exit_only_instrument_rejects_new_entries():
    exit_only = replace(BTC_USDT, exit_only=True)
    result = calculate_precision_sized_quantity(80000.0, 88.0, exit_only)
    assert result.approved is False
    assert "exit_only" in result.reason.lower() or "active" in result.reason.lower()


def test_inactive_status_instrument_rejects_new_entries():
    inactive = replace(BTC_USDT, status="delisted")
    result = calculate_precision_sized_quantity(80000.0, 88.0, inactive)
    assert result.approved is False


def test_fine_grained_instrument_hits_near_exact_margin():
    """Sanity check with a deliberately fine-grained (unrealistic)
    instrument, proving the sizing math itself is correct independent of
    any real instrument's coarseness."""
    fine = replace(BTC_USDT, quantity_increment=0.00000001, min_quantity=0.00000001, min_notional=0.01, max_leverage_long=20.0, max_leverage_short=20.0)
    result = calculate_precision_sized_quantity(80000.0, 88.0, fine)
    assert result.approved is True
    assert result.margin_deviation_pct < 0.1
    assert abs(result.realized_margin_inr - 200.0) < 1.0


def test_quantity_capped_by_max_quantity_is_excluded_as_a_candidate():
    """A pathological instrument whose max_quantity is below even the
    rounded-down candidate must never be silently allowed through."""
    capped = replace(BTC_USDT, quantity_increment=0.001, min_quantity=0.001, max_quantity=0.0001, min_notional=0.01)
    result = calculate_precision_sized_quantity(80000.0, 88.0, capped)
    assert result.approved is False


def test_realized_margin_never_exceeds_the_deviation_tolerance_when_approved():
    for price, instrument in ((REAL_PRICES_20260828["XRP"], XRP_USDT),):
        result = calculate_precision_sized_quantity(price, REAL_USDT_INR_RATE_20260828, instrument)
        if result.approved:
            assert result.margin_deviation_pct <= MAX_MARGIN_DEVIATION_PCT
