"""Real, bounded, read-only verification of services/exchange/coindcx_ws.py
(Phase 5's AUTHENTICATED "coindcx" account channel: df-position-update,
df-order-update, balance-update). Never places, cancels, or modifies any
order, position, or leverage -- this script only listens. Runs for a
fixed, short window (default 25s) and disconnects cleanly; never runs
indefinitely. Never prints the API key/secret.

Purpose: confirm/deny whether the real wire-format bug found in
services/market_data/coindcx_ws.py (CoinDCX wraps every event's payload
as {"event": ..., "data": "<JSON-encoded STRING>"}, not a nested object)
also affects this file's _on_price_change_event/_on_position_update_event/
_on_balance_update_event adapters, which use the identical
`response.get("data", response) if isinstance(response, dict) else response`
pattern and have only ever been tested against mocks.
"""
import asyncio
import sys
import time

sys.path.insert(0, r"C:\Users\Naresh Reddy\Downloads\AlphaOne AI\alphaone")

from apps.api.config import get_settings
from services.exchange.coindcx_ws import CoinDCXWebSocketClient

RUN_SECONDS = 25


async def main():
    settings = get_settings()
    print("=" * 60)
    print("COINDCX AUTHENTICATED ACCOUNT WEBSOCKET: REAL VERIFICATION TEST")
    print("=" * 60)
    print("Endpoint: wss://stream.coindcx.com")
    print('Channels: "coindcx" (authenticated), "B-BTC_USDT@prices-futures" (public)')
    print(f"Run window: {RUN_SECONDS}s (bounded, then clean disconnect)")
    print("This script never places, cancels, or modifies any order/position.")
    print()

    if not (settings.coindcx_api_key and settings.coindcx_api_secret):
        print("COINDCX credentials not configured -- aborting, nothing connected.")
        return

    client = CoinDCXWebSocketClient(
        api_key=settings.coindcx_api_key, api_secret=settings.coindcx_api_secret, symbol="BTC/USDT",
    )
    # This class's default instrument resolves to B-BTC_INR (the account's
    # own low-liquidity margin market) -- a first run against it received
    # zero price-change events in the whole window. Override to the highly
    # active B-BTC_USDT market (same server, same event, same
    # _on_price_change_event adapter code path) purely so this diagnostic
    # can actually observe a real event; this does not change any
    # production code or behavior, only this throwaway script's target
    # channel for verification purposes.
    client._instrument = "B-BTC_USDT"

    # Patch the three real event adapters to catch and report a crash per
    # event type individually, rather than letting one bad event silently
    # kill the whole listener via socketio's own "Task exception was never
    # retrieved" swallowing (as happened on the first market-data run).
    crashes = {"price-change": None, "df-position-update": None, "balance-update": None}
    received = {"price-change": 0, "df-position-update": 0, "balance-update": 0}

    orig_price = client._on_price_change_event
    orig_position = client._on_position_update_event
    orig_balance = client._on_balance_update_event

    async def wrap_price(response):
        received["price-change"] += 1
        try:
            await orig_price(response)
        except Exception as e:
            crashes["price-change"] = f"{type(e).__name__}: {e}"

    async def wrap_position(response):
        received["df-position-update"] += 1
        try:
            await orig_position(response)
        except Exception as e:
            crashes["df-position-update"] = f"{type(e).__name__}: {e}"

    async def wrap_balance(response):
        received["balance-update"] += 1
        try:
            await orig_balance(response)
        except Exception as e:
            crashes["balance-update"] = f"{type(e).__name__}: {e}"

    client._sio.on("price-change", wrap_price)
    client._sio.on("df-position-update", wrap_position)
    client._sio.on("balance-update", wrap_balance)

    connect_ok = False
    error = None
    start = time.monotonic()
    try:
        await client.connect()
        connect_ok = True
        print("Connected. Listening (real account has 0 open positions -- no")
        print("position/balance activity is expected unless you touch it")
        print("manually elsewhere during this window)...")
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
    for event in ("price-change", "df-position-update", "balance-update"):
        n = received[event]
        c = crashes[event]
        print(f"{event}: received={n} crashed={'YES -- ' + c if c else 'no'}")

    print()
    print(f"market_state.price after run: {client.market_state.price}")
    print(f"account_state.positions_updated_at: {client.account_state.positions_updated_at}")
    print(f"account_state.balance_updated_at: {client.account_state.balance_updated_at}")
    print()
    print("Real trades placed: 0 (this script contains no order-capable code path)")


if __name__ == "__main__":
    asyncio.run(main())
