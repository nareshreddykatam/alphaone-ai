"""Controlled real CoinDCX API connectivity test (read-only only).

Per the Phase 5 report's section 21 ("Manual Real-Account Setup
Instructions"): before ever enabling the scheduler against a live
account, verify the real API responses actually match what
services/exchange/coindcx.py expects. This script does exactly that and
nothing else -- it never calls, imports, or references any order-placing/
cancelling/modifying/leverage/margin/transfer method (none exist in this
codebase; see tests/unit/test_no_order_placement_capability.py).

Safety:
- Never prints COINDCX_API_KEY/COINDCX_API_SECRET, in whole or in part.
- Every printed value is defensively scrubbed against the configured
  key/secret strings as a second layer, in case a future field ever
  echoed something unexpected back.
- Only classifies fields as present/absent and prints small representative
  samples of non-secret data (e.g. a wallet's non-identifying fields).

Usage: python scripts/coindcx_connectivity_test.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apps.api.config import get_settings
from services.exchange.coindcx import CoinDCXReadOnlyAccountProvider


def _scrub(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***REDACTED***")
    return text


def _safe_print(label: str, value, secrets: tuple[str, str]) -> None:
    print(_scrub(f"{label}: {value}", *secrets))


async def main() -> None:
    settings = get_settings()
    secrets = (settings.coindcx_api_key, settings.coindcx_api_secret)

    print("=" * 60)
    print("COINDCX REAL API CONNECTIVITY TEST")
    print("=" * 60)

    if not settings.coindcx_api_key or not settings.coindcx_api_secret:
        print("COINDCX_API_KEY/COINDCX_API_SECRET not configured in .env -- aborting.")
        return

    provider = CoinDCXReadOnlyAccountProvider(settings.coindcx_api_key, settings.coindcx_api_secret)
    results = {}

    try:
        # 1. Authentication (via wallet call -- CoinDCX has no separate "ping" endpoint)
        print("\n--- 1. AUTHENTICATION ---")
        status = await provider.get_connection_status()
        _safe_print("status", status, secrets)
        results["authentication"] = status.get("status") == "OK"

        # 2 & 3. Balance / available balance
        print("\n--- 2/3. FUTURES WALLET BALANCE ---")
        balance = await provider.get_balance()
        _safe_print("status", balance["status"], secrets)
        _safe_print("total_equity", balance.get("total_equity"), secrets)
        _safe_print("available_balance", balance.get("available_balance"), secrets)
        _safe_print("used_margin", balance.get("used_margin"), secrets)
        raw = balance.get("raw", {})
        expected_wallet_fields = {"id", "currency_short_name", "balance", "locked_balance", "cross_order_margin", "cross_user_margin"}
        missing = expected_wallet_fields - set(raw.keys()) if raw else expected_wallet_fields
        extra = set(raw.keys()) - expected_wallet_fields if raw else set()
        _safe_print("raw wallet fields present", sorted(raw.keys()) if raw else "none", secrets)
        if missing:
            _safe_print("MISSING vs docs/coindcx_api_findings.md", sorted(missing), secrets)
        if extra:
            _safe_print("EXTRA fields not in docs/coindcx_api_findings.md", sorted(extra), secrets)
        results["balance"] = balance["status"] == "OK"

        # 4. Open positions
        print("\n--- 4. OPEN POSITIONS ---")
        positions = await provider.get_open_positions()
        print(f"count: {len(positions)}")
        for p in positions:
            _safe_print("position", {k: v for k, v in p.items() if k != "exchange_position_id"}, secrets)
        results["positions"] = True  # empty list is a valid, honest result -- not a failure

        # 5. Trade history (last 30 days, default lookback)
        print("\n--- 5. TRADE HISTORY (trailing 30 days) ---")
        trades = await provider.get_trade_history(symbol="BTC/USDT")
        print(f"count: {len(trades)}")
        for t in trades[:5]:
            _safe_print("trade", t, secrets)
        if len(trades) > 5:
            print(f"... and {len(trades) - 5} more")
        results["trades"] = True

        # 6. Transactions / P&L
        print("\n--- 6. POSITION TRANSACTIONS (P&L/funding) ---")
        transactions = await provider.get_transactions()
        print(f"count: {len(transactions)}")
        for tx in transactions[:5]:
            _safe_print("transaction", tx, secrets)
        if len(transactions) > 5:
            print(f"... and {len(transactions) - 5} more")
        results["transactions"] = True

    except Exception as e:
        _safe_print("ERROR", f"{type(e).__name__}: {e}", secrets)
        results.setdefault("authentication", False)
    finally:
        await provider.close()

    print("\n" + "=" * 60)
    print("RESULT SUMMARY")
    print("=" * 60)
    for key in ("authentication", "balance", "positions", "trades", "transactions"):
        print(f"{key}: {'PASS' if results.get(key) else 'FAIL'}")


if __name__ == "__main__":
    asyncio.run(main())
