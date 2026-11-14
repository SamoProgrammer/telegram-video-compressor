import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from tgcompress.config import MT_UPLOAD_LIMIT_BYTES
from tgcompress.errors import BotError, UploadTooLarge
from tgcompress.files import safe_unlink
from tgcompress.mtproto import MTProtoClient, mt_regions
from tgcompress.transfer import BOT_API_UPLOAD_CAP, Transfer

from .support import FakeApi, StubMTProto, make_config


class LimitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)

    def test_limits_extend_with_mtproto(self):
        plain = make_config(self.tmp)
        self.assertEqual(plain.input_limit_bytes, 20 * 1024 * 1024)
        self.assertEqual(plain.upload_limit_bytes, BOT_API_UPLOAD_CAP)

        mt = make_config(self.tmp, telegram_api_id=1, telegram_api_hash="hash")
        self.assertEqual(mt.input_limit_bytes, MT_UPLOAD_LIMIT_BYTES)
        self.assertEqual(mt.upload_limit_bytes, MT_UPLOAD_LIMIT_BYTES)


class DownloadFallbackTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)

    def test_falls_back_to_mtproto_on_too_big(self):
        config = make_config(self.tmp, telegram_api_id=1, telegram_api_hash="hash")
        mtproto = StubMTProto()
        sentinel = self.tmp / "mt.bin"
        mtproto.download = lambda chat_id, message_id, dest, on_percent=None: sentinel
        transfer = Transfer(config, FakeApi(), mtproto)
        self.assertIs(transfer.download("big", chat_id=2, message_id=3), sentinel)

    def test_still_fails_without_mtproto(self):
        config = make_config(self.tmp)
        transfer = Transfer(config, FakeApi(), StubMTProto())
        with self.assertRaises(BotError):
            transfer.download("big", chat_id=2, message_id=3)

    def test_other_errors_are_not_swallowed(self):
        config = make_config(self.tmp, telegram_api_id=1, telegram_api_hash="hash")
        api = FakeApi()
        api.get_file = lambda file_id: (_ for _ in ()).throw(
            BotError("getFile: network error")
        )
        transfer = Transfer(config, api, StubMTProto())
        with self.assertRaises(BotError):
            transfer.download("big", chat_id=2, message_id=3)


class SendResultRoutingTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)

    def test_routes_large_file_to_mtproto_when_ready(self):
        config = make_config(self.tmp, telegram_api_id=1, telegram_api_hash="hash")
        api = FakeApi()
        mtproto = StubMTProto()
        transfer = Transfer(config, api, mtproto)

        path = self.tmp / "large.mp4"
        path.write_bytes(b"x")
        with patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value = MagicMock(st_size=BOT_API_UPLOAD_CAP + 1024)
            transfer.send_result(
                chat_id=123,
                message_id=456,
                path=path,
                kind="video",
                info={"width": 1920, "height": 1080, "duration": 60},
                caption="test",
            )
        self.assertEqual(len(mtproto.uploads), 1)
        self.assertEqual(len(api.calls), 0)

    def test_routes_small_file_to_bot_api(self):
        config = make_config(self.tmp, telegram_api_id=1, telegram_api_hash="hash")
        api = FakeApi()
        mtproto = StubMTProto()
        transfer = Transfer(config, api, mtproto)

        path = self.tmp / "small.mp4"
        path.write_bytes(b"x" * 1024)
        transfer.send_result(
            chat_id=123,
            message_id=456,
            path=path,
            kind="video",
            info={"width": 1920, "height": 1080, "duration": 60},
            caption="test",
        )
        self.assertEqual(len(mtproto.uploads), 0)
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(api.calls[0][0], "sendVideo")

    def test_rejects_oversize_without_mtproto(self):
        config = make_config(self.tmp, telegram_api_id=0, telegram_api_hash="")
        api = FakeApi()
        mtproto = StubMTProto()
        transfer = Transfer(config, api, mtproto)

        path = self.tmp / "large.mp4"
        path.write_bytes(b"x")
        with patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value = MagicMock(st_size=BOT_API_UPLOAD_CAP + 1024)
            with self.assertRaises(UploadTooLarge):
                transfer.send_result(
                    chat_id=123,
                    message_id=456,
                    path=path,
                    kind="video",
                    info={"width": 1920, "height": 1080, "duration": 60},
                    caption="test",
                )
        self.assertEqual(len(mtproto.uploads), 0)
        self.assertEqual(api.calls, [])


class OversizeRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)

    def test_explicit_cloud_rejection_falls_back_to_mtproto(self):
        class OversizeApi(FakeApi):
            def call(self, method, files=None, **params):
                raise UploadTooLarge(f"{method}: Request Entity Too Large")

        config = make_config(self.tmp, telegram_api_id=1, telegram_api_hash="hash")
        mtproto = StubMTProto()
        transfer = Transfer(config, OversizeApi(), mtproto)
        path = self.tmp / "output.mp4"
        path.write_bytes(b"x" * 1024)

        transfer.send_result(
            chat_id=1,
            message_id=2,
            path=path,
            kind="video",
            info={"width": 640, "height": 360, "duration": 1},
            caption="test",
        )

        self.assertEqual(len(mtproto.uploads), 1)

    def test_real_file_above_cloud_cap_never_calls_bot_api(self):
        config = make_config(self.tmp, telegram_api_id=1, telegram_api_hash="hash")
        api = FakeApi()
        mtproto = StubMTProto()
        transfer = Transfer(config, api, mtproto)
        path = self.tmp / "output.mp4"
        with path.open("wb") as handle:
            handle.truncate(BOT_API_UPLOAD_CAP + 1)

        output = io.StringIO()
        with redirect_stdout(output):
            transfer.send_result(
                chat_id=1,
                message_id=2,
                path=path,
                kind="video",
                info={"width": 640, "height": 360, "duration": 1},
                caption="test",
            )

        self.assertEqual(api.calls, [])
        self.assertEqual(len(mtproto.uploads), 1)
        self.assertIn(f"size_bytes={BOT_API_UPLOAD_CAP + 1}", output.getvalue())
        self.assertIn("route=mtproto", output.getvalue())

    def test_cloud_only_oversize_fails_before_doomed_upload(self):
        config = make_config(self.tmp)
        api = FakeApi()
        transfer = Transfer(config, api, StubMTProto())
        path = self.tmp / "output.mp4"
        with path.open("wb") as handle:
            handle.truncate(BOT_API_UPLOAD_CAP + 1)

        with self.assertRaisesRegex(UploadTooLarge, "MTProto"):
            transfer.send_result(
                chat_id=1,
                message_id=2,
                path=path,
                kind="video",
                info={"width": 640, "height": 360, "duration": 1},
                caption="test",
            )
        self.assertEqual(api.calls, [])

    def test_local_api_uses_its_larger_limit_before_mtproto(self):
        config = make_config(
            self.tmp,
            api_base="http://127.0.0.1:8081",
            max_output_mb=2000,
            telegram_api_id=1,
            telegram_api_hash="hash",
        )
        api = FakeApi()
        mtproto = StubMTProto()
        transfer = Transfer(config, api, mtproto)
        path = self.tmp / "output.mp4"
        with path.open("wb") as handle:
            handle.truncate(BOT_API_UPLOAD_CAP + 1)

        transfer.send_result(
            chat_id=1,
            message_id=2,
            path=path,
            kind="video",
            info={"width": 640, "height": 360, "duration": 1},
            caption="test",
        )

        self.assertEqual(len(api.calls), 1)
        self.assertEqual(mtproto.uploads, [])


class SafeUnlinkTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)

    def test_unlinks_file_inside_root(self):
        inside = self.tmp / "sub" / "file.bin"
        inside.parent.mkdir(parents=True)
        inside.write_bytes(b"hello")
        safe_unlink(inside, self.tmp)
        self.assertFalse(inside.exists())

    def test_blocks_path_traversal_outside_root(self):
        outside = self.tmp.parent / f"secret-{Path(self.tmp).name}-leak.bin"
        outside.write_bytes(b"secret")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))

        traversal_path = self.tmp / ".." / outside.name
        safe_unlink(traversal_path, self.tmp)
        self.assertTrue(outside.exists())

    def test_does_not_delete_root_itself(self):
        safe_unlink(self.tmp, self.tmp)
        self.assertTrue(self.tmp.exists())

    def test_none_or_missing_path_is_safe(self):
        safe_unlink(None, self.tmp)
        safe_unlink(self.tmp / "nonexistent.bin", self.tmp)


class RegionsTest(unittest.TestCase):
    def test_single_region_for_small_files(self):
        self.assertEqual(mt_regions(4 * 1024 * 1024), [(0, 4 * 1024 * 1024)])

    def test_regions_are_aligned_and_complete(self):
        total = 100 * 1024 * 1024
        plan = mt_regions(total)
        self.assertTrue(2 <= len(plan) <= 6)
        self.assertEqual(sum(length for _, length in plan), total)
        for start, _ in plan:
            self.assertEqual(start % (512 * 1024), 0)


class MTProtoClientTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)

    def test_session_file_is_persistent_config_path(self):
        session_path = self.tmp / "my-session.session"
        config = make_config(self.tmp, mt_session_file=session_path)
        client = MTProtoClient(config)
        self.assertEqual(client.session_file, session_path)


if __name__ == "__main__":
    unittest.main()
