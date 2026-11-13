import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from tgcompress.errors import Defer
from tgcompress.media import DEFAULT_SETTINGS
from tgcompress.pipeline import ExecutionRegistry

from .support import item, make_stack


class FakePipeline:
    def __init__(self, results) -> None:
        self.results = iter(results)
        self.calls = 0

    def compress_one(self, *args, **kwargs):
        self.calls += 1
        report = kwargs.get("report")
        if report:
            report("⚙️ compressing", 40)
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


class PipelinedPipeline:
    """Mock pipeline that exposes decoupled download_one, transcode_one, and upload_one."""

    def __init__(self, tmp: Path, registry: ExecutionRegistry | None = None) -> None:
        self.tmp = tmp
        self.registry = registry or ExecutionRegistry()
        self.events: list[tuple[str, str]] = []
        self.active_transcodes = 0
        self.max_concurrent_transcodes = 0
        self.lock = threading.Lock()

    def probe(self, path: Path):
        return {"width": 640, "height": 360, "duration": 1.0, "has_audio": False}

    def download_one(self, chat_id, message_id, media, report=None):
        fid = media.get("file_id", "clip")
        self.events.append(("download", fid))
        path = self.tmp / f"{fid}.mp4"
        path.write_bytes(b"dummy" * 200)
        time.sleep(0.01)
        return path

    def transcode_one(
        self,
        src,
        kind,
        settings,
        duration=1.0,
        deadline=None,
        report=None,
        user_id=None,
    ):
        name = src.stem
        with self.lock:
            self.active_transcodes += 1
            self.max_concurrent_transcodes = max(
                self.max_concurrent_transcodes, self.active_transcodes
            )
        self.events.append(("transcode", name))
        out = self.tmp / f"out-{name}.mp4"
        out.write_bytes(b"compressed" * 100)
        time.sleep(0.02)
        with self.lock:
            self.active_transcodes -= 1
        return {
            "out": out,
            "info": {},
            "out_kind": "video",
            "fallback": False,
            "in_mb": 1.0,
            "out_mb": 0.5,
        }

    def upload_one(
        self,
        chat_id,
        reply_to,
        media,
        src,
        out,
        kind,
        out_kind,
        info,
        settings,
        in_mb,
        out_mb,
        filename="compressed.mp4",
        fallback=False,
        report=None,
    ):
        fid = media.get("file_id", "clip")
        self.events.append(("upload", fid))
        time.sleep(0.01)
        if out and out.exists():
            out.unlink()
        if src and src.exists():
            src.unlink()
        return {
            "in": in_mb,
            "out": out_mb,
            "saved": True,
            "kind": out_kind,
            "fallback": fallback,
        }


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)

    def test_mixed_success_and_failure(self):
        pipeline = FakePipeline(
            (
                {
                    "in": 10.0,
                    "out": 2.0,
                    "saved": True,
                    "kind": "video",
                    "fallback": False,
                },
                {"error": "ffmpeg exploded"},
            )
        )
        stack = make_stack(self.tmp, pipeline=pipeline)
        state = stack.store.get(1, 10)
        state["settings"] = dict(DEFAULT_SETTINGS)
        state["working"] = [item("a"), item("b")]
        state["processing"] = True

        stack.batch.process(1, dict(state["settings"]))

        self.assertFalse(state["processing"])
        self.assertEqual(state["working"], [])
        self.assertEqual(len(state["queue"]), 1)
        self.assertTrue(state["queue"][0]["status"].startswith("❌"))
        self.assertIn("Delivered 1/2", state["summary"])
        self.assertIn("1 failed", state["summary"])
        bodies = [
            call[1].get("text", "")
            for call in stack.api.calls
            if call[0] == "editMessageText"
        ]
        self.assertTrue(any("compressing 40%" in body for body in bodies))

    def test_cancel_returns_everything(self):
        pipeline = FakePipeline(())
        stack = make_stack(self.tmp, pipeline=pipeline)
        state = stack.store.get(1, 10)
        state["settings"] = dict(DEFAULT_SETTINGS)
        state["cancel"] = True
        state["working"] = [item("a"), item("b")]
        state["processing"] = True

        stack.batch.process(1, dict(state["settings"]))

        self.assertEqual(pipeline.calls, 0)
        self.assertEqual(len(state["queue"]), 2)
        self.assertIn("Cancelled", state["summary"])

    def test_defer_keeps_remaining(self):
        pipeline = FakePipeline(
            (
                {
                    "in": 1.0,
                    "out": 0.5,
                    "saved": True,
                    "kind": "video",
                    "fallback": False,
                },
                Defer(),
            )
        )
        stack = make_stack(self.tmp, pipeline=pipeline)
        state = stack.store.get(1, 10)
        state["settings"] = dict(DEFAULT_SETTINGS)
        state["working"] = [item("a"), item("b"), item("c")]
        state["processing"] = True

        stack.batch.process(1, dict(state["settings"]))

        self.assertEqual(len(state["queue"]), 2)
        self.assertIn("Shift ending", state["summary"])

    def test_start_guards(self):
        stack = make_stack(self.tmp)
        state = stack.store.get(1, 10)
        self.assertIn("empty", (stack.batch.start(state) or ""))
        state["queue"].append(item("a"))
        state["processing"] = True
        self.assertIn("already", (stack.batch.start(state) or ""))

    def test_pipelined_double_buffering_execution(self):
        pipeline = PipelinedPipeline(self.tmp)
        stack = make_stack(self.tmp, pipeline=pipeline)
        state = stack.store.get(1, 10)
        state["settings"] = dict(DEFAULT_SETTINGS)
        state["working"] = [item("vid1"), item("vid2"), item("vid3")]
        state["processing"] = True

        stack.batch.process(1, dict(state["settings"]))

        self.assertFalse(state["processing"])
        self.assertEqual(len(state["queue"]), 0)
        self.assertIn("Delivered 3/3", state["summary"])

        # Check that download, transcode, upload occurred for all items
        downloads = [e[1] for e in pipeline.events if e[0] == "download"]
        transcodes = [e[1] for e in pipeline.events if e[0] == "transcode"]
        uploads = [e[1] for e in pipeline.events if e[0] == "upload"]

        self.assertEqual(downloads, ["vid1", "vid2", "vid3"])
        self.assertEqual(transcodes, ["vid1", "vid2", "vid3"])
        self.assertEqual(uploads, ["vid1", "vid2", "vid3"])

        # Concurrency must be bounded: at most 1 transcode at a time
        self.assertEqual(pipeline.max_concurrent_transcodes, 1)

    def test_immediate_cancel_kills_process(self):
        registry = ExecutionRegistry()
        mock_proc = MagicMock()
        registry.register(1, mock_proc)

        pipeline = FakePipeline(())
        pipeline.registry = registry
        stack = make_stack(self.tmp, pipeline=pipeline)

        state = stack.store.get(1, 10)
        state["processing"] = True
        state["working"] = [item("a")]

        # Trigger cancel
        stack.batch.cancel(1)

        self.assertTrue(state["cancel"])
        mock_proc.kill.assert_called_once()


class WorkerPoolTest(unittest.TestCase):
    def test_worker_pool_dispatch_and_join(self):
        from tgcompress.app import WorkerPool

        pool = WorkerPool()
        done = threading.Event()

        def worker():
            time.sleep(0.02)
            done.set()

        t = pool.dispatch(worker)
        self.assertTrue(t.is_alive() or done.is_set())
        alive = pool.join_all(time.time() + 1.0)
        self.assertFalse(alive)
        self.assertTrue(done.is_set())
        self.assertEqual(len(pool.workers), 0)


if __name__ == "__main__":
    unittest.main()
