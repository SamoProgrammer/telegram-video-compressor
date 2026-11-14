"""Bounded, reusable MTProto transfers using the bot's own token."""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from .config import Config
from .errors import BotError

MT_WORKERS = 4
CHUNK_SIZE = 512 * 1024


def mt_regions(
    total: int, branches: int = MT_WORKERS, min_each: int = 8 * 1024 * 1024
) -> list[tuple[int, int]]:
    """Plan [start, length] regions on 512 KB-aligned offsets."""
    count = max(1, min(branches, max(1, total // min_each)))
    size = max(CHUNK_SIZE, (total // count) - (total // count) % CHUNK_SIZE)
    plan = []
    offset = 0
    for _ in range(count - 1):
        plan.append((offset, size))
        offset += size
    if offset < total:
        plan.append((offset, total - offset))
    return plan


class MTProtoClient:
    """One event loop owns persistent clients and a four-connection worker pool."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.session_file = config.mt_session_file
        self.work_dir = config.work_dir
        self._runtime_lock = threading.Lock()
        self._loop = None
        self._thread = None
        self._ready = None
        self._closing = False
        self._clients = {}
        self._clients_lock = None
        self._leases = None

    @property
    def ready(self) -> bool:
        return self.config.mt_ready

    async def _start_client(self, session: str):
        from telethon import TelegramClient

        client = TelegramClient(
            session,
            self.config.telegram_api_id,
            self.config.telegram_api_hash,
            receive_updates=False,
        )
        await client.start(bot_token=self.config.token)
        return client

    async def _client(self, index: int):
        if self._clients_lock is None:
            self._clients_lock = asyncio.Lock()
        async with self._clients_lock:
            if index in self._clients:
                return self._clients[index]
            if index == 0:
                session = str(self.session_file)
            else:
                path = self.work_dir / f"bot-m{index}.session"
                if not path.exists():
                    shutil.copyfile(self.session_file, path)
                session = str(path)
            self._clients[index] = await self._start_client(session)
            return self._clients[index]

    def _thread_main(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._initialize())
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.close()

    async def _initialize(self) -> None:
        self._leases = asyncio.Queue()
        for index in range(1, MT_WORKERS + 1):
            self._leases.put_nowait(index)

    def _ensure_runtime(self) -> None:
        with self._runtime_lock:
            if self._closing:
                raise BotError("MTProto is shut down")
            if self._loop is not None:
                return
            self._loop = asyncio.new_event_loop()
            self._ready = threading.Event()
            self._thread = threading.Thread(
                target=self._thread_main, name="mtproto-io", daemon=True
            )
            self._thread.start()
        self._ready.wait(timeout=60)
        if not self._ready.is_set():
            raise BotError("MTProto event loop did not start")

    def _run(self, coroutine):
        self._ensure_runtime()
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()

    @asynccontextmanager
    async def _workers(self, count: int):
        indexes = []
        try:
            for _ in range(count):
                indexes.append(await self._leases.get())
            yield [await self._client(index) for index in indexes]
        finally:
            for index in indexes:
                self._leases.put_nowait(index)

    async def _download_async(self, chat_id, message_id, dest, on_percent, stats):
        from telethon.tl.types import InputPeerUser
        from telethon import utils

        main = await self._client(0)
        # Bots may address users in their own chats without an access hash.
        peer = InputPeerUser(user_id=chat_id, access_hash=0)
        message = await main.get_messages(peer, ids=message_id)
        if not message or not message.document:
            raise BotError("Could not fetch the original video from Telegram")
        document = message.document
        dc_id, location = utils.get_input_location(document)
        total = document.size
        regions = mt_regions(total)
        if stats is not None:
            stats["connections"] = len(regions)
        progress = {"done": 0}

        async def pump(client, offset, length) -> None:
            remaining = length
            with open(dest, "r+b") as output:
                output.seek(offset)
                async for piece in client.iter_download(
                    location,
                    dc_id=dc_id,
                    offset=offset,
                    request_size=CHUNK_SIZE,
                    file_size=total,
                ):
                    if remaining <= 0:
                        break
                    take = piece[:remaining]
                    output.write(take)
                    done = len(take)
                    remaining -= done
                    progress["done"] += done
                    if on_percent:
                        on_percent(min(100, progress["done"] * 100 // total))

        with open(dest, "wb") as output:
            output.truncate(total)
        async with self._workers(len(regions)) as clients:
            results = await asyncio.gather(
                *(
                    pump(client, offset, length)
                    for client, (offset, length) in zip(clients, regions)
                ),
                return_exceptions=True,
            )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        if progress["done"] != total:
            raise BotError("MTProto download came back truncated; try again")
        return dest

    def download(
        self, chat_id, message_id, dest: Path, on_percent=None, stats=None
    ) -> Path:
        """Download aligned regions concurrently; connections are leased globally."""
        from telethon import errors as telethon_errors

        try:
            return self._run(
                self._download_async(chat_id, message_id, dest, on_percent, stats)
            )
        except telethon_errors.FloodWaitError as exc:
            raise BotError(
                f"MTProto cooldown: Telegram asked for a {exc.seconds}s wait. "
                "Try the transfer again after that delay."
            ) from None

    @staticmethod
    def _attributes(kind: str, filename: str, info: dict):
        from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo

        if kind == "video_note":
            video = DocumentAttributeVideo(
                max(1, int(info["duration"])),
                int(info["width"]),
                int(info["height"]),
                round_message=True,
            )
            return [video, DocumentAttributeFilename(filename)]
        if kind == "video":
            video = DocumentAttributeVideo(
                max(1, int(info["duration"])),
                int(info["width"]),
                int(info["height"]),
                supports_streaming=True,
            )
            return [video, DocumentAttributeFilename(filename)]
        return [DocumentAttributeFilename(filename)]

    async def _upload_large(self, main, path: Path, on_percent, stats):
        from telethon import functions
        from telethon.tl.types import InputFileBig

        size = path.stat().st_size
        regions = mt_regions(size)
        total_parts = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
        if not total_parts:
            return await main.upload_file(path)
        stats["connections"] = len(regions)
        file_id = secrets.randbelow(2**63 - 1) + 1
        progress = {"done": 0}

        async def send_part(fd: int, index: int) -> None:
            async with self._workers(1) as (worker,):
                data = await asyncio.to_thread(
                    os.pread, fd, CHUNK_SIZE, index * CHUNK_SIZE
                )
                if not data:
                    raise BotError("Upload ended before every part was read")
                result = await worker(
                    functions.upload.SaveBigFilePartRequest(
                        file_id=file_id,
                        file_part=index,
                        file_total_parts=total_parts,
                        bytes=data,
                    )
                )
                if not result:
                    raise BotError("Telegram rejected an upload part")
                progress["done"] += len(data)
                if on_percent:
                    on_percent(min(100, progress["done"] * 100 // size))

        with path.open("rb") as uploaded:
            results = await asyncio.gather(
                *(send_part(uploaded.fileno(), index) for index in range(total_parts)),
                return_exceptions=True,
            )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return InputFileBig(file_id, total_parts, path.name)

    async def _upload_async(
        self, chat_id, message_id, path, kind, caption, filename, info,
        on_percent, stats,
    ) -> None:
        main = await self._client(0)
        callback = None
        if on_percent:
            callback = lambda current, total: on_percent(
                min(100, current * 100 // total)
            )
        if path.stat().st_size >= 32 * 1024 * 1024:
            uploaded = await self._upload_large(main, path, on_percent, stats)
            callback = None
        else:
            uploaded = str(path)
            if stats is not None:
                stats["connections"] = 1
        await main.send_file(
            chat_id,
            uploaded,
            caption=caption,
            attributes=self._attributes(kind, filename, info),
            force_document=kind == "document",
            supports_streaming=kind == "video",
            reply_to=message_id,
            progress_callback=callback,
        )

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
        stats=None,
    ) -> None:
        """Upload large files by parts; keep Telethon's normal sender for small files."""
        from telethon import errors as telethon_errors

        if stats is None:
            stats = {}
        try:
            return self._run(
                self._upload_async(
                    chat_id, message_id, path, kind, caption, filename, info,
                    on_percent, stats,
                )
            )
        except telethon_errors.FloodWaitError as exc:
            raise BotError(
                f"MTProto cooldown: Telegram asked for a {exc.seconds}s wait "
                "before uploading. Try again shortly."
            ) from None

    async def _disconnect_all(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        if clients:
            await asyncio.gather(
                *(client.disconnect() for client in clients), return_exceptions=True
            )

    def close(self) -> None:
        """Disconnect persisted clients; called after transfer workers join."""
        with self._runtime_lock:
            if self._closing:
                return
            self._closing = True
            loop = self._loop
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._disconnect_all(), loop).result(
                timeout=30
            )
        finally:
            loop.call_soon_threadsafe(loop.stop)
            self._thread.join(timeout=30)
