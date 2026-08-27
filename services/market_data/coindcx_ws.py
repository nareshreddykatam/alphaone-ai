"""Public live BTC/USDT market-data WebSocket ("Live Market Data" phase).

This is intentionally a NEW, separate client from
services/exchange/coindcx_ws.py (Phase 5's AUTHENTICATED account channel
for the user's real positions/orders/balance) -- that file is untouched by
this phase. This client only joins CoinDCX's PUBLIC, unauthenticated
market-data channels and exists purely to feed dashboard display and
monitoring freshness with a live price, replacing the previous
periodic-candle-only "STALE" market-price path.

Channels used (verified against the live official docs at
https://docs.coindcx.com, "Futures Sockets" section, 2026-08-26 -- see
docs/coindcx_api_findings.md for the full research record):

  - f"{instrument}@prices-futures"  -> event "price-change"
      Last-traded-price tick. Response: {"T": <epoch_ms>, "p": "<price>", "pr": "f"}
  - "currentPrices@futures@rt"      -> event "currentPrices@futures#update"
      Mark price for ALL pairs in one stream. Response:
      {"ts": <epoch_ms>, "prices": {"<instrument>": {"mp": <mark_price>, ...}, ...}}

No documented "index price" field exists anywhere in CoinDCX's futures
WebSocket API -- index_price_usdt is therefore always None here. This is
a documented absence, never a fabricated value.

Instrument choice -- B-BTC_USDT, NOT B-BTC_INR: the real CoinDCX account
this session verified is INR-margined and trades B-BTC_INR (see
services/exchange/coindcx.py's DEFAULT_MARGIN_CURRENCY). This client
deliberately subscribes to B-BTC_USDT instead: a genuinely USDT-
denominated live price that matches AlphaOne's canonical "BTC/USDT"
symbol and the Binance-sourced signal strategy's own price terms, so it
plugs into the existing USDT->INR conversion pipeline
(services/exchange/fx.py) built for the INR-only UI. The real position's
own mark price is a separate, already-live (30s REST poll, Phase 5),
already-INR-native data path -- this client never feeds position PnL
math, only the dashboard's "current BTC/USDT market price" display and
connection-freshness state.

Heartbeat: emits {"data": "Ping message"} on the "ping" event every 25
seconds, exactly matching CoinDCX's own official sample code (no
documented pong/ack is expected back).

Reconnect: delegated to python-socketio's own built-in reconnection
(exponential backoff between 2s and 30s, unlimited attempts) rather than
reimplemented -- the same choice already made in
services/exchange/coindcx_ws.py. Resubscription happens automatically
because socketio invokes the "connect" handler again on every successful
reconnect, and each reconnect is a fresh server-side session, so there is
no risk of duplicate subscriptions accumulating.
"""
import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

import socketio
import structlog

from database.schema.models import ConnectionState
from services.exchange.coindcx import normalize_symbol

logger = structlog.get_logger()

STREAM_URL = "wss://stream.coindcx.com"
PING_INTERVAL_SECONDS = 25  # matches CoinDCX's own official sample code

# BTC/USDT futures trade roughly every few seconds on CoinDCX during normal
# activity; 20s is tight enough to catch a real stall quickly without
# flapping to STALE on ordinary short gaps between trades.
DEFAULT_STALE_AFTER = timedelta(seconds=20)

TransitionCallback = Callable[[ConnectionState, ConnectionState], Awaitable[None]]


@dataclass
class MarketTick:
    symbol: str = "BTC/USDT"
    instrument: str = ""
    event_timestamp: Optional[datetime] = None  # exchange-reported time (T/ts fields)
    last_price_usdt: Optional[float] = None
    mark_price_usdt: Optional[float] = None
    index_price_usdt: Optional[float] = None  # never populated -- not documented by CoinDCX
    volume: Optional[float] = None
    source: str = "CoinDCX WebSocket (public)"
    received_at: Optional[datetime] = None  # AlphaOne's own local receipt clock
    raw: Optional[dict] = field(default=None, repr=False)  # last raw message, never destroyed


def _ms_to_dt(ms) -> Optional[datetime]:
    try:
        return datetime.utcfromtimestamp(float(ms) / 1000)
    except (TypeError, ValueError):
        return None


def _extract_payload(response) -> dict:
    """CoinDCX wraps every event's payload as {"event": <name>, "data": <JSON-
    encoded STRING>} -- confirmed against the real WebSocket (2026-08-26,
    scripts/coindcx_market_ws_connectivity_test.py). The official docs'
    response samples show only the inner shape, not this string-encoded
    outer wrapper, which crashed the first real connectivity test attempt
    (AttributeError: 'str' object has no attribute 'get') before this fix.
    Also accepts an already-parsed dict `data` (and a bare payload with no
    wrapper at all) so existing mocked tests supplying either shape keep
    working -- never assumes only one wire format."""
    if not isinstance(response, dict):
        return {}
    if "data" not in response:
        return response
    data = response["data"]
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except (ValueError, TypeError):
            logger.warning("CoinDCX market WS: failed to parse event data string", data=data[:200])
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(data, dict):
        return data
    return {}


