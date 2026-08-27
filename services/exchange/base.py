"""Exchange-agnostic provider interfaces (Phase 4F). Kept deliberately
separate from `services/market_data` (the Binance research-data
abstraction from Phase 2) -- these interfaces are about a live account the
user manually trades on, not historical research data.

ABSOLUTE CONSTRAINT (Phase 4 spec, section 55 -- "this rule overrides
everything else"): no implementation of ExchangeAccountProvider, anywhere
in this codebase, may EVER define a method that places, cancels, or
modifies an order, or changes leverage/margin mode. AlphaOne must remain
architecturally incapable of automatic trading. This is enforced by
tests/unit/test_no_order_placement_capability.py, which introspects every
concrete subclass -- not just SunCrypto's -- for exactly this.
"""
from abc import ABC, abstractmethod


class ExchangeMarketDataProvider(ABC):
    """Public market data only -- no authentication, no account access."""

    @abstractmethod
    async def get_pairs(self) -> list[dict]:
        ...

    @abstractmethod
    async def get_ticker(self, symbol: str) -> dict:
        ...

    @abstractmethod
    async def get_historical_trades(self, symbol: str) -> list[dict]:
        ...


class ExchangeAccountProvider(ABC):
    """Read-only account access ONLY. See module docstring."""

    @abstractmethod
    async def get_connection_status(self) -> dict:
        ...

    @abstractmethod
    async def get_balance(self) -> dict:
        ...

    @abstractmethod
    async def get_open_positions(self) -> list[dict]:
        ...

    @abstractmethod
    async def get_trade_history(self) -> list[dict]:
        ...
