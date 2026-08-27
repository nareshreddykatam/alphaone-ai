from dataclasses import dataclass


@dataclass
class ExchangeSpec:
    """Exchange-specific market parameters used by the backtester.

    These are RESEARCH ASSUMPTIONS, not values pulled from a live exchange
    fee-tier/margin API -- see docs/exchange_assumptions.md for what each
    default is approximating and why. Nothing here should be read as "the
    actual current Binance fee schedule."
    """

    maker_fee: float = 0.0002
    taker_fee: float = 0.0004
    funding_interval_hours: int = 8
    slippage_bps: float = 1.0
    spread_bps: float = 0.5
    tick_size: float = 0.1
    qty_precision: int = 3
    min_qty: float = 0.001
    max_leverage: int = 5
    maintenance_margin_pct: float = 0.5

    @property
    def slippage_rate(self) -> float:
        return self.slippage_bps / 10000

    @property
    def spread_rate(self) -> float:
        return self.spread_bps / 10000

    def round_qty(self, qty: float) -> float:
        rounded = round(qty, self.qty_precision)
        return rounded if rounded >= self.min_qty else 0.0

    def round_price(self, price: float) -> float:
        if self.tick_size <= 0:
            return price
        return round(price / self.tick_size) * self.tick_size
