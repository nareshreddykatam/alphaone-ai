from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class OHLCV:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str
    symbol: str


@dataclass
class FundingRate:
    timestamp: datetime
    rate: float
    symbol: str


@dataclass
class OpenInterest:
    timestamp: datetime
    value: float
    symbol: str


@dataclass
class Liquidation:
    timestamp: datetime
    side: str
    price: float
    quantity: float
    symbol: str


@dataclass
class OrderBookLevel:
    price: float
    quantity: float


@dataclass
class OrderBook:
    timestamp: datetime
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    symbol: str


class ExchangeDataUnavailable(Exception):
    """Raised when a RETRYABLE exchange call fails after all retries are
    exhausted. Distinct from "no data exists" (an empty list) -- callers
    must not treat this the same as a legitimately empty historical range.
    """


class ExchangeCapabilityUnsupported(Exception):
    """Raised immediately (no retries) when an exchange implementation does
    not support this call at all -- e.g. Binance has no historical
    liquidations endpoint. Kept exchange-agnostic here (not in a concrete
    exchange module) so ingestion code can catch it without depending on
    any specific exchange implementation.
    """


class ExchangeBase(ABC):
    """Abstract exchange interface for exchange abstraction layer."""

    @abstractmethod
    async def fetch_ohlcv(self, symbol: str, timeframe: str, since: Optional[datetime] = None, limit: int = 1000) -> list[OHLCV]:
        ...

    @abstractmethod
    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        ...

    @abstractmethod
    async def fetch_funding_rate_history(
        self, symbol: str, since: Optional[datetime] = None, limit: int = 1000
    ) -> list[FundingRate]:
        ...

    @abstractmethod
    async def fetch_open_interest(self, symbol: str) -> OpenInterest:
        ...

    @abstractmethod
    async def fetch_open_interest_history(
        self, symbol: str, timeframe: str = "1h", since: Optional[datetime] = None, limit: int = 500
    ) -> list[OpenInterest]:
        ...

    @abstractmethod
    async def fetch_liquidations(self, symbol: str, limit: int = 100) -> list[Liquidation]:
        ...

    @abstractmethod
    async def fetch_order_book(self, symbol: str, limit: int = 20) -> OrderBook:
        ...

    @abstractmethod
    async def fetch_ticker(self, symbol: str) -> dict:
        ...

    @abstractmethod
    async def close(self):
        ...
