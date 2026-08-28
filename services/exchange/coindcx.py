"""CoinDCX integration (Phase 5). Endpoints, request/response fields, and
the authentication scheme below were verified against the OFFICIAL docs at
https://docs.coindcx.com/ (fetched directly, not inferred from tutorials or
GitHub repos, per the Phase 5 brief) on 2026-08-26. Exact findings:

- Futures instrument identifier format is "B-BTC_USDT" (NOT Binance's
  "BTC/USDT" or "BTCUSDT" -- see normalize_symbol()).
- Auth: header X-AUTH-APIKEY (raw key) + X-AUTH-SIGNATURE
  (hex HMAC-SHA256 of the exact JSON-serialized request body, using the API
  secret as the HMAC key). Every authenticated request is a POST or GET
  with a `timestamp` field in the JSON body (epoch MILLISECONDS -- the
  official code samples all use `int(time.time() * 1000)` despite some
  prose in the docs saying "seconds").
- Wallet endpoint's own docs say to IGNORE the `balance` field, but a real
  connectivity test against a live (funded, flat) account showed
  `balance` holding the real deposited amount while `locked_balance`/
  `cross_order_margin`/`cross_user_margin` were all zero (no open
  positions/orders to lock margin in) -- treating the docs literally would
  have reported $0 equity for an account that genuinely has funds. Per
  the account owner's explicit choice (2026-08-26 real-account
  connectivity test), `balance` is now treated as `total_equity`, and
  `used_margin` = `locked_balance + cross_order_margin + cross_user_margin`
  is reported separately as "margin currently in use," with
  `available_balance = total_equity - used_margin`. See
  docs/coindcx_api_findings.md for the full note.
- The account's real futures wallet is INR-margined, not USDT --
  `DEFAULT_MARGIN_CURRENCY` reflects the account owner's real setup
  (2026-08-26). AlphaOne's BTC/USDT signal/backtest naming is unaffected;
  this only changes which CoinDCX wallet/instrument variant account data
  is queried against.
- No position field reports unrealized PnL directly -- it is computed here
  from active_pos * (mark_price - avg_price), clearly documented as
  AlphaOne-computed, not exchange-reported (see docs/coindcx_api_findings.md).
- No single documented unique id exists per trade fill; idempotent sync
  (services/exchange/coindcx_sync.py) derives one from
  order_id+timestamp+price+quantity+side.

ABSOLUTE CONSTRAINT: this file must NEVER define a method that places,
cancels, modifies, or closes an order, or changes leverage/margin --
enforced by tests/unit/test_no_order_placement_capability.py. The
documented mutating endpoints that MUST NEVER be called from this codebase
are listed (not implemented) in docs/coindcx_api_findings.md for the
record.
"""
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx
import structlog

from services.exchange.base import ExchangeMarketDataProvider, ExchangeAccountProvider

logger = structlog.get_logger()

COINDCX_API_BASE = "https://api.coindcx.com"
COINDCX_PUBLIC_BASE = "https://public.coindcx.com"
DEFAULT_MARGIN_CURRENCY = "INR"


def normalize_symbol(symbol: str, margin_currency: str = DEFAULT_MARGIN_CURRENCY) -> str:
    """AlphaOne's internal symbol format is "BTC/USDT" (Binance-style,
    used throughout Phases 1-4). CoinDCX futures instruments are named
    "B-BTC_USDT". Never assume these are interchangeable."""
    base = symbol.split("/")[0].upper()
    return f"B-{base}_{margin_currency.upper()}"


def denormalize_symbol(instrument: str) -> str:
    """Inverse of normalize_symbol -- "B-BTC_USDT" -> "BTC/USDT", so
    exchange-detected positions are stored using AlphaOne's own canonical
    symbol format everywhere else in the codebase (Trade.symbol, Signal.symbol)."""
    body = instrument[2:] if instrument.startswith("B-") else instrument
    base, _, margin = body.partition("_")
    return f"{base}/{margin}" if margin else body


def _sign(secret: str, body: dict) -> tuple[str, str]:
    """Returns (json_body_string, hex_signature) -- CoinDCX requires the
    signature to be computed over the EXACT byte string sent as the
    request body, so the caller must send this same json_body string, not
    re-serialize the dict."""
    json_body = json.dumps(body, separators=(",", ":"))
    signature = hmac.new(secret.encode(), json_body.encode(), hashlib.sha256).hexdigest()
    return json_body, signature


class CoinDCXAuthError(Exception):
    pass


