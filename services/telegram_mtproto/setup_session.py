"""ONE-TIME, MANUAL setup script -- run this YOURSELF, in your OWN
terminal, on your own machine. This script is never invoked by any
AlphaOne process, test, or automated agent -- it requires live,
interactive input (your phone number, the login code Telegram sends you,
and your 2FA password if you have one enabled) that only you can safely
provide. Nothing in this codebase asks you to paste that information
anywhere else -- if anything other than this script's own terminal
prompts you for a Telegram login code or password, stop and do not
enter it.

WHAT THIS DOES:
  1. Reads TELEGRAM_API_ID / TELEGRAM_API_HASH from your environment (get
     these yourself from https://my.telegram.org -> "API development
     tools" -- they are tied to YOUR Telegram account, not AlphaOne's).
  2. Logs in interactively (Telethon's standard flow: phone number, the
     code Telegram sends to your Telegram app/SMS, then your 2FA
     password if you have one).
  3. Prints a session string ONCE at the end.

WHAT TO DO WITH THE SESSION STRING:
  Treat it exactly like a password -- it grants read access to your
  Telegram account for as long as it's valid. Put it in your OWN .env
  file (never committed -- .env is already gitignored) as:

      TELEGRAM_SESSION=<the string printed below>
      TELEGRAM_MTPROTO_ENABLED=true

  Never paste it into a chat message, a commit, a log, or anywhere other
  than your own local .env / your deployment platform's secret store.

This script never sends, forwards, edits, or deletes anything -- it only
authenticates and immediately disconnects.
"""
import asyncio
import os
import sys


async def main():
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        print("Telethon is not installed. Run: pip install telethon", file=sys.stderr)
        sys.exit(1)

    api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()

    if not api_id or not api_hash:
        print(
            "TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in your environment before running this script.\n"
            "Get them from https://my.telegram.org -> API development tools (uses YOUR Telegram account).",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Connecting to Telegram... you will be prompted for your phone number, then a login code,")
    print("then your 2FA password if you have one enabled. This script never stores or transmits")
    print("these anywhere except directly to Telegram's own servers over MTProto.\n")

    client = TelegramClient(StringSession(), int(api_id), api_hash)
    # client.start() is Telethon's standard interactive login -- it calls
    # send_code_request/sign_in internally ONLY here, in this manual,
    # human-run script, never from any AlphaOne server process.
    await client.start()

    session_string = client.session.save()
    await client.disconnect()

    print("\nLogin successful. Your session string (copy this into your OWN .env, never share it):\n")
    print(session_string)
    print(
        "\nSet these in your .env (or your deployment platform's secret store, never in Git):\n"
        "  TELEGRAM_SESSION=<the string above>\n"
        "  TELEGRAM_MTPROTO_ENABLED=true\n"
        "  TELEGRAM_API_ID=<your API ID>\n"
        "  TELEGRAM_API_HASH=<your API hash>\n"
    )


if __name__ == "__main__":
    asyncio.run(main())
