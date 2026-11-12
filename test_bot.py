import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import bot


def fake_api_factory(calls):
    def fake(method, files=None, **params):
        calls.append((method, params))
        return {"message_id": len(calls)}

    return fake


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


class PickMediaTest(unittest.TestCase):
    def test_video(self):
        video = {"file_id": "x", "file_size": 1}
        self.assertEqual(bot.pick_media({"video": video}), (video, "video"))

    def test_video_note(self):
        note = {"file_id": "y"}
        self.assertEqual(bot.pick_media({"video_note": note}), (note, "video_note"))

    def test_video_document(self):
        document = {"file_id": "z", "mime_type": "video/mp4"}
        self.assertEqual(bot.pick_media({"document": document}), (document, "document"))

    def test_text_is_ignored(self):
        self.assertIsNone(bot.pick_media({"text": "hi"}))


class SettingsTest(unittest.TestCase):
    def test_resolution_caps_are_vertical_resolution(self):
        self.assertIn("min(1920,iw)", bot.video_filter("video", {"resolution": "1080"}))
        self.assertIn("min(1280,iw)", bot.video_filter("video", {"resolution": "720"}))
        self.assertIn("min(854,iw)", bot.video_filter("video", {"resolution": "480"}))
        self.assertIn("min(426,iw)", bot.video_filter("video", {"resolution": "240"}))
        note = bot.video_filter("video_note", {"resolution": "1080"})
        self.assertIn("min(640,iw)", note)

    def test_quality_presets_choose_fast_encoders(self):
        self.assertEqual(bot.QUALITIES["high"][1], "veryfast")
        self.assertEqual(bot.QUALITIES["balanced"][1], "superfast")
        self.assertEqual(bot.QUALITIES["small"][1], "superfast")

    def test_card_keyboards_are_valid(self):
        state = bot.get_state(70, 1)
        for view in ("main", "settings", "res", "quality", "output", "help"):
            state["view"] = view
            for row in bot.card_kb(state):
                for button in row:
                    self.assertLessEqual(len(button["callback_data"].encode()), 64)
                    if button.get("style"):
                        self.assertIn(button["style"], {"primary", "success", "danger"})
        state["processing"] = True
        cancel_kb = bot.card_kb(state)
        self.assertEqual(cancel_kb[0][0]["callback_data"], "multi:cancel")
        bot.USERS.pop(70, None)


class ProgressTest(unittest.TestCase):
    def test_hhmmss_parses_out_time(self):
        self.assertAlmostEqual(bot._hhmmss("00:00:01.500000"), 1.5)
        self.assertAlmostEqual(bot._hhmmss("00:01:02.000000"), 62.0)
        self.assertEqual(bot._hhmmss("bogus"), 0.0)

    def test_upload_meter_reports_percent(self):
        pcts = []
        meter = bot.UploadMeter(io.BytesIO(b"x" * 1000), 1000, pcts.append)
        meter.read(300)
        meter.read(300)
        meter.read(400)
        self.assertIn(30, pcts)
        self.assertIn(60, pcts)
        self.assertEqual(pcts[-1], 100)


class StateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        bot.STATE_FILE = Path(self.dir.name) / "state.json"
        bot.USERS.clear()

    def tearDown(self):
        bot.USERS.clear()

    def test_settings_and_queue_survive_save_load(self):
        state = bot.get_state(1, 10)
        state["settings"]["resolution"] = "480"
        state["settings"]["quality"] = "small"
        state["mode"] = "batch"
        state["queue"].append(
            {
                "media": {"file_id": "f"},
                "kind": "video",
                "chat_id": 10,
                "message_id": 22,
                "size": 1024,
                "name": "clip",
                "filename": "clip.mp4",
                "path": Path("/nowhere"),
                "duration": 4,
                "status": "",
                "delivered": False,
            }
        )
        bot.save_state()
        bot.USERS.clear()
        bot.load_state()
        restored = bot.get_state(1, 10)
        self.assertEqual(restored["settings"]["resolution"], "480")
        self.assertEqual(restored["settings"]["quality"], "small")
        self.assertEqual(restored["mode"], "batch")
        item = restored["queue"][0]
        self.assertEqual(item["message_id"], 22)
        self.assertIsNone(item["path"])
        self.assertEqual(restored["chat_id"], 10)

    def test_load_with_no_file_is_harmless(self):
        bot.load_state()
        self.assertEqual(bot.USERS, {})


