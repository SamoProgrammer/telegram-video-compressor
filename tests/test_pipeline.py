import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from tgcompress.errors import BotError
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

    def test_pre_transcode_cancellation_is_not_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            src = tmp / "in.mp4"
            src.write_bytes(b"x")
            registry = ExecutionRegistry()
            compress = MagicMock()
            pipeline = Pipeline(
                make_config(tmp),
                FakeTransfer(src),
                compress_fn=compress,
                registry=registry,
            )
            registry.cancel(123)

            with self.assertRaisesRegex(Exception, "cancelled"):
                pipeline.transcode_one(src, "video", {}, user_id=123)
            compress.assert_not_called()

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


    def test_cancelled_job_does_not_cancel_next_job(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            src = tmp / "in.mp4"
            src.write_bytes(b"x" * 1000)
            registry = ExecutionRegistry()
            calls = 0
            info = {"width": 640, "height": 360, "duration": 1.0}

            def fake_compress(source, destination, kind, settings, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    registry.cancel(123)
                    raise RuntimeError("ffmpeg killed")
                destination.write_bytes(b"y" * 500)
                return info

            pipeline = Pipeline(
                make_config(tmp),
                FakeTransfer(src),
                probe_fn=lambda _: info,
                compress_fn=fake_compress,
                registry=registry,
            )
            first = pipeline.compress_one(
                1, 2, {"file_id": "x"}, "video", {}, user_id=123
            )
            src.write_bytes(b"x" * 1000)
            second = pipeline.compress_one(
                1, 2, {"file_id": "x"}, "video", {}, user_id=123
            )

            self.assertIn("cancelled", first["error"])
            self.assertNotIn("error", second)
            self.assertEqual(calls, 2)


class ProbeReuseTest(unittest.TestCase):
    def test_single_pipeline_probes_one_input_and_compression_reuses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            src = tmp / "input.mp4"
            src.write_bytes(b"x" * 1000)
            probes = []
            received = {}
            info = {"width": 640, "height": 360, "duration": 1.0}

            def fake_probe(path):
                probes.append(path)
                return info

            def fake_compress(source, destination, kind, settings, **kwargs):
                received.update(kwargs)
                destination.write_bytes(b"compressed")
                return info

            pipeline = Pipeline(
                make_config(tmp),
                FakeTransfer(src),
                probe_fn=fake_probe,
                compress_fn=fake_compress,
            )
            result = pipeline.compress_one(
                1, 2, {"file_id": "x"}, "video", dict(DEFAULT_SETTINGS)
            )
            self.assertNotIn("error", result)
            self.assertEqual(probes, [src])
            self.assertIs(received["probe_info"], info)


class ProcessIsolationTest(unittest.TestCase):
    def test_transcode_does_not_replace_global_popen(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            src = tmp / "in.mp4"
            src.write_bytes(b"x")
            original_popen = subprocess.Popen
            info = {"width": 640, "height": 360, "duration": 1.0}

            def fake_compress(source, destination, kind, settings, **kwargs):
                self.assertIs(subprocess.Popen, original_popen)
                destination.write_bytes(b"y")
                return info

            pipeline = Pipeline(
                make_config(tmp),
                FakeTransfer(src),
                probe_fn=lambda _: info,
                compress_fn=fake_compress,
            )
            pipeline.transcode_one(src, "video", {}, duration=1.0)
            self.assertIs(subprocess.Popen, original_popen)


class CloudLimitMessageTest(unittest.TestCase):
    def test_cloud_only_preflight_reports_enforced_routing_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            src = tmp / "in.mp4"
            src.write_bytes(b"x" * 1000)
            info = {"width": 640, "height": 360, "duration": 1.0}

            def fake_compress(source, destination, kind, settings, **kwargs):
                with destination.open("wb") as handle:
                    handle.truncate(int(49.5 * 1024 * 1024))
                return info

            pipeline = Pipeline(
                make_config(tmp),
                FakeTransfer(src),
                compress_fn=fake_compress,
            )
            with self.assertRaisesRegex(BotError, "49\\.0 MiB") as caught:
                pipeline.transcode_one(src, "video", {}, duration=1.0)
            self.assertNotIn("50 MB", str(caught.exception))


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


class DeliveryCommitTest(unittest.TestCase):
    def test_successful_send_survives_final_report_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            src = tmp / "in.mp4"
            src.write_bytes(b"x" * 1000)
            config = make_config(tmp)
            transfer = FakeTransfer(src)
            info = {
                "width": 640,
                "height": 360,
                "duration": 1.0,
                "has_audio": False,
            }

            def fake_compress(source, destination, kind, settings, **kwargs):
                destination.write_bytes(b"y" * 500)
                return info

            def report(status, percent=None):
                if status.startswith("✅"):
                    from tgcompress.errors import BotError

                    raise BotError("status repaint failed")

            pipeline = Pipeline(
                config,
                transfer,
                probe_fn=lambda _: info,
                compress_fn=fake_compress,
            )
            result = pipeline.compress_one(
                1, 2, {"file_id": "x"}, "video", dict(DEFAULT_SETTINGS), report=report
            )

            self.assertNotIn("error", result)
            self.assertIn("sendVideo", transfer.events)
            self.assertTrue(result["saved"])


if __name__ == "__main__":
    unittest.main()
