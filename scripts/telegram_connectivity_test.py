"""Controlled real Telegram connectivity test (Phase 5). Verifies
authentication and chat-id discovery via python-telegram-bot's `Bot`
(the same library the real services/telegram/bot.py:TelegramBot wraps),
then sends exactly ONE test message through TelegramBot._send() -- the
same send path every real alert uses.

Safety:
- Never prints TELEGRAM_BOT_TOKEN, in whole or in part.
- Does not enable the scheduler or generate any signal.
- Does not call any CoinDCX endpoint.
- Sends exactly one message, only if a chat id was found from a real
  update (never fabricates one).

Usage: python scripts/telegram_connectivity_test.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telegram import Bot

from apps.api.config import get_settings
from services.telegram.bot import TelegramBot


def _scrub(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***REDACTED***")
    return text


async def main() -> None:
    settings = get_settings()
    token = settings.telegram_bot_token
    secrets = (token,)

    print("=" * 60)
    print("TELEGRAM REAL CONNECTIVITY TEST")
    print("=" * 60)

    if not token:
        print("TELEGRAM_BOT_TOKEN not configured in .env -- aborting.")
        print("\nauthentication: FAIL")
        print("chat_id_detected: FAIL")
        print("test_message_delivered: FAIL")
        return

    results = {"authentication": False, "chat_id_detected": False, "test_message_delivered": False}
    chat_id = None

    bot = Bot(token=token)
    try:
        # --- Authentication: get_me() returns the bot's own (non-secret) identity ---
        me = await bot.get_me()
        print(_scrub(f"authenticated as: @{me.username} (id={me.id})", *secrets))
        results["authentication"] = True

        # --- Chat ID discovery: look for the /start update the user already sent ---
        updates = await bot.get_updates()
        print(f"pending updates found: {len(updates)}")
        for update in reversed(updates):  # most recent first
            if update.message is not None:
                chat_id = update.message.chat.id
                print(_scrub(f"found chat_id from message: {chat_id!r} (text={update.message.text!r})", *secrets))
                break

        if chat_id is None:
            print("No /start (or any) message found via getUpdates. "
                  "If you already sent /start, Telegram may have already delivered "
                  "that update to a previous long-poll/webhook consumer -- send /start again and re-run.")
        else:
            results["chat_id_detected"] = True

        # --- Send exactly one test message, via the real TelegramBot send path ---
        if chat_id is not None:
            test_bot = TelegramBot(bot_token=token, chat_id=str(chat_id))
            await test_bot._send(
                "AlphaOne connectivity test -- this is a one-time manual verification message. "
                "No trading signal, no order, no automated monitoring is active."
            )
            results["test_message_delivered"] = True
            print("test message sent.")

    except Exception as e:
        print(_scrub(f"ERROR: {type(e).__name__}: {e}", *secrets))
    finally:
        try:
            await bot.close()
        except Exception:
            pass

    print("\n" + "=" * 60)
    print("RESULT SUMMARY")
    print("=" * 60)
    print(f"authentication: {'PASS' if results['authentication'] else 'FAIL'}")
    print(f"chat_id_detected: {'PASS' if results['chat_id_detected'] else 'FAIL'}")
    print(f"test_message_delivered: {'PASS' if results['test_message_delivered'] else 'FAIL'}")


if __name__ == "__main__":
    asyncio.run(main())