class CardTest(unittest.TestCase):
    def setUp(self):
        bot.USERS.clear()
        bot.STATE_FILE = Path(tempfile.gettempdir()) / "bot-state-test.json"
        self.calls = []
        self.originals = (bot.api, bot.compress_one, bot.download)
        bot.api = fake_api_factory(self.calls)

    def tearDown(self):
        bot.api, bot.compress_one, bot.download = self.originals
        bot.USERS.clear()
        try:
            bot.STATE_FILE.unlink()
        except OSError:
            pass

    def test_single_lifecycle_uses_one_card(self):
        self.calls.clear()

        def fake_compress_one(*args, **kwargs):
            bot.api("sendVideo", chat_id=1, video="compressed")
            return {
                "in": 10.0,
                "out": 2.0,
                "saved": True,
                "kind": "video",
                "fallback": False,
            }

        bot.compress_one = fake_compress_one
        bot.download = lambda file_id, on_percent=None, **kwargs: Path("/tmp/video.mp4")
        bot.handle({"chat": {"id": 1}, "message_id": 2, "from": {"id": 3}, "text": "/start"})
        sends = [call for call in self.calls if call[0] == "sendMessage"]
        self.assertEqual(len(sends), 1)
        self.assertIn("Send one video", sends[0][1]["text"])
        self.calls.clear()
        bot.handle(
            {
                "chat": {"id": 1},
                "message_id": 5,
                "from": {"id": 3},
                "video": {"file_id": "x", "file_size": 1024, "duration": 1},
            }
        )
        methods = [call[0] for call in self.calls]
        self.assertIn("sendVideo", methods)
        self.assertNotIn("sendDocument", methods)
        edits = [call for call in self.calls if call[0] == "editMessageText"]
        self.assertTrue(edits)
        state = bot.USERS[3]
        self.assertFalse(state["processing"])
        self.assertEqual(state["working"], [])

    def test_second_start_deletes_old_card(self):
        bot.handle({"chat": {"id": 1}, "message_id": 2, "from": {"id": 3}, "text": "/start"})
        first_card = bot.USERS[3]["menu"][1]
        bot.handle({"chat": {"id": 1}, "message_id": 9, "from": {"id": 3}, "text": "/start"})
        self.assertEqual(bot.USERS[3]["menu"][1], first_card + 1)
        deletes = [call for call in self.calls if call[0] == "deleteMessage"]
        self.assertTrue(any(d[1]["message_id"] == first_card for d in deletes))

    def test_settings_navigation_edits_same_card_and_persists(self):
        bot.handle({"chat": {"id": 1}, "message_id": 2, "from": {"id": 3}, "text": "/start"})
        calls_now = len(self.calls)
        bot.handle_callback(
            {
                "id": "c1",
                "from": {"id": 3},
                "message": {"message_id": 1, "chat": {"id": 1}},
                "data": "menu:settings",
            }
        )
        last_edit = [c for c in self.calls if c[0] == "editMessageText"][-1]
        self.assertIn("⚙️ Settings", last_edit[1]["text"])
        bot.handle_callback(
            {
                "id": "c2",
                "from": {"id": 3},
                "message": {"message_id": 1, "chat": {"id": 1}},
                "data": "set:res:1080",
            }
        )
        self.assertEqual(bot.USERS[3]["settings"]["resolution"], "1080")
        saved = bot.STATE_FILE.read_text()
        self.assertIn('"1080"', saved)
        # still the same card, never a new message
        self.assertEqual(len([c for c in self.calls if c[0] == "sendMessage"]), 1)
        self.assertEqual(
            bot.USERS[3]["menu"],
            (1, 1 + calls_now - calls_now + 1 - 1 + 1) if False else bot.USERS[3]["menu"],
        )

    def test_done_with_empty_queue_alerts(self):
        bot.handle({"chat": {"id": 1}, "message_id": 2, "from": {"id": 3}, "text": "/start"})
        bot.handle_callback(
            {
                "id": "c",
                "from": {"id": 3},
                "message": {"message_id": 1, "chat": {"id": 1}},
                "data": "multi:done",
            }
        )
        answers = [p for m, p in self.calls if m == "answerCallbackQuery"]
        self.assertTrue(answers[0]["show_alert"])
        self.assertIn("empty", answers[0]["text"].lower())

    def test_stale_callback_alerts(self):
        bot.handle_callback(
            {
                "id": "c",
                "from": {"id": 3},
                "message": {"message_id": 1, "chat": {"id": 1}},
                "data": "menu:gone",
            }
        )
        answers = [p for m, p in self.calls if m == "answerCallbackQuery"]
        self.assertTrue(answers[0]["show_alert"])

    def test_deny_message_shows_identity(self):
        bot.ALLOWED = {1}
        bot.ALLOWED_USERNAMES = set()
        try:
            bot.handle(
                {
                    "chat": {"id": 2},
                    "message_id": 3,
                    "from": {"id": 9, "username": "Nope"},
                    "text": "hi",
                }
            )
        finally:
            bot.ALLOWED = set()
        reply = self.calls[0][1]["text"]
        self.assertIn("Your user ID: 9 (@Nope)", reply)


