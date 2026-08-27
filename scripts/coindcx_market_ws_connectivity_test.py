"""Real, read-only CoinDCX public market-data WebSocket connectivity test
(services/market_data/coindcx_ws.py). Connects to the real
wss://stream.coindcx.com endpoint, joins ONLY the two public,
unauthenticated channels this phase uses (B-BTC_USDT@prices-futures and
currentPrices@futures@rt), listens for a bounded window, then disconnects
cleanly. Never joins the authenticated "coindcx" account channel, never
supplies any API key/secret, never places/cancels/modifies any order --
this script has no code path capable of doing so at all.

Run for a fixed, short window (default 20s) and exit -- never runs
indefinitely. Safe to run repeatedly.
"""
import asyncio
import sys
import time

sys.path.insert(0, r"C:\Users\Naresh Reddy\Downloads\AlphaOne AI\alphaone")

from services.market_data.coindcx_ws import CoinDCXMarketDataWebSocket

RUN_SECONDS = 20


async def main():
    print("=" * 60)
    print("COINDCX PUBLIC MARKET-DATA WEBSOCKET: REAL CONNECTIVITY TEST")
    print("=" * 60)
    print(f"Endpoint: wss://stream.coindcx.com")
    print(f"Channels: B-BTC_USDT@prices-futures, currentPrices@futures@rt")
    print(f"Run window: {RUN_SECONDS}s (bounded, then clean disconnect)")
    print()

    client = CoinDCXMarketDataWebSocket(symbol="BTC/USDT")
    connect_ok = False
    error = None

    start = time.monotonic()
    try:
        await client.connect()
        connect_ok = True
        print("Connected. Listening...")
        await asyncio.sleep(RUN_SECONDS)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    elapsed = time.monotonic() - start

    print()
    print("=" * 60)
    print("RESULT")
    print("=" * 60)
    print(f"Connection: {'PASS' if connect_ok else 'FAIL'}" + (f" ({error})" if error else ""))
    print(f"Elapsed: {elapsed:.1f}s")

    tick = client.state
    got_ltp = tick.last_price_usdt is not None
    got_mark = tick.mark_price_usdt is not None
    print(f"LTP (price-change) received: {'PASS' if got_ltp else 'FAIL'}")
    if got_ltp:
        print(f"  last_price_usdt = {tick.last_price_usdt}")
        print(f"  event_timestamp = {tick.event_timestamp}")
    print(f"Mark price (currentPrices) received: {'PASS' if got_mark else 'FAIL'}")
    if got_mark:
        print(f"  mark_price_usdt = {tick.mark_price_usdt}")

    status = client.connection_status()
    print(f"connection_status() at end: {status.value}")

    if tick.received_at is not None:
        freshness = (__import__("datetime").datetime.utcnow() - tick.received_at).total_seconds()
        print(f"Freshness: last message {freshness:.1f}s ago")

    print()
    print("Real trades placed: 0 (this script contains no order-capable code path)")


if __name__ == "__main__":
    asyncio.run(main())
