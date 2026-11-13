"""MTProto transfers with the bot's own token (no 20 MB Bot API cap)."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from .config import Config
from .errors import BotError


def mt_regions(
    total: int, branches: int = 6, min_each: int = 8 * 1024 * 1024
) -> list[tuple[int, int]]:
    """Plan [start, length] regions on 512 KB-aligned offsets."""
    count = max(1, min(branches, max(1, total // min_each)))
    size = max(512 * 1024, (total // count) - (total // count) % (512 * 1024))
    plan = []
    offset = 0
    for _ in range(count - 1):
        plan.append((offset, size))
        offset += size
    if offset < total:
        plan.append((offset, total - offset))
    return plan


class MTProtoClient:
    """Telethon client for files above the cloud Bot API limits."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.session_file = config.mt_session_file
        self.work_dir = config.work_dir

    @property
    def ready(self) -> bool:
        return self.config.mt_ready

    def download(
        self, chat_id: int, message_id: int, dest: Path, on_percent=None
    ) -> Path:
        """Parallel download via MTProto; the bot token is the login.

        Fetches the original message (modern Bot API file_ids cannot be decoded
        by Telethon) and streams several connections in parallel. One persistent
        MTProto session is reused; parallel workers run on copies of that auth
        key, so Telegram never sees extra ImportBotAuthorization requests (those
        cause multi-hour flood waits).
        """
        from telethon import TelegramClient
        from telethon import errors as telethon_errors
        from telethon import utils as telethon_utils
        from telethon.tl.types import InputPeerUser

        config = self.config
        chunk = 512 * 1024

        async def start(index: int, seed: bytes | None = None) -> TelegramClient:
            if index == 0:
                session = str(self.session_file)
            else:
                session_path = self.work_dir / f"bot-m{index}.session"
                if seed:
                    session_path.write_bytes(seed)
                else:
                    shutil.copyfile(self.session_file, session_path)
                session = str(session_path)
            client = TelegramClient(
                session, config.telegram_api_id, config.telegram_api_hash
            )
            await client.start(bot_token=config.token)
            return client

        async def pump(
            client, location, dc_id, offset, length, handle, state, total
        ) -> None:
            remaining = length
            async for piece in client.iter_download(
                location,
                dc_id=dc_id,
                offset=offset,
                request_size=chunk,
                file_size=total,
            ):
                if remaining <= 0:
                    break
                take = piece[:remaining]
                handle.write(take)
                remaining -= len(take)
                state["done"] += len(take)
                if on_percent:
                    on_percent(min(100, int(state["done"] * 100 / total)))

        async def run() -> Path:
            clients = []
            try:
                main = await start(0)
                clients.append(main)
                # Bots may address users in their own chats without an access hash.
                peer = InputPeerUser(user_id=chat_id, access_hash=0)
                message = await main.get_messages(peer, ids=message_id)
                if not message or not message.document:
                    raise BotError("Could not fetch the original video from Telegram")
                document = message.document
                dc_id, location = telethon_utils.get_input_location(document)
                total = document.size

                seed = None
                if self.session_file.exists():
                    seed = self.session_file.read_bytes()

                regions = mt_regions(total)
                state = {"done": 0}
                if len(regions) == 1:
                    with open(dest, "wb") as handle:  # noqa: ASYNC230
                        async for piece in main.iter_download(
                            location,
                            dc_id=dc_id,
                            request_size=chunk,
                            file_size=total,
                        ):
                            handle.write(piece)
                            state["done"] += len(piece)
                            if on_percent:
                                on_percent(min(100, int(state["done"] * 100 / total)))
                else:
                    with open(dest, "wb") as handle:  # noqa: ASYNC230
                        handle.truncate(total)
                    handles = []
                    try:
                        for index, (offset, length) in enumerate(regions):
                            if index > 0:
                                worker = await start(index, seed)
                                clients.append(worker)
                            handle = open(dest, "r+b")  # noqa: ASYNC230, SIM115
                            handle.seek(offset)
                            handles.append(handle)
                        await asyncio.gather(
                            *(
                                pump(
                                    client,
                                    location,
                                    dc_id,
                                    offset,
                                    length,
                                    handle,
                                    state,
                                    total,
                                )
                                for (client, (offset, length), handle) in zip(
                                    clients, regions, handles
                                )
                            )
                        )
                    finally:
                        for handle in handles:
                            handle.close()
                if state["done"] != total:
                    raise BotError("MTProto download came back truncated; try again")
                return dest
            except telethon_errors.FloodWaitError as exc:
                raise BotError(
                    f"MTProto cooldown: Telegram asked for a {exc.seconds}s wait "
                    "before more logins. Try small files now, big ones shortly."
                ) from None
            finally:
                for client in clients:
                    await client.disconnect()

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(run())
        finally:
            loop.close()

    def upload(
        self,
        chat_id: int,
        message_id: int,
        path: Path,
        kind: str,
        caption: str,
        filename: str,
        info: dict,
        on_percent=None,
    ) -> None:
        """Upload via MTProto with the bot's own token; no 50 MB cap."""
        from telethon import TelegramClient
        from telethon import errors as telethon_errors
        from telethon.tl.types import (
            DocumentAttributeFilename,
            DocumentAttributeVideo,
        )

        config = self.config

        async def run() -> None:
            client = TelegramClient(
                str(self.session_file),
                config.telegram_api_id,
                config.telegram_api_hash,
            )
            try:
                await client.start(bot_token=config.token)
                duration = max(1, int(info["duration"]))
                width = int(info["width"])
                height = int(info["height"])
                if kind == "video_note":
                    attributes = [
                        DocumentAttributeVideo(
                            duration, width, height, round_message=True
                        ),
                        DocumentAttributeFilename(filename),
                    ]
                elif kind == "video":
                    attributes = [
                        DocumentAttributeVideo(
                            duration, width, height, supports_streaming=True
                        ),
                        DocumentAttributeFilename(filename),
                    ]
                else:
                    attributes = [DocumentAttributeFilename(filename)]

                def callback(current: int, total: int) -> None:
                    if on_percent and total:
                        on_percent(min(100, int(current * 100 / total)))

                await client.send_file(
                    chat_id,
                    str(path),
                    caption=caption,
                    attributes=attributes,
                    force_document=kind == "document",
                    supports_streaming=kind == "video",
                    reply_to=message_id,
                    progress_callback=callback,
                )
            except telethon_errors.FloodWaitError as exc:
                raise BotError(
                    f"MTProto cooldown: Telegram asked for a {exc.seconds}s wait "
                    "before uploading. Try again shortly."
                ) from None
            finally:
                await client.disconnect()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(run())
        finally:
            loop.close()
