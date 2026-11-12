import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import bot


class ApiTest(unittest.TestCase):
    def test_form_encoding(self):
        captured = {}
        original = bot.SESSION.post

        class Response:
            status_code = 200

            def json(self):
                return {"ok": True, "result": {}}

        def fake_post(url, data=None, files=None, timeout=None):
            captured["data"] = data
            return Response()

        bot.SESSION.post = fake_post
        try:
            bot.api(
                "sendMessage",
                chat_id=1,
                text="hi",
                reply_parameters='{"message_id": 2}',
                drop_pending_updates=False,
            )
        finally:
            bot.SESSION.post = original
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

            def fake_download(file_id):
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
            self.assertEqual(
                methods,
                [
                    "sendMessage",
                    "editMessageText",
                    "editMessageText",
                    "sendVideo",
                    "editMessageText",
                ],
            )
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


if __name__ == "__main__":
    unittest.main()
