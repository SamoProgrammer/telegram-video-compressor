import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import bot


class ApiTest(unittest.TestCase):
    def test_form_encoding(self):
        captured = {}
        original = bot.requests.post

        class Response:
            status_code = 200

            def json(self):
                return {"ok": True, "result": {}}

        def fake_post(url, data=None, files=None, timeout=None):
            captured["data"] = data
            return Response()

        bot.requests.post = fake_post
        try:
            bot.api(
                "sendMessage",
                chat_id=1,
                text="hi",
                reply_parameters='{"message_id": 2}',
                drop_pending_updates=False,
            )
        finally:
            bot.requests.post = original
        self.assertEqual(captured["data"]["drop_pending_updates"], "false")
        self.assertEqual(captured["data"]["reply_parameters"], '{"message_id": 2}')


class HandleTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.original_api = bot.api
        bot.api = lambda method, files=None, **params: (
            self.calls.append((method, params)) or {"message_id": 1}
        )

    def tearDown(self):
        bot.api = self.original_api

    def test_rejects_oversized_video_before_download(self):
        bot.handle(
            {
                "chat": {"id": 1},
                "message_id": 2,
                "from": {"id": 3},
                "video": {"file_id": "x", "file_size": 21 * 1024 * 1024},
            }
        )
        self.assertEqual(self.calls[0][0], "sendMessage")
        self.assertIn("20 MB", self.calls[0][1]["text"])

    def test_help(self):
        bot.handle({"chat": {"id": 1}, "message_id": 2, "from": {"id": 3}, "text": "/start"})
        self.assertEqual(self.calls[0][0], "sendMessage")
        self.assertIn("720p", self.calls[0][1]["text"])


class PickMediaTest(unittest.TestCase):
    def test_video(self):
        video = {"file_id": "x", "file_size": 1}
        self.assertEqual(bot.pick_media({"video": video}), (video, "video"))

    def test_video_note(self):
        note = {"file_id": "y"}
        self.assertEqual(bot.pick_media({"video_note": note}), (note, "video_note"))

    def test_video_document(self):
        document = {"file_id": "z", "mime_type": "video/mp4"}
        self.assertEqual(
            bot.pick_media({"document": document}), (document, "document")
        )

    def test_text_is_ignored(self):
        self.assertIsNone(bot.pick_media({"text": "hi"}))


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed"
)
class HandleFlowTest(unittest.TestCase):
    def test_video_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "in.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=640x360:rate=30",
                    "-t",
                    "1",
                    "-pix_fmt",
                    "yuv420p",
                    str(src),
                ],
                check=True,
            )
            calls = []
            original_api = bot.api
            original_download = bot.download

            def fake_api(method, files=None, **params):
                size = None
                if files:
                    handle = next(iter(files.values()))[1]
                    position = handle.tell()
                    handle.seek(0, 2)
                    size = handle.tell()
                    handle.seek(position)
                calls.append((method, files, params, size))
                return {"message_id": 1}

            def fake_download(file_id, progress=None):
                return src

            bot.api = fake_api
            bot.download = fake_download
            try:
                bot.handle(
                    {
                        "chat": {"id": 1},
                        "message_id": 2,
                        "from": {"id": 3},
                        "video": {
                            "file_id": "x",
                            "file_size": src.stat().st_size,
                            "duration": 1,
                        },
                    }
                )
            finally:
                bot.api = original_api
                bot.download = original_download

            methods = [call[0] for call in calls]
            self.assertEqual(methods[0], "sendMessage")
            self.assertEqual(methods[-1], "editMessageText")
            self.assertIn("sendVideo", methods)
            self.assertTrue(all(m != "sendMessage" for m in methods[1:-1]))
            video = next(call for call in calls if call[0] == "sendVideo")
            self.assertGreater(video[3], 0)
            self.assertEqual(video[2]["width"], 640)
            self.assertEqual(video[2]["height"], 360)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed"
)
class CompressTest(unittest.TestCase):
    def test_compress_short_video(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "in.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=640x360:rate=30",
                    "-t",
                    "1",
                    "-pix_fmt",
                    "yuv420p",
                    str(src),
                ],
                check=True,
            )
            out = Path(directory) / "out.mp4"
            info = bot.compress(src, out, "video")
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 0)
            self.assertEqual(info["width"], 640)
            self.assertEqual(info["height"], 360)
            self.assertGreater(info["duration"], 0)


