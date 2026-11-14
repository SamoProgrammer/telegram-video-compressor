import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from tgcompress.metrics import new_job_id, stage


class StageMetricsTest(unittest.TestCase):
    def test_stage_logs_structured_duration_throughput_and_correlation(self):
        output = StringIO()
        started = time.monotonic()
        with redirect_stdout(output):
            with stage(
                "download",
                job_id="job-123",
                route="mtproto",
                connections=4,
                bytes=1048576,
                started=started - 1,
            ):
                pass
        event = json.loads(output.getvalue())
        self.assertEqual(event["event"], "pipeline_stage")
        self.assertEqual(event["stage"], "download")
        self.assertEqual(event["job"], "job-123")
        self.assertEqual(event["status"], "ok")
        self.assertEqual(event["route"], "mtproto")
        self.assertEqual(event["connections"], 4)
        self.assertEqual(event["bytes"], 1048576)
        self.assertAlmostEqual(event["mib_s"], 1.0, places=1)
        self.assertGreater(event["milliseconds"], 0)

    def test_stage_failure_and_transport_error_do_not_hide_original_error(self):
        output = StringIO()
        with redirect_stdout(output):
            with self.assertRaisesRegex(ValueError, "network failed"):
                with stage("upload", route="mtproto", bytes=2048) as details:
                    raise ValueError("network failed: token must stay unseen")
        event = json.loads(output.getvalue())
        self.assertEqual(event["status"], "error")
        self.assertEqual(event["error_type"], "ValueError")
        self.assertEqual(event["bytes"], 0)
        self.assertEqual(event["mib_s"], 0)
        self.assertNotIn("token", output.getvalue())

    def test_profiled_ffmpeg_logs_cpu_as_process_wide_delta(self):
        from tgcompress import media

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.mp4"
            output.write_bytes(b"encoded")
            stdout = StringIO()
            command = [
                "ffmpeg", "-c:v", "libx264", "-preset", "veryfast",
                "-threads", "0", str(output),
            ]
            with patch.object(media, "_run_ffmpeg_step"), redirect_stdout(stdout):
                media._profiled_ffmpeg_step(
                    command,
                    "profile-job",
                    err_file=Path(directory) / "encode.err",
                    duration=1.0,
                )
            event = json.loads(stdout.getvalue())
            self.assertEqual(event["job"], "profile-job")
            self.assertEqual(event["pass"], "crf")
            self.assertEqual(event["bytes"], 7)
            self.assertIn("child_process_cpu_seconds", event)
            self.assertNotIn("child_cpu_seconds", event)

    def test_job_ids_are_not_user_identifiers(self):
        job_id = new_job_id()
        self.assertEqual(len(job_id), 12)
        self.assertNotEqual(job_id, new_job_id())


if __name__ == "__main__":
    unittest.main()
