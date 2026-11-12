#!/usr/bin/env python3
"""One-shot MTProto login check with the bot token (proves api_id/hash)."""

import asyncio
import os
import sys

from telethon import TelegramClient


async def main() -> int:
    token = os.environ.get("BOT_TOKEN", "")
    api_id = int(os.environ.get("TELEGRAM_API_ID", "0") or 0)
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    if not (token and api_id and api_hash):
        print("MTProto check skipped (credentials missing).")
        return 0
    client = TelegramClient("mt-check", api_id, api_hash)
    await client.start(bot_token=token)
    me = await client.get_me()
    print(f"MTProto OK: logged in as @{me.username} (bot id {me.id})")
    await client.disconnect()
    os.remove("mt-check.session") if os.path.exists("mt-check.session") else None
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
