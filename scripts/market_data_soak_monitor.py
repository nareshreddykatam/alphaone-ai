"""Bounded real soak monitor for the live CoinDCX market-data WebSocket
running inside the already-started backend process (apps.api.main, port
8000, MARKET_DATA_WS_ENABLED=true). Polls the real running dashboard API
and the real SQLite DB every 30s for a fixed duration, recording summary
statistics only (never per-tick data) -- never places an order, never
generates a signal, never touches the account.
"""
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, r"C:\Users\Naresh Reddy\Downloads\AlphaOne AI\alphaone")

import httpx

DURATION_SECONDS = 30 * 60
POLL_INTERVAL_SECONDS = 30
DB_PATH = r"C:\Users\Naresh Reddy\Downloads\AlphaOne AI\alphaone\alphaone_research.db"
API_BASE = "http://127.0.0.1:8000"


def db_counts():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM signals")
    signals = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM trades")
    trades = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM sync_events WHERE source='coindcx_market_ws'")
    ws_transitions = cur.fetchone()[0]
    conn.close()
    return signals, trades, ws_transitions


def main():
    print("=" * 60)
    print("LIVE MARKET DATA -- REAL SOAK MONITOR")
    print("=" * 60)
    print(f"Duration: {DURATION_SECONDS}s (~{DURATION_SECONDS // 60} min), polling every {POLL_INTERVAL_SECONDS}s")
    print(f"Started: {datetime.utcnow().isoformat()}Z")
    print()

    client = httpx.Client(timeout=5.0)
    start = time.monotonic()
    samples = []
    errors = []
    statuses_seen = set()
    signals_start, trades_start, transitions_start = db_counts()

    while time.monotonic() - start < DURATION_SECONDS:
        t = time.monotonic() - start
        try:
            resp = client.get(f"{API_BASE}/api/v1/dashboard/")
            body = resp.json()
            status = body.get("market_data_status")
            price = body.get("btc_price_usdt")
            mark = body.get("market_data_mark_price_usdt")
            statuses_seen.add(status)
            samples.append({"t": round(t, 1), "status": status, "price": price, "mark": mark})
            print(f"[{t:7.1f}s] status={status:<12} price_usdt={price} mark_usdt={mark}")
        except Exception as e:
            errors.append({"t": round(t, 1), "error": f"{type(e).__name__}: {e}"})
            print(f"[{t:7.1f}s] POLL ERROR: {type(e).__name__}: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)

    signals_end, trades_end, transitions_end = db_counts()

    print()
    print("=" * 60)
    print("SOAK SUMMARY")
    print("=" * 60)
    print(f"Ended: {datetime.utcnow().isoformat()}Z")
    print(f"Total samples: {len(samples)}")
    print(f"Poll errors: {len(errors)}")
    print(f"Distinct market_data_status values seen: {sorted(s for s in statuses_seen if s)}")
    live_samples = [s for s in samples if s["status"] == "LIVE"]
    print(f"Samples with status=LIVE: {len(live_samples)} / {len(samples)}")
    prices = [s["price"] for s in samples if s["price"] is not None]
    if prices:
        print(f"Price range observed (USDT): {min(prices)} .. {max(prices)}")
    print(f"Signals in DB: {signals_start} -> {signals_end} (delta {signals_end - signals_start})")
    print(f"Trades in DB: {trades_start} -> {trades_end} (delta {trades_end - trades_start})")
    print(f"coindcx_market_ws SyncEvent (connection transition) rows: {transitions_start} -> {transitions_end} (delta {transitions_end - transitions_start})")
    if errors:
        print("Errors:")
        for e in errors:
            print(f"  [{e['t']}s] {e['error']}")
    print()
    print("Real trades placed: 0 (this script only polls read-only HTTP endpoints and reads the local DB)")


if __name__ == "__main__":
    main()