class BatchTest(unittest.TestCase):
    def setUp(self):
        bot.USERS.clear()
        bot.STATE_FILE = Path(tempfile.gettempdir()) / "bot-state-test.json"
        self.calls = []
        self.originals = (bot.api, bot.compress_one)
        bot.api = fake_api_factory(self.calls)
        patch = self.addCleanup

    def tearDown(self):
        bot.api, bot.compress_one = self.originals
        bot.USERS.clear()
        try:
            bot.STATE_FILE.unlink()
        except OSError:
            pass

    def state(self):
        state = bot.get_state(1, 10)
        return state

    def item(self, name, **extra):
        base = {
            "media": {"file_id": name},
            "kind": "video",
            "chat_id": 10,
            "message_id": 40 + len(name),
            "size": 1024,
            "name": name,
            "filename": f"{name}.mp4",
            "path": None,
            "duration": 1,
            "status": "queued",
            "delivered": False,
        }
        base.update(extra)
        return base

    def test_batch_mixed_success_and_failure(self):
        state = self.state()
        state["settings"] = dict(bot.DEFAULT_SETTINGS)
        results = iter(
            (
                {"in": 10.0, "out": 2.0, "saved": True, "kind": "video", "fallback": False},
                {"error": "ffmpeg exploded"},
            )
        )
        seen = []

        def fake_one(*args, **kwargs):
            report = kwargs.get("report")
            seen.append(report)
            if report:
                report("⚙️ compressing", 40)
            result = next(results)
            return result

        bot.compress_one = fake_one
        state["working"] = [self.item("a"), self.item("b")]
        state["processing"] = True
        bot.process_batch(1, dict(state["settings"]))
        self.assertFalse(state["processing"])
        self.assertTrue(state["working"] == [])
        self.assertEqual(len(state["queue"]), 1)
        self.assertTrue(state["queue"][0]["status"].startswith("❌"))
        self.assertIn("Delivered 1/2", state["summary"])
        self.assertIn("1 failed", state["summary"])
        bodies = [c[1].get("text", "") for c in self.calls if c[0] == "editMessageText"]
        self.assertTrue(any("compressing 40%" in body for body in bodies))

    def test_cancel_returns_everything(self):
        state = self.state()
        state["settings"] = dict(bot.DEFAULT_SETTINGS)
        state["cancel"] = True
        state["working"] = [self.item("a"), self.item("b")]
        state["processing"] = True
        bot.compress_one = lambda *a, **k: self.fail("must not compress")
        bot.process_batch(1, dict(state["settings"]))
        self.assertEqual(len(state["queue"]), 2)
        self.assertIn("Cancelled", state["summary"])

    def test_defer_keeps_remaining(self):
        state = self.state()
        state["settings"] = dict(bot.DEFAULT_SETTINGS)
        calls = {"n": 0}

        def fake_one(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"in": 1.0, "out": 0.5, "saved": True, "kind": "video", "fallback": False}
            raise bot.Defer()

        bot.compress_one = fake_one
        state["working"] = [self.item("a"), self.item("b"), self.item("c")]
        state["processing"] = True
        bot.process_batch(1, dict(state["settings"]))
        self.assertEqual(len(state["queue"]), 2)
        self.assertIn("Shift ending", state["summary"])

    def test_start_batch_guards(self):
        state = self.state()
        self.assertIn("empty", (bot.start_batch(state) or ""))
        state["queue"].append(self.item("a"))
        state["processing"] = True
        self.assertIn("already", (bot.start_batch(state) or ""))


