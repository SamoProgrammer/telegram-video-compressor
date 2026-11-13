import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from tgcompress.media import DEFAULT_SETTINGS
from tgcompress.pipeline import ExecutionRegistry, Pipeline

from .support import make_config


class FakeTransfer:
    def __init__(self, src: Path) -> None:
        self.src = src
        self.events: list[str] = []

    def download(self, file_id, on_percent=None, **kwargs):
        if on_percent:
            on_percent(50)
        return self.src

    def send_original(self, *args, **kwargs):
        self.events.append("sendOriginal")

    def send_result(self, *args, **kwargs):
        self.events.append("sendVideo")


class OutputLimitTest(unittest.TestCase):
    def test_large_output_is_rejected_before_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            src = tmp / "in.mp4"
            src.write_bytes(b"x" * (3 * 1024 * 1024))
            config = make_config(tmp, max_output_mb=1)
            transfer = FakeTransfer(src)
            lines = []

            def fake_probe(path):
                return {
                    "width": 640,
                    "height": 360,
                    "duration": 1.0,
                    "has_audio": False,
                }

            def fake_compress(source, destination, kind, settings, **kwargs):
                destination.write_bytes(b"y" * (2 * 1024 * 1024))
                return fake_probe(destination)

            pipeline = Pipeline(
                config,
                transfer,
                probe_fn=fake_probe,
                compress_fn=fake_compress,
            )
            result = pipeline.compress_one(
                1,
                2,
                {"file_id": "x"},
                "video",
                dict(DEFAULT_SETTINGS),
                report=lambda status, percent=None: lines.append(status),
            )
            self.assertIn("upload limit", result["error"])
            self.assertTrue(any("upload limit" in line for line in lines))
            self.assertEqual(transfer.events, [])