class CoinDCXMarketDataWebSocket:
    def __init__(self, symbol: str = "BTC/USDT", on_state_change: Optional[TransitionCallback] = None):
        self.symbol = symbol
        self._instrument = normalize_symbol(symbol, margin_currency="USDT")  # deliberate override -> B-BTC_USDT
        self.state = MarketTick(symbol=symbol, instrument=self._instrument)
        self._connected = False
        self._ever_connected = False
        self._last_notified_status: Optional[ConnectionState] = None
        self._on_state_change = on_state_change
        self._ping_task: Optional[asyncio.Task] = None

        self._sio = socketio.AsyncClient(
            reconnection=True, reconnection_attempts=0, reconnection_delay=2, reconnection_delay_max=30,
        )
        self._register_handlers()

    def _register_handlers(self) -> None:
        self._sio.on("connect", self._on_connect)
        self._sio.on("disconnect", self._on_disconnect)
        self._sio.on("price-change", self._on_price_change_event)
        self._sio.on("currentPrices@futures#update", self._on_current_prices_event)

    # ---- pure message handlers (unit-testable without a connection) ----

    def handle_price_change(self, data: dict, now: Optional[datetime] = None) -> None:
        price = data.get("p")
        if price is None:
            return
        try:
            price = float(price)
        except (TypeError, ValueError):
            logger.warning("CoinDCX market WS: malformed price-change price", data=data)
            return
        self.state.last_price_usdt = price
        ts = _ms_to_dt(data.get("T"))
        if ts is not None:
            self.state.event_timestamp = ts
        self.state.received_at = now or datetime.utcnow()
        self.state.raw = data

    def handle_current_prices(self, data: dict, now: Optional[datetime] = None) -> None:
        prices = data.get("prices")
        if not isinstance(prices, dict):
            return
        entry = prices.get(self._instrument)
        if not entry or entry.get("mp") is None:
            return  # no mark-price update for our instrument in this message -- never fabricate one
        try:
            mark_price = float(entry["mp"])
        except (TypeError, ValueError):
            logger.warning("CoinDCX market WS: malformed mark price", entry=entry)
            return
        self.state.mark_price_usdt = mark_price
        ts = _ms_to_dt(data.get("ts"))
        if ts is not None:
            self.state.event_timestamp = ts
        self.state.received_at = now or datetime.utcnow()
        self.state.raw = data

    # ---- socket.io event adapters (extract `.data`, delegate to the above) ----

    async def _on_connect(self) -> None:
        self._connected = True
        self._ever_connected = True
        logger.info("CoinDCX market-data WebSocket connected", instrument=self._instrument)
        await self._sio.emit("join", {"channelName": f"{self._instrument}@prices-futures"})
        await self._sio.emit("join", {"channelName": "currentPrices@futures@rt"})
        self._ping_task = asyncio.create_task(self._ping_loop())
        await self._maybe_notify_transition()

    async def _on_disconnect(self) -> None:
        self._connected = False
        logger.warning("CoinDCX market-data WebSocket disconnected")
        if self._ping_task is not None:
            self._ping_task.cancel()
            self._ping_task = None
        await self._maybe_notify_transition()

    async def _on_price_change_event(self, response) -> None:
        self.handle_price_change(_extract_payload(response))

    async def _on_current_prices_event(self, response) -> None:
        self.handle_current_prices(_extract_payload(response))

    async def _ping_loop(self) -> None:
        while self._connected:
            await asyncio.sleep(PING_INTERVAL_SECONDS)
            if not self._connected:
                break
            try:
                await self._sio.emit("ping", {"data": "Ping message"})
            except Exception as e:
                logger.warning("CoinDCX market WS ping failed", error=str(e))

    async def _maybe_notify_transition(self) -> None:
        new_status = ConnectionState.LIVE if self._connected else ConnectionState.DISCONNECTED
        # The very first connect at startup is a baseline, not a "recovery"
        # from anything -- suppress the alert for it, only fire on a real
        # transition after having previously been in the opposite state.
        if self._last_notified_status is not None and self._last_notified_status != new_status:
            if self._on_state_change is not None:
                await self._on_state_change(self._last_notified_status, new_status)
        self._last_notified_status = new_status

    # ---- freshness ----

    def connection_status(self, now: Optional[datetime] = None, stale_after: timedelta = DEFAULT_STALE_AFTER) -> ConnectionState:
        if not self._connected:
            return ConnectionState.DISCONNECTED if self._ever_connected else ConnectionState.UNAVAILABLE
        if self.state.received_at is None:
            return ConnectionState.UNAVAILABLE
        age = (now or datetime.utcnow()) - self.state.received_at
        return ConnectionState.LIVE if age <= stale_after else ConnectionState.STALE

    # ---- real connection lifecycle ----

    async def connect(self) -> None:
        await self._sio.connect(STREAM_URL, transports=["websocket"])

    async def disconnect(self) -> None:
        if self._ping_task is not None:
            self._ping_task.cancel()
            self._ping_task = None
        if self._connected:
            await self._sio.disconnect()