class SettingsTest(unittest.TestCase):
    def test_round_video_falls_back_when_over_60s(self):
        note = {"output": "note"}
        self.assertEqual(bot.target_kind("video", note, 30), ("video_note", False))
        self.assertEqual(bot.target_kind("video", note, 61), ("video", True))
        self.assertEqual(bot.target_kind("video", {"output": "auto"}, 1), ("video", False))

    def test_resolution_labels_map_to_vertical_resolution(self):
        self.assertIn("min(1920,iw)", bot.video_filter("video", {"resolution": "1080"}))
        self.assertIn("min(1280,iw)", bot.video_filter("video", {"resolution": "720"}))
        self.assertIn("min(854,iw)", bot.video_filter("video", {"resolution": "480"}))
        self.assertIn("min(426,iw)", bot.video_filter("video", {"resolution": "240"}))
        self.assertIn("min(480,iw)", bot.video_filter("video_note", {"resolution": "480"}))
        self.assertIn("min(640,iw)", bot.video_filter("video_note", {"resolution": "1080"}))

    def test_buttons_fit_callback_limit_and_use_styles(self):
        state = bot.get_state(99, 1)
        rows = bot.main_kb(state) + bot.settings_kb(state) + bot.queue_kb(state)
        for row in rows:
            for button in row:
                self.assertLessEqual(len(button["callback_data"].encode()), 64)
        self.assertEqual(
            {button.get("style") for row in rows for button in row if button.get("style")},
            {"primary", "success", "danger"},
        )
        bot.USERS.pop(99, None)


