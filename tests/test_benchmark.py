import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import benchmark


class BenchmarkCommandTest(unittest.TestCase):
    def test_svt_speed_candidate_updates_both_preset_options(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            output = root / "output.mp4"
            source.write_bytes(b"not probed")
            seen = []

            def fake_run(command):
                seen.append(command)
                Path(command[-1]).write_bytes(b"output")

            info = {"width": 1280, "height": 720, "duration": 1, "has_audio": False}
            with patch.object(benchmark, "run", fake_run), patch.object(
                benchmark, "probe", return_value=info
            ):
                benchmark.encode(source, output, "av1", {"-preset": "10"})

            command = seen[0]
            self.assertEqual(command[command.index("-preset") + 1], "10")
            self.assertEqual(command[command.index("-svtav1-params") + 1], "preset=10")

    def test_vp9_candidate_sets_cpu_used_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            output = root / "output.webm"
            source.write_bytes(b"not probed")
            seen = []

            def fake_run(command):
                seen.append(command)
                Path(command[-1]).write_bytes(b"output")

            info = {"width": 1280, "height": 720, "duration": 1, "has_audio": False}
            with patch.object(benchmark, "run", fake_run), patch.object(
                benchmark, "probe", return_value=info
            ):
                benchmark.encode(source, output, "vp9", {"-cpu-used": "3"})

            command = seen[0]
            self.assertEqual(command[command.index("-cpu-used") + 1], "3")
            self.assertEqual(command[-1], str(output))


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class QualityEvaluationTest(unittest.TestCase):
    def test_quality_uses_supported_float_ssim_feature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = benchmark.make_fixture(root, "motion", 0.2)
            output = root / "encoded.mp4"
            record = benchmark.encode(source, output, "h264", {})
            scores = benchmark.quality(
                source,
                output,
                record["width"],
                record["height"],
                root / "scores.json",
            )
            self.assertGreaterEqual(scores["vmaf"], 0)
            self.assertLessEqual(scores["vmaf"], 100)
            self.assertGreaterEqual(scores["ssim"], 0)
            self.assertLessEqual(scores["ssim"], 1)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed"
)
class AudioBearingCodecTest(unittest.TestCase):
    def test_vp9_benchmark_supports_an_aac_mp4_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "audio-bearing.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
                "-t", "0.2", "-map", "0:v", "-map", "1:a",
                "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "96k", str(source),
            ], check=True, capture_output=True, timeout=30)

            encoded = root / "vp9-output.mp4"
            benchmark.encode(source, encoded, "vp9", {})
            metadata = benchmark.probe(encoded)
            self.assertEqual(metadata["codec_name"], "vp9")
            self.assertEqual(metadata["audio_codec"], "aac")


class BenchmarkFailureSafetyTest(unittest.TestCase):
    def test_input_path_cannot_leak_through_benchmark_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "private-media-path.mp4"
            private.write_bytes(b"media")
            failure = subprocess.TimeoutExpired(["ffmpeg", str(private)], 360)
            with patch("sys.argv", ["benchmark", "--input", str(private)]), patch.object(
                benchmark, "encode", side_effect=failure
            ):
                with self.assertRaises(RuntimeError) as caught:
                    benchmark.main()
            self.assertNotIn(str(private), str(caught.exception))
            self.assertIn("TimeoutExpired", str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
