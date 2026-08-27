"""Tests for BinanceExchange's retry/backoff and UTC-timestamp handling --
the fixes for two Phase 1 audit findings: (1) transient failures were
silently swallowed into an empty list rather than retried, and (2) epoch-ms
timestamps were converted with the local system timezone instead of UTC.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from services.market_data import ExchangeCapabilityUnsupported, ExchangeDataUnavailable
from services.market_data.binance import BinanceExchange, _ms_to_utc_naive
import ccxt


def _make_exchange_with_mock(mock_ccxt_exchange) -> BinanceExchange:
    exchange = BinanceExchange.__new__(BinanceExchange)
    exchange.exchange = mock_ccxt_exchange
    exchange._testnet = False
    return exchange


def test_ms_to_utc_naive_uses_utc_not_local_timezone():
    # 2024-01-01 00:00:00 UTC
    ms = 1704067200000
    result = _ms_to_utc_naive(ms)
    assert result == datetime(2024, 1, 1, 0, 0, 0)
    # Sanity: this must equal the UTC conversion regardless of local tz
    assert result == datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_transient_failure_is_retried_then_succeeds():
    mock = AsyncMock()
    mock.fetch_ohlcv = AsyncMock(side_effect=[
        ConnectionError("simulated network blip"),
        ConnectionError("simulated network blip"),
        [[1704067200000, 100, 101, 99, 100.5, 10]],
    ])
    exchange = _make_exchange_with_mock(mock)

    candles = await exchange.fetch_ohlcv("BTC/USDT", "1h", limit=1)

    assert len(candles) == 1
    assert candles[0].close == 100.5
    assert mock.fetch_ohlcv.await_count == 3


@pytest.mark.asyncio
async def test_permanent_failure_raises_after_exhausting_retries_not_silently_empty():
    mock = AsyncMock()
    mock.fetch_ohlcv = AsyncMock(side_effect=ConnectionError("persistent failure"))
    exchange = _make_exchange_with_mock(mock)

    with pytest.raises(ExchangeDataUnavailable):
        await exchange.fetch_ohlcv("BTC/USDT", "1h", limit=1)

    assert mock.fetch_ohlcv.await_count == 3  # RETRYABLE_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_unsupported_capability_fails_fast_without_retrying():
    mock = AsyncMock()
    mock.fetch_liquidations = AsyncMock(side_effect=ccxt.NotSupported("binance fetchLiquidations() is not supported yet"))
    exchange = _make_exchange_with_mock(mock)

    with pytest.raises(ExchangeCapabilityUnsupported):
        await exchange.fetch_liquidations("BTC/USDT")

    assert mock.fetch_liquidations.await_count == 1  # no retries for a permanently-unsupported call
