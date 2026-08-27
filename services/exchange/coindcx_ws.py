"""CoinDCX WebSocket client (Phase 5, sections 9, 20, 25). Socket.IO
protocol against wss://stream.coindcx.com (NOT a raw websocket -- see
docs/coindcx_api_findings.md). Reconnection/backoff is delegated to
python-socketio's own built-in reconnection support rather than
reimplemented here.

Message-handling is factored into plain methods (`handle_price_change`,
`handle_position_update`, `handle_balance_update`) that take already-
parsed dicts, so the parsing/state-update logic is fully unit-testable by
calling them directly -- never requires a live connection or real
credentials (Phase 5 section 42/43: mock first, tests must not need real
creds).
"""
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import socketio
import structlog

from database.schema.models import ConnectionState
from services.exchange.coindcx import normalize_symbol

logger = structlog.get_logger()

STREAM_URL = "wss://stream.coindcx.com"
DEFAULT_STALE_AFTER = timedelta(seconds=30)
PING_INTERVAL_SECONDS = 25  # matches CoinDCX's own sample code


def _extract_payload(response):
    """CoinDCX wraps every event's payload as {"event": <name>, "data":
    <JSON-encoded STRING>} on the real wire -- confirmed against a real
    authenticated connection
    (scripts/coindcx_account_ws_verification_test.py, 2026-08-26: 142/142
    real price-change events crashed with
    AttributeError: 'str' object has no attribute 'get' before this fix).
    Mirrors the identical discovery/fix in
    services/market_data/coindcx_ws.py's own _extract_payload() -- kept as
    a separate copy rather than a shared import so this file's account
    channel and that file's public market-data channel stay fully
    decoupled modules, per how they were each designed.

    The docs' own response samples show only each event's INNER shape --
    a dict for price-change, a JSON ARRAY for
    df-position-update/balance-update -- never this string-encoded outer
    wrapper. Returns whatever json.loads() decodes to (dict or list, matching
    whichever shape the specific event uses) so callers can handle both;
    returns None on a genuinely malformed/unparseable string rather than
    guessing, and falls back to the response itself if there's no "data"
    key at all (forward-compatible with an already-decoded payload, e.g.
    in tests)."""
    if not isinstance(response, dict):
        return response
    if "data" not in response:
        return response
    data = response["data"]
    if isinstance(data, str):
        try:
            return json.loads(data)
        except (ValueError, TypeError):
            logger.warning("CoinDCX WS: failed to parse event data string", data=data[:200])
            return None
    return data


@dataclass
class LiveMarketState:
    price: Optional[float] = None
    last_updated: Optional[datetime] = None


@dataclass
class LiveAccountState:
    positions: dict = field(default_factory=dict)  # exchange_position_id -> position dict
    balance: Optional[dict] = None
    positions_updated_at: Optional[datetime] = None
    balance_updated_at: Optional[datetime] = None


class CoinDCXWebSocketClient:
    def __init__(self, api_key: str = "", api_secret: str = "", symbol: str = "BTC/USDT"):
        self._api_key = api_key
        self._api_secret = api_secret
        self._instrument = normalize_symbol(symbol)
        self._connected = False

        self.market_state = LiveMarketState()
        self.account_state = LiveAccountState()

        self._sio = socketio.AsyncClient(reconnection=True, reconnection_attempts=0, reconnection_delay=2, reconnection_delay_max=30)
        self._register_handlers()

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def _register_handlers(self) -> None:
        self._sio.on("connect", self._on_connect)
        self._sio.on("disconnect", self._on_disconnect)
        self._sio.on("price-change", self._on_price_change_event)
        self._sio.on("df-position-update", self._on_position_update_event)
        self._sio.on("balance-update", self._on_balance_update_event)

    # ---- pure message handlers (unit-testable without a connection) ----

    def handle_price_change(self, data: dict) -> None:
        price = data.get("p")
        if price is None:
            return
        self.market_state.price = float(price)
        self.market_state.last_updated = datetime.utcnow()

    def handle_position_update(self, positions: list[dict]) -> None:
        now = datetime.utcnow()
        for pos in positions:
            pos_id = pos.get("id")
            if pos_id is None:
                continue
            self.account_state.positions[pos_id] = pos
        self.account_state.positions_updated_at = now

    def handle_balance_update(self, balances: list[dict]) -> None:
        self.account_state.balance = balances
        self.account_state.balance_updated_at = datetime.utcnow()

    # ---- socket.io event adapters (extract `.data`, delegate to the above) ----

    async def _on_connect(self):
        self._connected = True
        logger.info("CoinDCX WebSocket connected")
        if self.is_configured:
            body = {"channel": "coindcx"}
            signature = hmac.new(self._api_secret.encode(), json.dumps(body, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
            await self._sio.emit("join", {"channelName": "coindcx", "authSignature": signature, "apiKey": self._api_key})
        await self._sio.emit("join", {"channelName": f"{self._instrument}@prices-futures"})

    async def _on_disconnect(self):
        self._connected = False
        logger.warning("CoinDCX WebSocket disconnected")

    async def _on_price_change_event(self, response):
        payload = _extract_payload(response)
        if isinstance(payload, dict):
            self.handle_price_change(payload)

    async def _on_position_update_event(self, response):
        data = _extract_payload(response)
        if data is None:
            return
        self.handle_position_update(data if isinstance(data, list) else [data])

    async def _on_balance_update_event(self, response):
        data = _extract_payload(response)
        if data is None:
            return
        self.handle_balance_update(data if isinstance(data, list) else [data])

    # ---- freshness (Phase 5 section 36) ----

    def market_data_state(self, now: Optional[datetime] = None, stale_after: timedelta = DEFAULT_STALE_AFTER) -> ConnectionState:
        if not self._connected:
            return ConnectionState.DISCONNECTED
        if self.market_state.last_updated is None:
            return ConnectionState.UNAVAILABLE
        age = (now or datetime.utcnow()) - self.market_state.last_updated
        return ConnectionState.LIVE if age <= stale_after else ConnectionState.STALE

    def account_data_state(self, now: Optional[datetime] = None, stale_after: timedelta = DEFAULT_STALE_AFTER) -> ConnectionState:
        if not self.is_configured:
            return ConnectionState.NOT_CONFIGURED
        if not self._connected:
            return ConnectionState.DISCONNECTED
        if self.account_state.positions_updated_at is None:
            return ConnectionState.UNAVAILABLE
        age = (now or datetime.utcnow()) - self.account_state.positions_updated_at
        return ConnectionState.LIVE if age <= stale_after else ConnectionState.STALE

    # ---- real connection lifecycle (not exercised in this session --
    # no live CoinDCX credentials were available; see docs/known_limitations.md) ----

    async def connect(self) -> None:
        await self._sio.connect(STREAM_URL, transports=["websocket"])

    async def disconnect(self) -> None:
        if self._connected:
            await self._sio.disconnect()