class CoinDCXMarketDataProvider(ExchangeMarketDataProvider):
    """Public endpoints only -- no API key needed or used."""

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def get_pairs(self) -> list[dict]:
        resp = await self._client.get(
            f"{COINDCX_API_BASE}/exchange/v1/derivatives/futures/data/active_instruments",
            params={"margin_currency_short_name[]": DEFAULT_MARGIN_CURRENCY},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_ticker(self, symbol: str) -> dict:
        """Uses the real-time current-prices endpoint (recommended by
        CoinDCX's own docs over REST candles for anything time-sensitive).
        Unlike account-data calls, this looks up a specific public market
        pair, so the instrument's margin currency must come from the
        symbol's own quote (e.g. "BTC/USDT" -> "USDT"), not
        DEFAULT_MARGIN_CURRENCY -- that default reflects the connected
        account's INR wallet and is irrelevant to which market is being
        priced."""
        instrument = normalize_symbol(symbol, margin_currency=symbol.split("/")[1])
        resp = await self._client.get(f"{COINDCX_PUBLIC_BASE}/market_data/v3/current_prices/futures/rt")
        resp.raise_for_status()
        data = resp.json()
        return data.get("prices", {}).get(instrument, {})

    async def get_historical_trades(self, symbol: str) -> list[dict]:
        instrument = normalize_symbol(symbol)
        resp = await self._client.get(
            f"{COINDCX_API_BASE}/exchange/v1/derivatives/futures/data/trades",
            params={"pair": instrument},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_candles(self, symbol: str, resolution: str, from_ts: int, to_ts: int) -> list[dict]:
        """REST fallback only -- CoinDCX's own docs recommend the futures
        WebSocket for candlestick data (see services/exchange/coindcx_ws.py).
        resolution: '1', '5', '60', or '1D' (CoinDCX's own REST vocabulary,
        narrower than the WebSocket's)."""
        instrument = normalize_symbol(symbol)
        resp = await self._client.get(
            f"{COINDCX_PUBLIC_BASE}/market_data/candlesticks",
            params={"pair": instrument, "from": from_ts, "to": to_ts, "resolution": resolution, "pcode": "f"},
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class CoinDCXReadOnlyAccountProvider(ExchangeAccountProvider):
    """Every method here is a read operation against CoinDCX's documented
    authenticated futures endpoints. No credentials -> every method
    reports NOT_CONFIGURED rather than raising, so the app (and the test
    suite -- see Phase 5 section 43) works without real API keys."""

    def __init__(self, api_key: str = "", api_secret: str = "", client: Optional[httpx.AsyncClient] = None):
        self._api_key = api_key
        self._api_secret = api_secret
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=10.0)

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key and self._api_secret)

    async def _post(self, path: str, body: dict) -> httpx.Response:
        if not self.is_configured:
            raise CoinDCXAuthError("COINDCX_API_KEY/COINDCX_API_SECRET not configured")
        body = {**body, "timestamp": int(time.time() * 1000)}
        json_body, signature = _sign(self._api_secret, body)
        headers = {
            "Content-Type": "application/json",
            "X-AUTH-APIKEY": self._api_key,
            "X-AUTH-SIGNATURE": signature,
        }
        # Never log headers/body here -- would leak the signature and, if a
        # future caller ever puts the secret in the body, the secret too.
        return await self._client.post(f"{COINDCX_API_BASE}{path}", content=json_body, headers=headers)

    async def _get(self, path: str, body: dict) -> httpx.Response:
        if not self.is_configured:
            raise CoinDCXAuthError("COINDCX_API_KEY/COINDCX_API_SECRET not configured")
        body = {**body, "timestamp": int(time.time() * 1000)}
        json_body, signature = _sign(self._api_secret, body)
        headers = {
            "Content-Type": "application/json",
            "X-AUTH-APIKEY": self._api_key,
            "X-AUTH-SIGNATURE": signature,
        }
        # httpx's `.get()` shorthand doesn't accept a body; CoinDCX's GET
        # endpoints (e.g. wallets) require one per their own docs, so this
        # must go through `.request()` instead.
        return await self._client.request("GET", f"{COINDCX_API_BASE}{path}", content=json_body, headers=headers)

    async def get_connection_status(self) -> dict:
        if not self.is_configured:
            return {"status": "NOT_CONFIGURED", "reason": "COINDCX_API_KEY/COINDCX_API_SECRET not set"}
        try:
            resp = await self._get("/exchange/v1/derivatives/futures/wallets", {})
            resp.raise_for_status()
            return {"status": "OK"}
        except CoinDCXAuthError as e:
            return {"status": "NOT_CONFIGURED", "reason": str(e)}
        except httpx.HTTPStatusError as e:
            status = "AUTH_FAILURE" if e.response.status_code == 401 else "API_FAILURE"
            return {"status": status, "reason": f"HTTP {e.response.status_code}"}
        except httpx.HTTPError as e:
            return {"status": "CONNECTION_LOST", "reason": str(e)}

    async def get_balance(self) -> dict:
        """Returns normalized {status, total_equity, available_balance,
        used_margin, raw}. `total_equity` = the wallet's own `balance`
        field (real deposited funds); `used_margin` =
        `locked_balance + cross_order_margin + cross_user_margin` (margin
        currently locked in isolated/cross orders and positions);
        `available_balance` = `total_equity - used_margin`. See the module
        docstring for why this deviates from CoinDCX's own "ignore
        balance" doc note -- confirmed against a real account."""
        if not self.is_configured:
            return {"status": "NOT_CONFIGURED", "total_equity": None, "available_balance": None, "used_margin": None}
        try:
            resp = await self._get("/exchange/v1/derivatives/futures/wallets", {})
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("CoinDCX get_balance failed", error=str(e))
            return {"status": "UNAVAILABLE", "total_equity": None, "available_balance": None, "used_margin": None}

        wallets = resp.json()
        wallet = next((w for w in wallets if w.get("currency_short_name") == DEFAULT_MARGIN_CURRENCY), None)
        if wallet is None:
            return {"status": "UNAVAILABLE", "total_equity": None, "available_balance": None, "used_margin": None}

        total_equity = float(wallet.get("balance", 0) or 0)
        locked = float(wallet.get("locked_balance", 0) or 0)
        order_margin = float(wallet.get("cross_order_margin", 0) or 0)
        user_margin = float(wallet.get("cross_user_margin", 0) or 0)
        used_margin = locked + order_margin + user_margin

        return {
            "status": "OK",
            "total_equity": total_equity,
            "available_balance": total_equity - used_margin,
            "used_margin": used_margin,
            "raw": wallet,
        }

    async def get_open_positions(self) -> list[dict]:
        """Normalized: symbol, side, quantity, entry_price, mark_price,
        liquidation_price, leverage, unrealized_pnl (COMPUTED, see module
        docstring), margin, exchange_position_id. Filters to active_pos !=
        0 -- CoinDCX returns a row per pair regardless of whether a
        position is actually open."""
        if not self.is_configured:
            return []
        try:
            resp = await self._post(
                "/exchange/v1/derivatives/futures/positions",
                {"page": "1", "size": "50", "margin_currency_short_name": [DEFAULT_MARGIN_CURRENCY]},
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("CoinDCX get_open_positions failed", error=str(e))
            return []

        positions = []
        for p in resp.json():
            active_pos = float(p.get("active_pos", 0) or 0)
            if active_pos == 0:
                continue
            avg_price = float(p.get("avg_price", 0) or 0)
            mark_price = float(p.get("mark_price", 0) or 0)
            side = "LONG" if active_pos > 0 else "SHORT"
            unrealized_pnl = (mark_price - avg_price) * active_pos if mark_price else None

            positions.append({
                "exchange_position_id": p.get("id"),
                "symbol": p.get("pair"),
                "side": side,
                "quantity": abs(active_pos),
                "entry_price": avg_price,
                "mark_price": mark_price or None,
                "liquidation_price": float(p.get("liquidation_price", 0) or 0) or None,
                "leverage": p.get("leverage"),
                "margin": float(p.get("locked_margin", 0) or 0),
                "margin_type": p.get("margin_type"),
                "unrealized_pnl": unrealized_pnl,
                "updated_at": p.get("updated_at"),
            })
        return positions

    async def get_trade_history(
        self, symbol: str = "BTC/USDT", from_date: str = "", to_date: str = "", lookback_days: int = 30
    ) -> list[dict]:
        """`from_date`/`to_date` are mandatory per CoinDCX's docs (format
        YYYY-MM-DD) -- default to the trailing `lookback_days` if the
        caller doesn't specify a range, since the shared ABC's
        `get_trade_history()` takes no arguments."""
        if not self.is_configured:
            return []
        instrument = normalize_symbol(symbol)
        if not to_date:
            to_date = datetime.utcnow().strftime("%Y-%m-%d")
        if not from_date:
            from_date = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        body = {
            "pair": instrument, "from_date": from_date, "to_date": to_date, "page": "1", "size": "100",
            "margin_currency_short_name": [DEFAULT_MARGIN_CURRENCY],
        }
        try:
            resp = await self._post("/exchange/v1/derivatives/futures/trades", body)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("CoinDCX get_trade_history failed", error=str(e))
            return []
        return resp.json()

    async def get_transactions(self, stage: str = "all") -> list[dict]:
        """Position-level PnL/funding transactions (CoinDCX's "Get
        Transactions" endpoint) -- not part of the shared ABC since it's a
        CoinDCX-specific capability, but a real read-only method."""
        if not self.is_configured:
            return []
        try:
            resp = await self._post(
                "/exchange/v1/derivatives/futures/positions/transactions",
                {"stage": stage, "page": "1", "size": "100", "margin_currency_short_name": [DEFAULT_MARGIN_CURRENCY]},
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("CoinDCX get_transactions failed", error=str(e))
            return []
        return resp.json()

    async def get_orders(self, status: str = "open,filled,cancelled") -> list[dict]:
        """Read-only "List Orders". CoinDCX requires `side` as a mandatory
        single value (buy OR sell) -- there is no documented "both" option,
        so this queries each side and merges the results."""
        if not self.is_configured:
            return []
        orders: list[dict] = []
        for side in ("buy", "sell"):
            try:
                resp = await self._post(
                    "/exchange/v1/derivatives/futures/orders",
                    {"status": status, "side": side, "page": "1", "size": "50",
                     "margin_currency_short_name": [DEFAULT_MARGIN_CURRENCY]},
                )
                resp.raise_for_status()
                orders.extend(resp.json())
            except httpx.HTTPError as e:
                logger.warning("CoinDCX get_orders failed", side=side, error=str(e))
        return orders

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