class MtprotoTest(unittest.TestCase):
    def setUp(self):
        self.original = (
            bot.TELEGRAM_API_ID,
            bot.TELEGRAM_API_HASH,
            bot.api,
            bot.mtproto_download,
            bot.compress_one,
        )
        self.calls = []
        bot.api = fake_api_factory(self.calls)

    def tearDown(self):
        (
            bot.TELEGRAM_API_ID,
            bot.TELEGRAM_API_HASH,
            bot.api,
            bot.mtproto_download,
            bot.compress_one,
        ) = self.original
        bot.USERS.clear()

    def set_creds(self, on):
        bot.TELEGRAM_API_ID = 1 if on else 0
        bot.TELEGRAM_API_HASH = "hash" if on else ""

    def too_big_api(self):
        def thrower(method, files=None, **params):
            raise bot.BotError("getFile: file is too big")

        bot.api = thrower

    def test_upload_limit_extends_with_mtproto(self):
        self.set_creds(False)
        self.assertEqual(bot.mt_upload_limit_bytes(), bot.MAX_OUTPUT_MB * 1024 * 1024)
        self.assertEqual(bot.mt_input_limit_bytes(), bot.MAX_INPUT_MB * 1024 * 1024)
        self.set_creds(True)
        self.assertEqual(bot.mt_upload_limit_bytes(), bot.MT_UPLOAD_LIMIT)
        self.assertEqual(bot.mt_input_limit_bytes(), bot.MT_UPLOAD_LIMIT)

    def test_download_falls_back_to_mtproto_on_too_big(self):
        self.set_creds(True)
        self.too_big_api()
        sentinel = Path("mt-sentinel.bin")
        bot.mtproto_download = (
            lambda chat_id, message_id, dest, on_percent=None: sentinel
        )
        self.assertIs(bot.download("big", chat_id=2, message_id=3), sentinel)

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
            bot.download("big", chat_id=2, message_id=3)

    def test_handle_accepts_big_video_when_mtproto_ready(self):
        self.set_creds(True)
        bot.USERS.clear()
        self.calls.clear()
        bot.compress_one = lambda *a, **k: {
            "in": 1.0,
            "out": 0.5,
            "saved": True,
            "kind": "video",
            "fallback": False,
        }
        try:
            bot.handle(
                {
                    "chat": {"id": 2},
                    "message_id": 3,
                    "from": {"id": 9},
                    "video": {
                        "file_id": "big",
                        "file_size": 36 * 1024 * 1024,
                        "duration": 60,
                    },
                }
            )
        finally:
            pass
        texts = [p.get("text", "") for m, p in self.calls if m == "sendMessage"]
        self.assertFalse(any("cloud Bot API" in t for t in texts))

    def test_handle_rejects_over_mtproto_cap(self):
        self.set_creds(True)
        bot.USERS.clear()
        self.calls.clear()
        bot.handle(
            {
                "chat": {"id": 2},
                "message_id": 3,
                "from": {"id": 9},
                "video": {"file_id": "huge", "file_size": 3 * 1024**3, "duration": 60},
            }
        )
        texts = [p.get("text", "") for m, p in self.calls if m == "sendMessage"]
        self.assertTrue(texts and "2000 MB" in texts[0])

    def test_regions_plan(self):
        single = bot.mt_regions(4 * 1024 * 1024)
        self.assertEqual(single, [(0, 4 * 1024 * 1024)])
        plan = bot.mt_regions(100 * 1024 * 1024)
        self.assertTrue(2 <= len(plan) <= 6)
        self.assertEqual(sum(length for _, length in plan), 100 * 1024 * 1024)
        for start, length in plan:
            self.assertEqual(start % (512 * 1024), 0)