class QueueTest(unittest.TestCase):
    def test_oldest_cached_file_is_evicted(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            old = tmp / "old.bin"
            new = tmp / "new.bin"
            old.write_bytes(b"x" * 100)
            new.write_bytes(b"y" * 100)
            queue = [{"media": {"file_id": "old"}, "size": 100, "path": old, "name": "old"}]
            item = {"media": {"file_id": "new"}, "size": 100, "path": None, "name": "new"}
            originals = (bot.TMP, bot.DISK_BUDGET, bot.download, bot.probe)
            bot.TMP = tmp
            bot.DISK_BUDGET = 150
            bot.download = lambda file_id, progress=None: new
            bot.probe = lambda path: {
                "width": 640,
                "height": 360,
                "duration": 1.0,
                "has_audio": False,
            }
            try:
                error = bot.cache_item(item, queue)
            finally:
                bot.TMP, bot.DISK_BUDGET, bot.download, bot.probe = originals
            self.assertIsNone(error)
            self.assertFalse(old.exists())
            self.assertIsNone(queue[0]["path"])
            self.assertEqual(queue[0]["media"]["file_id"], "old")
            self.assertEqual(item["path"], new)

    def test_invalid_media_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            bad = tmp / "bad.bin"
            bad.write_bytes(b"not a video")
            item = {"media": {"file_id": "bad"}, "size": 11, "path": None, "name": "bad"}
            originals = (bot.TMP, bot.download, bot.probe)
            bot.TMP = tmp
            bot.download = lambda file_id, progress=None: bad
            bot.probe = lambda path: (_ for _ in ()).throw(
                bot.BotError("No video stream found")
            )
            try:
                error = bot.cache_item(item, [])
            finally:
                bot.TMP, bot.download, bot.probe = originals
            self.assertIn("Not a readable video", error)
            self.assertIsNone(item["path"])
            self.assertFalse(bad.exists())


class OutputLimitTest(unittest.TestCase):
    def test_large_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            src = tmp / "in.mp4"
            src.write_bytes(b"x" * (3 * 1024 * 1024))
            calls = []
            originals = (
                bot.TMP,
                bot.MAX_OUTPUT_MB,
                bot.api,
                bot.download,
                bot.probe,
                bot.compress,
            )
            bot.TMP = tmp
            bot.MAX_OUTPUT_MB = 1
            bot.api = lambda method, files=None, **params: (
                calls.append((method, params)) or {"message_id": 1}
            )
            bot.download = lambda file_id, progress=None: src
            bot.probe = lambda path: {
                "width": 640,
                "height": 360,
                "duration": 1.0,
                "has_audio": False,
            }

            def fake_compress(source, destination, kind, settings, **kwargs):
                destination.write_bytes(b"y" * (2 * 1024 * 1024))
                return bot.probe(destination)

            bot.compress = fake_compress
            try:
                result = bot.compress_one(
                    1, 2, {"file_id": "x"}, "video", dict(bot.DEFAULT_SETTINGS)
                )
            finally:
                (
                    bot.TMP,
                    bot.MAX_OUTPUT_MB,
                    bot.api,
                    bot.download,
                    bot.probe,
                    bot.compress,
                ) = originals
            self.assertIn("upload limit", result["error"])


class AllowTest(unittest.TestCase):
    def setUp(self):
        self.original = (bot.ALLOWED, bot.ALLOWED_USERNAMES)
        self.calls = []
        self.original_api = bot.api
        bot.api = lambda method, files=None, **params: (
            self.calls.append((method, params)) or {"message_id": 1}
        )

    def tearDown(self):
        bot.ALLOWED, bot.ALLOWED_USERNAMES = self.original
        bot.api = self.original_api
        bot.USERS.clear()

    def test_username_allowlist_is_case_insensitive(self):
        bot.ALLOWED = set()
        bot.ALLOWED_USERNAMES = {"samo"}
        self.assertTrue(bot.is_allowed(7, "Samo"))
        self.assertTrue(bot.is_allowed(7, "@samo"))
        self.assertFalse(bot.is_allowed(8, "other"))
        self.assertFalse(bot.is_allowed(8, ""))

    def test_id_allowlist(self):
        bot.ALLOWED = {5}
        bot.ALLOWED_USERNAMES = set()
        self.assertTrue(bot.is_allowed(5, "someone"))
        self.assertFalse(bot.is_allowed(6, "someone"))

    def test_empty_lists_are_open(self):
        bot.ALLOWED = set()
        bot.ALLOWED_USERNAMES = set()
        self.assertTrue(bot.is_allowed(42, ""))

    def test_deny_message_shows_identity(self):
        bot.ALLOWED = {1}
        bot.ALLOWED_USERNAMES = set()
        bot.handle(
            {
                "chat": {"id": 2},
                "message_id": 3,
                "from": {"id": 9, "username": "Nope"},
                "text": "hi",
            }
        )
        reply = self.calls[0][1]["text"]
        self.assertIn("private", reply.lower())
        self.assertIn("Your user ID: 9 (@Nope)", reply)


class ProgressTest(unittest.TestCase):
    def test_hhmmss_parses_out_time(self):
        self.assertAlmostEqual(bot._hhmmss("00:00:01.500000"), 1.5)
        self.assertAlmostEqual(bot._hhmmss("00:01:02.000000"), 62.0)
        self.assertEqual(bot._hhmmss("bogus"), 0.0)

    def test_upload_meter_reports_percent(self):
        class FakeReporter:
            def __init__(self):
                self.texts = []

            def show(self, text, force=False):
                self.texts.append(text)

        reporter = FakeReporter()
        meter = bot.UploadMeter(io.BytesIO(b"x" * 1000), 1000, reporter)
        meter.read(300)
        meter.read(300)
        meter.read(400)
        self.assertIn("📤 Uploading… 30%", reporter.texts)
        self.assertIn("📤 Uploading… 60%", reporter.texts)
        self.assertEqual(reporter.texts[-1], "📤 Uploading… 100%")


class MtprotoTest(unittest.TestCase):
    def setUp(self):
        self.original = (bot.TELEGRAM_API_ID, bot.TELEGRAM_API_HASH, bot.api, bot.mtproto_download)

    def tearDown(self):
        bot.TELEGRAM_API_ID, bot.TELEGRAM_API_HASH, bot.api, bot.mtproto_download = self.original

    def set_creds(self, on):
        bot.TELEGRAM_API_ID = 1 if on else 0
        bot.TELEGRAM_API_HASH = "hash" if on else ""

    def too_big_api(self):
        def thrower(method, files=None, **params):
            raise bot.BotError("getFile: file is too big")
        bot.api = thrower

    def test_upload_limit_extends_with_mtproto(self):
        self.set_creds(False)
        self.assertEqual(
            bot.mt_upload_limit_bytes(), bot.MAX_OUTPUT_MB * 1024 * 1024
        )
        self.set_creds(True)
        self.assertEqual(bot.mt_upload_limit_bytes(), bot.MT_UPLOAD_LIMIT)

    def test_download_falls_back_to_mtproto_on_too_big(self):
        self.set_creds(True)
        self.too_big_api()
        sentinel = Path("mt-sentinel.bin")
        bot.mtproto_download = lambda file_id, dest, progress=None: sentinel
        self.assertIs(bot.download("big"), sentinel)

    def test_download_still_fails_without_mtproto(self):
        self.set_creds(False)
        self.too_big_api()
        with self.assertRaises(bot.BotError):
            bot.download("big")

    def test_download_reraises_other_errors(self):
        self.set_creds(True)

        def thrower(method, files=None, **params):
            raise bot.BotError("getFile: network error")

        bot.api = thrower
        with self.assertRaises(bot.BotError):
            bot.download("big")


class CallbackTest(unittest.TestCase):
    def setUp(self):
        bot.USERS.clear()
        self.calls = []
        self.original_api = bot.api
        bot.api = lambda method, files=None, **params: (
            self.calls.append((method, params)) or {"message_id": 1}
        )

    def tearDown(self):
        bot.api = self.original_api
        bot.USERS.clear()

    def test_done_with_empty_queue_alerts(self):
        bot.handle_callback(
            {
                "id": "1",
                "from": {"id": 3},
                "message": {"message_id": 2, "chat": {"id": 1}},
                "data": "multi:done",
            }
        )
        answers = [params for method, params in self.calls if method == "answerCallbackQuery"]
        self.assertEqual(len(answers), 1)
        self.assertTrue(answers[0]["show_alert"])
        self.assertIn("empty", answers[0]["text"].lower())

    def test_stale_callback_alerts(self):
        bot.handle_callback(
            {
                "id": "2",
                "from": {"id": 3},
                "message": {"message_id": 2, "chat": {"id": 1}},
                "data": "menu:gone",
            }
        )
        answers = [params for method, params in self.calls if method == "answerCallbackQuery"]
        self.assertTrue(answers[0]["show_alert"])
        self.assertIn("outdated", answers[0]["text"].lower())


class BatchTest(unittest.TestCase):
    def setUp(self):
        bot.USERS.clear()
        self.calls = []
        self.original_api = bot.api
        self.original_compress_one = bot.compress_one
        bot.api = lambda method, files=None, **params: (
            self.calls.append((method, params)) or {"message_id": 1}
        )

    def tearDown(self):
        bot.api = self.original_api
        bot.compress_one = self.original_compress_one
        bot.USERS.clear()

    def item(self, name):
        return {
            "media": {"file_id": name},
            "kind": "video",
            "message_id": 2,
            "size": 1024,
            "name": name,
            "filename": f"{name}.mp4",
            "path": None,
            "duration": 1,
        }

    def test_batch_completes_and_clears_queue(self):
        state = bot.get_state(3, 1)
        state["processing"] = True
        state["queue"] = []
        bot.compress_one = lambda *args, **kwargs: {
            "in": 10.0,
            "out": 4.0,
            "saved": True,
            "kind": "video",
            "fallback": False,
        }
        bot.process_batch(
            3, 1, 10, [self.item("a"), self.item("b")], dict(bot.DEFAULT_SETTINGS)
        )
        self.assertEqual(state["queue"], [])
        self.assertFalse(state["processing"])
        summary = [params["text"] for method, params in self.calls if method == "editMessageText"][-1]
        self.assertIn("Batch complete — 2/2", summary)
        self.assertIn("20.0 MB → 8.0 MB", summary)

    def test_cancel_keeps_remaining_items(self):
        state = bot.get_state(3, 1)
        state["processing"] = True
        state["cancel"] = True
        state["queue"] = []
        bot.compress_one = lambda *args, **kwargs: self.fail("should not compress")
        items = [self.item("a"), self.item("b")]
        bot.process_batch(3, 1, 10, items, dict(bot.DEFAULT_SETTINGS))
        self.assertEqual(state["queue"], items)
        summary = [params["text"] for method, params in self.calls if method == "editMessageText"][-1]
        self.assertIn("Cancelled", summary)


if __name__ == "__main__":
    unittest.main()