class SizeSavingsRuleTest(unittest.TestCase):
    def test_savings_under_two_percent_sends_original(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            src = tmp / "in.mp4"
            src.write_bytes(b"x" * 1000)
            config = make_config(tmp)
            transfer = FakeTransfer(src)

            def fake_probe(path):
                return {
                    "width": 640,
                    "height": 360,
                    "duration": 1.0,
                    "has_audio": False,
                }

            def fake_compress(source, destination, kind, settings, **kwargs):
                # 990 bytes out of 1000 = 1% savings (< 2%)
                destination.write_bytes(b"y" * 990)
                return fake_probe(destination)

            pipeline = Pipeline(
                config,
                transfer,
                probe_fn=fake_probe,
                compress_fn=fake_compress,
            )
            result = pipeline.compress_one(
                1,
                2,
                {"file_id": "x"},
                "video",
                dict(DEFAULT_SETTINGS),
            )
            self.assertFalse(result["saved"])
            self.assertIn("sendOriginal", transfer.events)
            self.assertNotIn("sendVideo", transfer.events)

    def test_savings_over_two_percent_sends_compressed(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            src = tmp / "in.mp4"
            src.write_bytes(b"x" * 1000)
            config = make_config(tmp)
            transfer = FakeTransfer(src)

            def fake_probe(path):
                return {
                    "width": 640,
                    "height": 360,
                    "duration": 1.0,
                    "has_audio": False,
                }

            def fake_compress(source, destination, kind, settings, **kwargs):
                # 500 bytes out of 1000 = 50% savings
                destination.write_bytes(b"y" * 500)
                return fake_probe(destination)

            pipeline = Pipeline(
                config,
                transfer,
                probe_fn=fake_probe,
                compress_fn=fake_compress,
            )
            result = pipeline.compress_one(
                1,
                2,
                {"file_id": "x"},
                "video",
                dict(DEFAULT_SETTINGS, codec="hevc"),
            )
            self.assertTrue(result["saved"])
            self.assertIn("sendVideo", transfer.events)
            self.assertNotIn("sendOriginal", transfer.events)


class ExecutionRegistryTest(unittest.TestCase):
    def test_register_and_cancel_process(self):
        registry = ExecutionRegistry()
        mock_proc = MagicMock()
        registry.register(42, mock_proc)

        cancelled = registry.cancel(42)
        self.assertTrue(cancelled)
        mock_proc.kill.assert_called_once()
        self.assertTrue(registry.is_cancelled(42))

    def test_cancel_all(self):
        registry = ExecutionRegistry()
        p1 = MagicMock()
        p2 = MagicMock()
        registry.register(1, p1)
        registry.register(2, p2)

        registry.cancel_all()
        p1.kill.assert_called_once()
        p2.kill.assert_called_once()

    def test_cancellation_during_transcode(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            src = tmp / "in.mp4"
            src.write_bytes(b"x" * 1000)
            config = make_config(tmp)
            transfer = FakeTransfer(src)
            registry = ExecutionRegistry()

            def fake_probe(path):
                return {
                    "width": 640,
                    "height": 360,
                    "duration": 1.0,
                    "has_audio": False,
                }

            def fake_compress(source, destination, kind, settings, **kwargs):
                registry.cancel(123)
                raise RuntimeError("ffmpeg killed")

            pipeline = Pipeline(
                config,
                transfer,
                probe_fn=fake_probe,
                compress_fn=fake_compress,
                registry=registry,
            )
            result = pipeline.compress_one(
                1,
                2,
                {"file_id": "x"},
                "video",
                dict(DEFAULT_SETTINGS),
                user_id=123,
            )
            self.assertIn("Transcode cancelled", result.get("error", ""))


class SettingsPassThroughTest(unittest.TestCase):
    def test_new_compression_options_passed(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            src = tmp / "in.mp4"
            src.write_bytes(b"x" * 1000)
            config = make_config(tmp)
            transfer = FakeTransfer(src)
            captured_kwargs = {}

            def fake_probe(path):
                return {
                    "width": 640,
                    "height": 360,
                    "duration": 1.0,
                    "has_audio": False,
                }

            def fake_compress(source, destination, kind, settings, **kwargs):
                captured_kwargs.update(kwargs)
                destination.write_bytes(b"y" * 500)
                return fake_probe(destination)

            pipeline = Pipeline(
                config,
                transfer,
                probe_fn=fake_probe,
                compress_fn=fake_compress,
            )
            settings = dict(
                DEFAULT_SETTINGS,
                codec="av1",
                mode="target",
                crf=26,
                target_size="20mb",
                fps="30",
                audio="opus_64k",
                tune="film",
                tonemap=True,
            )
            pipeline.compress_one(
                1,
                2,
                {"file_id": "x"},
                "video",
                settings,
            )
            for key in (
                "codec",
                "mode",
                "crf",
                "target_size",
                "fps",
                "audio",
                "tune",
                "tonemap",
            ):
                self.assertIn(key, captured_kwargs)
                self.assertEqual(captured_kwargs[key], settings[key])


class DecoupledStagesTest(unittest.TestCase):
    def test_download_transcode_upload_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            src = tmp / "in.mp4"
            src.write_bytes(b"x" * 1000)
            config = make_config(tmp)
            transfer = FakeTransfer(src)

            def fake_probe(path):
                return {
                    "width": 640,
                    "height": 360,
                    "duration": 1.0,
                    "has_audio": False,
                }

            def fake_compress(source, destination, kind, settings, **kwargs):
                destination.write_bytes(b"y" * 500)
                return fake_probe(destination)

            pipeline = Pipeline(
                config,
                transfer,
                probe_fn=fake_probe,
                compress_fn=fake_compress,
            )

            # 1. Download stage
            downloaded = pipeline.download_one(1, 2, {"file_id": "x"})
            self.assertEqual(downloaded, src)

            # 2. Transcode stage
            t_res = pipeline.transcode_one(
                src=downloaded,
                kind="video",
                settings=dict(DEFAULT_SETTINGS),
                duration=1.0,
            )
            self.assertTrue(t_res["out"].exists())

            # 3. Upload stage
            up_res = pipeline.upload_one(
                chat_id=1,
                reply_to=2,
                media={"file_id": "x"},
                src=downloaded,
                out=t_res["out"],
                kind="video",
                out_kind=t_res["out_kind"],
                info=t_res["info"],
                settings=dict(DEFAULT_SETTINGS),
                in_mb=t_res["in_mb"],
                out_mb=t_res["out_mb"],
            )
            self.assertTrue(up_res["saved"])
            self.assertIn("sendVideo", transfer.events)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed"
)
class PhasesTest(unittest.TestCase):
    def test_reports_download_compress_and_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            src = tmp / "in.mp4"
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
                    "-crf",
                    "10",
                    str(src),
                ],
                check=True,
            )
            config = make_config(tmp)
            transfer = FakeTransfer(src)
            events = []
            pipeline = Pipeline(config, transfer)
            result = pipeline.compress_one(
                1,
                2,
                {"file_id": "x", "duration": 1},
                "video",
                dict(DEFAULT_SETTINGS),
                report=lambda status, percent=None: events.append(
                    f"{status}{' ' + str(percent) + '%' if percent is not None else ''}"
                ),
            )
            self.assertNotIn("error", result)
            self.assertIn("sendVideo", transfer.events)
            self.assertTrue(any("compressing" in event for event in events))
            self.assertTrue(any("downloading" in event for event in events))


if __name__ == "__main__":
    unittest.main()
