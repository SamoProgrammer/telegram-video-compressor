import concurrent.futures
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tgcompress.errors import Defer
from tgcompress.state import StateError
from tgcompress.media import DEFAULT_SETTINGS
from tgcompress.pipeline import ExecutionRegistry

from .support import item, make_stack

REAL_THREAD_POOL_EXECUTOR = concurrent.futures.ThreadPoolExecutor


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

    def __init__(
        self,
        tmp: Path,
        registry: ExecutionRegistry | None = None,
        input_size=1000,
        output_size=1000,
    ) -> None:
        self.tmp = tmp
        self.registry = registry or ExecutionRegistry()
        self.input_size = input_size
        self.output_size = output_size
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
        path.write_bytes(b"d" * self.input_size)
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
        out.write_bytes(b"c" * self.output_size)
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

    def test_direct_download_over_budget_is_cleaned_up(self):
        pipeline = PipelinedPipeline(self.tmp, input_size=1024 * 1024 + 1)
        stack = make_stack(self.tmp, pipeline=pipeline, disk_budget_mb=1)
        state = stack.store.get(1, 10)
        state["settings"] = dict(DEFAULT_SETTINGS)
        state["working"] = [item("vid1", size=1000)]
        state["processing"] = True

        stack.batch.process(1, dict(state["settings"]))

        self.assertEqual(len(state["queue"]), 1)
        self.assertIn("Disk budget exceeded", state["queue"][0]["status"])
        self.assertIsNone(state["queue"][0]["path"])
        self.assertFalse((self.tmp / "vid1.mp4").exists())
        self.assertFalse((self.tmp / "out-vid1.mp4").exists())

    def test_encoded_output_over_budget_is_cleaned_up(self):
        pipeline = PipelinedPipeline(self.tmp, output_size=1024 * 1024)
        stack = make_stack(self.tmp, pipeline=pipeline, disk_budget_mb=1)
        state = stack.store.get(1, 10)
        state["settings"] = dict(DEFAULT_SETTINGS)
        state["working"] = [item("vid1")]
        state["processing"] = True

        stack.batch.process(1, dict(state["settings"]))

        self.assertEqual(len(state["queue"]), 1)
        self.assertIn("Disk budget exceeded", state["queue"][0]["status"])
        self.assertIsNone(state["queue"][0]["path"])
        self.assertIsNone(state["queue"][0].get("_output_path"))
        self.assertFalse((self.tmp / "vid1.mp4").exists())
        self.assertFalse((self.tmp / "out-vid1.mp4").exists())
        self.assertNotIn(("upload", "vid1"), pipeline.events)

    def test_shutdown_waits_for_in_flight_prefetch(self):
        class DeferredPipeline(PipelinedPipeline):
            def __init__(self, tmp):
                super().__init__(tmp)
                self.prefetch_started = threading.Event()
                self.release_prefetch = threading.Event()

            def download_one(self, chat_id, message_id, media, report=None):
                if media["file_id"] == "vid2":
                    self.prefetch_started.set()
                    self.release_prefetch.wait()
                return super().download_one(chat_id, message_id, media, report)

            def transcode_one(self, *args, **kwargs):
                raise Defer()

        pipeline = DeferredPipeline(self.tmp)
        stack = make_stack(self.tmp, pipeline=pipeline)
        state = stack.store.get(1, 10)
        state["settings"] = dict(DEFAULT_SETTINGS)
        state["working"] = [item("vid1"), item("vid2")]
        state["processing"] = True
        returned = threading.Event()

        def run():
            try:
                stack.batch.process(1, dict(state["settings"]))
            finally:
                returned.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(pipeline.prefetch_started.wait(1))
        try:
            self.assertFalse(returned.wait(0.05))
        finally:
            pipeline.release_prefetch.set()
            worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertTrue(returned.is_set())
        self.assertFalse((self.tmp / "vid1.mp4").exists())
        self.assertFalse((self.tmp / "vid2.mp4").exists())
        self.assertTrue(all(i.get("path") is None for i in state["queue"]))

    def test_immediate_cancel_kills_process(self):
        registry = ExecutionRegistry()
        mock_proc = MagicMock()
        registry.register((10, 1), mock_proc)

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
    def test_process_uses_requested_conversation(self):
        pipeline = FakePipeline(
            (
                {
                    "in": 1.0,
                    "out": 0.5,
                    "saved": True,
                    "kind": "video",
                    "fallback": False,
                },
            )
        )
        stack = make_stack(self.tmp, pipeline=pipeline)
        first = stack.store.get(1, 10)
        second = stack.store.get(1, 20)
        first["working"] = [item("first", chat_id=10)]
        first["processing"] = True
        second["working"] = [item("second", chat_id=20)]
        second["processing"] = True

        stack.batch.process(1, dict(second["settings"]), chat_id=20)

        self.assertEqual(pipeline.calls, 1)
        self.assertTrue(first["processing"])
        self.assertEqual(len(first["working"]), 1)
        self.assertFalse(second["processing"])
        self.assertEqual(second["queue"], [])

    def test_start_persists_resume_and_success_records_receipt_once(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingPipeline(FakePipeline):
            def compress_one(self, *args, **kwargs):
                entered.set()
                release.wait(1)
                return super().compress_one(*args, **kwargs)

        pipeline = BlockingPipeline(
            (
                {
                    "in": 1.0,
                    "out": 0.5,
                    "saved": True,
                    "kind": "video",
                    "fallback": False,
                },
            )
        )
        stack = make_stack(self.tmp, pipeline=pipeline)
        state = stack.store.get(1, 10)
        state["queue"] = [item("a")]

        self.assertIsNone(stack.batch.start(state))
        self.assertTrue(entered.wait(1))
        payload = json.loads(stack.config.state_file.read_text())
        saved = next(iter(payload.values()))
        self.assertTrue(saved["resume"])
        release.set()
        self.assertFalse(stack.batch.join_all(time.time() + 1))

        self.assertFalse(state["resume"])
        self.assertEqual(state["receipts"], [item("a")["message_id"]])
        state["working"] = [item("a")]
        state["processing"] = True
        stack.batch.process(1, dict(state["settings"]), chat_id=10)
        self.assertEqual(pipeline.calls, 1)

    def test_defer_is_resumable_but_cancel_is_not(self):
        pipeline = FakePipeline((Defer(),))
        stack = make_stack(self.tmp, pipeline=pipeline)
        state = stack.store.get(1, 10)
        state["working"] = [item("a")]
        state["processing"] = True
        state["resume"] = True

        stack.batch.process(1, dict(state["settings"]), chat_id=10)
        self.assertTrue(state["resume"])

        state["working"] = [item("a")]
        state["processing"] = True
        state["cancel"] = True
        stack.batch.process(1, dict(state["settings"]), chat_id=10)
        self.assertFalse(state["resume"])


class BatchFailureBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)

    def test_start_save_failure_rolls_back_to_queue(self):
        stack = make_stack(self.tmp)
        state = stack.store.get(1, 10)
        state["settings"] = dict(DEFAULT_SETTINGS)
        state["queue"] = [item("a")]
        with patch.object(
            stack.store, "save", side_effect=StateError("start: read-only")
        ):
            with self.assertRaises(StateError):
                stack.batch.start(state, chat_id=10)
        self.assertEqual(state["working"], [])
        self.assertEqual(len(state["queue"]), 1)
        self.assertFalse(state["processing"])
        self.assertTrue(state["resume"])

    def test_cancelled_prefetch_is_not_marked_failed(self):
        class SelectiveExecutor:
            def __init__(self, *args, **kwargs):
                self.download_calls = 0
                self.executor = REAL_THREAD_POOL_EXECUTOR(*args, **kwargs)

            def submit(self, fn, *args):
                if getattr(fn, "__name__", "") != "do_download":
                    return self.executor.submit(fn, *args)
                self.download_calls += 1
                if self.download_calls == 2:
                    future = concurrent.futures.Future()
                    future.cancel()
                    return future
                return self.executor.submit(fn, *args)

            def shutdown(self, wait=True, cancel_futures=False):
                self.executor.shutdown(wait=wait, cancel_futures=cancel_futures)

        pipeline = PipelinedPipeline(self.tmp)
        stack = make_stack(self.tmp, pipeline=pipeline)
        state = stack.store.get(1, 10)
        state["settings"] = dict(DEFAULT_SETTINGS)
        state["working"] = [item("a"), item("b"), item("c")]
        state["processing"] = True

        with patch(
            "tgcompress.batch.concurrent.futures.ThreadPoolExecutor",
            SelectiveExecutor,
        ):
            stack.batch.process(1, dict(state["settings"]), chat_id=10)

        self.assertIn("✅ Delivered 3/3", state["summary"])
        self.assertNotIn("failed", state["summary"])
        self.assertEqual(state["queue"], [])

    def test_upload_refresh_failure_keeps_delivered_item_delivered(self):
        pipeline = PipelinedPipeline(self.tmp)
        stack = make_stack(self.tmp, pipeline=pipeline)
        state = stack.store.get(1, 10)
        state["settings"] = dict(DEFAULT_SETTINGS)
        state["working"] = [item("a")]
        state["processing"] = True

        def failing_refresh(refresh_state):
            raise StateError("card save failed")

        stack.cards.refresh = failing_refresh
        stack.batch.process(1, dict(state["settings"]), chat_id=10)

        self.assertEqual(state["receipts"], [41])
        self.assertIn("✅ Delivered 1/1", state["summary"])
        self.assertNotIn("failed", state["summary"])
        self.assertNotIn("❌", state["summary"])


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
