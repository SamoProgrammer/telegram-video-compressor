#!/usr/bin/env python3
"""One-shot MTProto login check with the bot token (proves api_id/hash)."""

import asyncio
import os
import sys

from telethon import TelegramClient
from telethon import errors as telethon_errors

MT_SESSION = os.environ.get(
    "BOT_MT_SESSION", "/tmp/bot-mtproto.session"
)


async def main() -> int:
    token = os.environ.get("BOT_TOKEN", "")
    api_id = int(os.environ.get("TELEGRAM_API_ID", "0") or 0)
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    if not (token and api_id and api_hash):
        print("MTProto check skipped (credentials missing).")
        return 0
    client = TelegramClient(MT_SESSION, api_id, api_hash)
    try:
        await client.start(bot_token=token)
        me = await client.get_me()
        print(f"MTProto OK: logged in as @{me.username} (bot id {me.id}); session cached.")
        try:
            import cryptg

            print("cryptg present (fast AES).")
        except ImportError:
            print("WARNING: cryptg missing; downloads will be slow.")
    except telethon_errors.FloodWaitError as exc:
        print(
            f"MTProto login flood: Telegram wants {exc.seconds}s. The cached "
            "session avoids re-imports; big files will work after the wait."
        )
    finally:
        await client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
