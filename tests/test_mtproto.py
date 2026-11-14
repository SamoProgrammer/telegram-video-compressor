import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telethon.tl.types import Document

from tgcompress.mtproto import MTProtoClient

from .support import make_config


class FakeTelegram:
    total_active = 0
    max_total_active = 0

    def __init__(self, session, payload=b"", download_delay=0.001):
        from telethon.tl.types import DocumentAttributeFilename

        self.session = session
        self.payload = payload
        self.download_delay = download_delay
        self.parts = []
        self.sent = []
        self.active = 0
        self.max_active = 0
        self.started = False
        self.document = Document(
            id=1,
            access_hash=2,
            file_reference=b"reference",
            date=None,
            mime_type="video/mp4",
            size=len(payload),
            dc_id=2,
            attributes=[DocumentAttributeFilename("source.mp4")],
            thumbs=[],
            video_thumbs=[],
        )

    async def start(self, bot_token):
        self.started = True
        return self

    async def disconnect(self):
        pass

    async def get_messages(self, peer, ids):
        await asyncio.sleep(0)
        return SimpleNamespace(document=self.document)

    async def iter_download(self, location, **kwargs):
        self.active += 1
        type(self).total_active += 1
        self.max_active = max(self.max_active, self.active)
        type(self).max_total_active = max(
            type(self).max_total_active, type(self).total_active
        )
        try:
            offset = kwargs.get("offset", 0)
            while offset < len(self.payload):
                await asyncio.sleep(self.download_delay)
                end = min(len(self.payload), offset + kwargs["request_size"])
                yield self.payload[offset:end]
                offset = end
        finally:
            self.active -= 1
            type(self).total_active -= 1

    async def __call__(self, request):
        self.active += 1
        type(self).total_active += 1
        self.max_active = max(self.max_active, self.active)
        type(self).max_total_active = max(
            type(self).max_total_active, type(self).total_active
        )
        try:
            await asyncio.sleep(0.001)
            self.parts.append(request)
            return True
        finally:
            self.active -= 1
            type(self).total_active -= 1

    async def send_file(self, chat_id, file, **kwargs):
        self.sent.append((chat_id, file, kwargs))


class ParallelTransferTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tmp = Path(self.directory.name)
        self.config = make_config(
            self.tmp,
            telegram_api_id=1,
            telegram_api_hash="hash",
            mt_session_file=self.tmp / "bot.session",
        )
        (self.tmp / "bot.session").write_bytes(b"session")
        self.client = MTProtoClient(self.config)
        self.addCleanup(self.client.close)

    def install(self, payload):
        clients = []
        FakeTelegram.total_active = 0
        FakeTelegram.max_total_active = 0

        async def start(client, session):
            instance = FakeTelegram(session, payload)
            clients.append(instance)
            return instance

        patcher = patch.object(MTProtoClient, "_start_client", start)
        patcher.start()
        self.addCleanup(patcher.stop)
        return clients

    def test_upload_parallelizes_parts_and_reuses_sessions(self):
        payload = bytes(range(256)) * ((32 * 1024 * 1024 + 1024) // 256)
        path = self.tmp / "large.mp4"
        path.write_bytes(payload)
        clients = self.install(payload)
        stats = {}

        for _ in range(2):
            self.client.upload(
                1, 2, path, "video", "caption", "compressed.mp4",
                {"duration": 1, "width": 640, "height": 360}, stats=stats,
            )

        self.assertEqual(len(clients), 5)  # primary + four leased workers
        self.assertEqual(stats["connections"], 4)
        self.assertGreater(FakeTelegram.max_total_active, 1)
        by_id = {}
        for instance in clients:
            for request in instance.parts:
                by_id.setdefault(request.file_id, []).append(request)
        self.assertEqual(len(by_id), 2)
        for requests in by_id.values():
            requests.sort(key=lambda request: request.file_part)
            self.assertEqual(
                b"".join(request.bytes for request in requests), payload
            )
            self.assertEqual(len(requests), requests[0].file_total_parts)
        sent_files = [file for instance in clients for _, file, _ in instance.sent]
        self.assertEqual(len(sent_files), 2)
        self.assertTrue(all(type(file).__name__ == "InputFileBig" for file in sent_files))

    def test_upload_file_id_is_valid_signed_int64_at_the_upper_boundary(self):
        from tgcompress import mtproto

        path = self.tmp / "large.mp4"
        path.write_bytes(b"x" * (32 * 1024 * 1024))
        clients = self.install(b"")
        with patch.object(mtproto.secrets, "randbelow", side_effect=lambda upper: upper - 1), patch.object(
            mtproto.secrets, "randbits", return_value=2**63
        ):
            self.client.upload(
                1, 2, path, "video", "caption", "compressed.mp4",
                {"duration": 1, "width": 640, "height": 360},
            )

        requests = [request for instance in clients for request in instance.parts]
        self.assertTrue(requests)
        for request in requests:
            self.assertEqual(request.file_id, 2**63 - 1)
            bytes(request)
        sent_files = [file for instance in clients for _, file, _ in instance.sent]
        for uploaded in sent_files:
            bytes(uploaded)

    def test_small_upload_keeps_normal_send_file_path(self):
        path = self.tmp / "small.mp4"
        path.write_bytes(b"small-content")
        clients = self.install(b"")
        stats = {}
        self.client.upload(
            1, 2, path, "video", "caption", "small.mp4",
            {"duration": 1, "width": 640, "height": 360}, stats=stats,
        )
        self.assertEqual(len(clients), 1)
        self.assertEqual(stats["connections"], 1)
        self.assertEqual(clients[0].sent[0][1], str(path))
        self.assertEqual(clients[0].parts, [])

    def test_download_regions_reassemble_the_exact_original(self):
        payload = bytes(range(251)) * (40 * 1024 * 1024 // 251) + b"tail"
        clients = self.install(payload)
        destination = self.tmp / "downloaded.mp4"
        stats = {}

        returned = self.client.download(1, 2, destination, stats=stats)

        self.assertEqual(returned, destination)
        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(stats["connections"], 4)
        self.assertEqual(len(clients), 5)
        self.assertGreater(FakeTelegram.max_total_active, 1)


if __name__ == "__main__":
    unittest.main()
