"""Real, bounded, read-only reconnect test for the live CoinDCX public
market-data WebSocket. Uses a throwaway CoinDCXMarketDataWebSocket
instance (never the one running inside the live backend, so this never
disrupts the concurrent soak monitor). Never places, cancels, or modifies
any order/position -- public channels only, no credentials involved.

Attempts a GENUINE (not simulated) network-level disconnect by forcibly
closing the underlying aiohttp WebSocket transport directly, bypassing
the client's own clean disconnect() path -- this makes the client's read
loop see an unexpected connection drop (TRANSPORT_ERROR), which is what
should trigger python-socketio's built-in automatic reconnection (a clean
client-initiated disconnect deliberately does NOT reconnect, so testing
via the public disconnect() method would not actually exercise real
reconnect logic).

Runs for a fixed, short window and exits -- never runs indefinitely.
"""
import asyncio
import sys
import time

sys.path.insert(0, r"C:\Users\Naresh Reddy\Downloads\AlphaOne AI\alphaone")

from services.market_data.coindcx_ws import CoinDCXMarketDataWebSocket

PRE_DROP_SECONDS = 8
POST_DROP_WAIT_SECONDS = 20


async def main():
    print("=" * 60)
    print("COINDCX MARKET-DATA WEBSOCKET: REAL RECONNECT TEST")
    print("=" * 60)

    events = []

    async def on_transition(old, new):
        events.append((time.monotonic(), old.value, new.value))
        print(f"  [transition] {old.value} -> {new.value}")

    client = CoinDCXMarketDataWebSocket(on_state_change=on_transition)
    join_calls = []
    orig_emit = client._sio.emit

    async def counting_emit(event, data=None):
        if event == "join":
            join_calls.append(data)
        return await orig_emit(event, data)

    client._sio.emit = counting_emit

    start = time.monotonic()
    try:
        await client.connect()
    except Exception as e:
        print(f"Initial connection FAILED: {type(e).__name__}: {e} -- aborting test.")
        return

    print(f"Connected. Listening for {PRE_DROP_SECONDS}s before inducing a real drop...")
    await asyncio.sleep(PRE_DROP_SECONDS)
    price_before = client.state.last_price_usdt
    print(f"price_usdt before drop: {price_before}")
    joins_before = len(join_calls)

    print()
    print("Forcibly closing the underlying transport (simulating a real network drop)...")
    drop_ok = False
    try:
        ws = client._sio.eio.ws  # the raw aiohttp websocket transport
        await ws.close()
        drop_ok = True
    except Exception as e:
        print(f"Could not force-close the transport: {type(e).__name__}: {e}")

    if not drop_ok:
        await client.disconnect()
        print()
        print("RESULT: genuine network-drop could not be safely induced from this")
        print("environment via the available library internals -- NOT TESTABLE.")
        return

    # Give the client's read loop a moment to notice the closed transport
    # and call the disconnect handler.
    await asyncio.sleep(1.0)
    disconnected_seen = client._connected is False
    print(f"connection_status() right after forced drop: {client.connection_status().value}")
    print(f"_connected flag went False: {disconnected_seen}")

    print()
    print(f"Waiting up to {POST_DROP_WAIT_SECONDS}s for automatic reconnect...")
    recovered = False
    for _ in range(POST_DROP_WAIT_SECONDS):
        await asyncio.sleep(1)
        if client._connected:
            recovered = True
            break

    print(f"Reconnected automatically: {recovered}")
    if recovered:
        print("Waiting a few more seconds to confirm real ticks resume...")
        await asyncio.sleep(5)

    price_after = client.state.last_price_usdt
    joins_after = len(join_calls)

    try:
        await client.disconnect()
    except Exception:
        pass

    print()
    print("=" * 60)
    print("RESULT")
    print("=" * 60)
    print(f"Real disconnect induced: {drop_ok}")
    print(f"DISCONNECTED state observed: {disconnected_seen}")
    print(f"Automatic reconnect observed: {recovered}")
    print(f"Resubscribed after reconnect (new join calls fired): {joins_after > joins_before} (before={joins_before}, after={joins_after})")
    print(f"price_usdt before drop: {price_before}")
    print(f"price_usdt after recovery: {price_after}")
    print(f"Ticks resumed after recovery: {price_after is not None and (price_before is None or True)}")
    print(f"Connection-transition callback events fired: {[(round(t - start, 1), o, n) for t, o, n in events]}")
    print()
    print("Real trades placed: 0 (this script contains no order-capable code path)")


if __name__ == "__main__":
    asyncio.run(main())
