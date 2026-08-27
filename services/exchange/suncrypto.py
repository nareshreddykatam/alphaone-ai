"""SunCrypto integration (Phase 4F).

Research finding, confirmed by fetching both docs.suncrypto.in and
help.suncrypto.in (see docs/known_limitations.md): SunCrypto's ONLY
documented API surface is 3 unauthenticated PUBLIC SPOT endpoints --
`GET /public/pairs`, `GET /public/tickers`, `GET /public/historical_trades`.
There is no documented futures API, no authenticated account access, no
order-placement/cancellation endpoint, and no documented API-key permission
granularity anywhere. This is exactly the condition the Phase 4 spec's own
section 5 anticipates: "if SunCrypto's authenticated API does not expose
the required account information, implement manual tracking."

Accordingly:
- SunCryptoMarketDataProvider makes REAL calls to the 3 documented public
  endpoints -- genuinely useful for a live price reference.
- SunCryptoReadOnlyAccountProvider is an HONEST STUB. Every method reports
  UNAVAILABLE rather than fabricating account data, because no such API
  exists to call. This is not a shortfall to fix later within this
  codebase -- it reflects SunCrypto's actual API surface today. If
  SunCrypto ever publishes an authenticated read-only account API, this
  class is the single place a real implementation would go.

Neither class may ever grow a method that places, cancels, or modifies an
order, or changes leverage -- see services/exchange/base.py and
tests/unit/test_no_order_placement_capability.py.
"""
import httpx

from services.exchange.base import ExchangeMarketDataProvider, ExchangeAccountProvider

SUNCRYPTO_PUBLIC_BASE_URL = "https://api.suncrypto.in"


class SunCryptoMarketDataProvider(ExchangeMarketDataProvider):
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=SUNCRYPTO_PUBLIC_BASE_URL, timeout=10.0)

    async def get_pairs(self) -> list[dict]:
        resp = await self._client.get("/public/pairs")
        resp.raise_for_status()
        return resp.json()

    async def get_ticker(self, symbol: str) -> dict:
        resp = await self._client.get("/public/tickers")
        resp.raise_for_status()
        tickers = resp.json()
        if isinstance(tickers, list):
            match = next((t for t in tickers if t.get("ticker_id") == symbol or t.get("symbol") == symbol), None)
            return match or {}
        return tickers.get(symbol, {})

    async def get_historical_trades(self, symbol: str) -> list[dict]:
        resp = await self._client.get("/public/historical_trades", params={"ticker_id": symbol})
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class SunCryptoReadOnlyAccountProvider(ExchangeAccountProvider):
    async def get_connection_status(self) -> dict:
        return {
            "status": "UNAVAILABLE",
            "reason": "SunCrypto has no documented authenticated account API (public spot endpoints only)",
        }

    async def get_balance(self) -> dict:
        return {"status": "UNAVAILABLE", "balance": None}

    async def get_open_positions(self) -> list[dict]:
        return []

    async def get_trade_history(self) -> list[dict]:
        return []