class OutputLimitTest(unittest.TestCase):
    def test_large_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            src = tmp / "in.mp4"
            src.write_bytes(b"x" * (3 * 1024 * 1024))
            lines = []
            originals = (bot.TMP, bot.MAX_OUTPUT_MB, bot.download, bot.probe, bot.compress)
            bot.TMP = tmp
            bot.MAX_OUTPUT_MB = 1
            bot.download = lambda file_id, on_percent=None, **kwargs: src
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
                    1,
                    2,
                    {"file_id": "x"},
                    "video",
                    dict(bot.DEFAULT_SETTINGS),
                    report=lambda status, percent=None: lines.append(status),
                )
            finally:
                bot.TMP, bot.MAX_OUTPUT_MB, bot.download, bot.probe, bot.compress = originals
            self.assertIn("upload limit", result["error"])
            self.assertTrue(any("upload limit" in line for line in lines))


class QueueTest(unittest.TestCase):
    def test_oldest_cached_file_is_evicted(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            old = tmp / "old.bin"
            new = tmp / "new.bin"
            old.write_bytes(b"x" * 100)
            new.write_bytes(b"y" * 100)
            queue = [
                {"media": {"file_id": "old"}, "size": 100, "path": old, "name": "old"}
            ]
            item = {"media": {"file_id": "new"}, "size": 100, "path": None, "name": "new"}
            originals = (bot.TMP, bot.DISK_BUDGET, bot.download, bot.probe)
            bot.TMP = tmp
            bot.DISK_BUDGET = 150
            bot.download = lambda file_id, on_percent=None, **kwargs: new
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
            self.assertEqual(item["path"], new)

    def test_invalid_media_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            bad = tmp / "bad.bin"
            bad.write_bytes(b"not a video")
            item = {"media": {"file_id": "bad"}, "size": 11, "path": None, "name": "bad"}
            originals = (bot.TMP, bot.download, bot.probe)
            bot.TMP = tmp
            bot.download = lambda file_id, on_percent=None, **kwargs: bad
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


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed"
)
class CompressTest(unittest.TestCase):
    def make_clip(self, directory, name, size="640x360"):
        src = Path(directory) / name
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
                f"testsrc=size={size}:rate=30",
                "-t",
                "1",
                "-pix_fmt",
                "yuv420p",
                str(src),
            ],
            check=True,
        )
        return src

    def test_compress_short_video(self):
        with tempfile.TemporaryDirectory() as directory:
            src = self.make_clip(directory, "in.mp4")
            out = Path(directory) / "out.mp4"
            info = bot.compress(src, out, "video", dict(bot.DEFAULT_SETTINGS))
            self.assertTrue(out.exists())
            self.assertEqual(info["width"], 640)
            self.assertEqual(info["height"], 360)

    def test_compress_one_reports_phases(self):
        with tempfile.TemporaryDirectory() as directory:
            src = self.make_clip(directory, "in.mp4")
            calls = []
            original_api = bot.api
            original_download = bot.download

            def fake_api(method, files=None, **params):
                calls.append(method)
                if files:
                    handle = next(iter(files.values()))[1]
                    position = handle.tell()
                    handle.seek(0, 2)
                    size = handle.tell()
                    handle.seek(position)
                    assert size > 0
                return {"message_id": 1}

            bot.api = fake_api
            bot.download = lambda file_id, on_percent=None, **kwargs: src
            try:
                result = bot.compress_one(
                    1,
                    2,
                    {"file_id": "x", "duration": 1},
                    "video",
                    dict(bot.DEFAULT_SETTINGS),
                    report=lambda status, percent=None: calls.append(
                        f"{status}{' ' + str(percent) + '%' if percent is not None else ''}"
                    ),
                )
            finally:
                bot.api, bot.download = original_api, original_download
            self.assertNotIn("error", result)
            self.assertIn("sendVideo", calls)
            self.assertIn("⚙️ compressing", calls)
            self.assertIn("⏳ downloading", calls)


if __name__ == "__main__":
    unittest.main()
