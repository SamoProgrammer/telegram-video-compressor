import tempfile
import unittest
from pathlib import Path

from tgcompress.errors import BotError
from tgcompress.queue import BatchCache, PrefetchQueue

from .support import make_config


class FakeTransfer:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.downloads = []

    def download(self, file_id, on_percent=None, **kwargs):
        dest = self.base_dir / f"downloaded-{file_id}.bin"
        dest.write_bytes(b"x" * 1024)
        self.downloads.append(file_id)
        return dest


def info(path):
    return {"width": 640, "height": 360, "duration": 1.0, "has_audio": False}


class QueueEvictionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)

    def test_fifo_head_is_never_evicted(self):
        size = 800 * 1024
        file0 = self.tmp / "v0.bin"
        file1 = self.tmp / "v1.bin"
        file2 = self.tmp / "v2.bin"
        file0.write_bytes(b"0" * size)
        file1.write_bytes(b"1" * size)
        file2.write_bytes(b"2" * size)

        # Budget allows 2 items (2 MB = 2048 KB; 2 items = 1600 KB, 3 items = 2400 KB)
        config = make_config(self.tmp, disk_budget_mb=2)

        class DirectTransfer:
            def download(self, file_id, on_percent=None, **kwargs):
                return file2

        cache = BatchCache(config, DirectTransfer(), probe_fn=info, max_on_disk=10)

        item0 = {"media": {"file_id": "0"}, "size": size, "path": file0, "name": "0"}
        item1 = {"media": {"file_id": "1"}, "size": size, "path": file1, "name": "1"}
        item2 = {"media": {"file_id": "2"}, "size": size, "path": None, "name": "2"}

        queue = [item0, item1]

        # Caching item2 requires evicting. Tail item1 should be evicted, NEVER item0!
        res = cache.cache_item(item2, queue)
        self.assertIsNone(res)
        self.assertTrue(file0.exists())
        self.assertEqual(item0["path"], file0)
        self.assertFalse(file1.exists())
        self.assertIsNone(item1["path"])
        self.assertEqual(item2["path"], file2)

    def test_single_item_in_queue_is_preserved_when_new_item_does_not_fit(self):
        size = 800 * 1024
        file0 = self.tmp / "v0.bin"
        file1 = self.tmp / "v1.bin"
        file0.write_bytes(b"0" * size)
        file1.write_bytes(b"1" * size)

        config = make_config(self.tmp, disk_budget_mb=1)  # 1MB budget

        class DirectTransfer:
            def download(self, file_id, on_percent=None, **kwargs):
                return file1

        cache = BatchCache(config, DirectTransfer(), probe_fn=info)

        item0 = {"media": {"file_id": "0"}, "size": size, "path": file0, "name": "0"}
        item1 = {"media": {"file_id": "1"}, "size": size, "path": None, "name": "1"}
        queue = [item0]

        # Item1 cannot fit with item0. Item0 (head) must NOT be evicted!
        res = cache.cache_item(item1, queue)
        self.assertIsNone(res)
        self.assertTrue(file0.exists())
        self.assertEqual(item0["path"], file0)
        self.assertIsNone(item1["path"])

    def test_max_on_disk_caps_eager_downloads(self):
        size = 100 * 1024
        file0 = self.tmp / "v0.bin"
        file1 = self.tmp / "v1.bin"
        file2 = self.tmp / "v2.bin"
        file0.write_bytes(b"0" * size)
        file1.write_bytes(b"1" * size)
        file2.write_bytes(b"2" * size)

        config = make_config(self.tmp, disk_budget_mb=100)
        cache = BatchCache(config, None, probe_fn=info, max_on_disk=2)

        item0 = {"media": {"file_id": "0"}, "size": size, "path": file0}
        item1 = {"media": {"file_id": "1"}, "size": size, "path": file1}
        item2 = {"media": {"file_id": "2"}, "size": size, "path": None}
        queue = [item0, item1]

        # 2 items are already on disk. Item2 should not be downloaded upfront.
        res = cache.cache_item(item2, queue)
        self.assertIsNone(res)
        self.assertIsNone(item2["path"])
        self.assertTrue(file0.exists())
        self.assertTrue(file1.exists())


class MediaValidationTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)

    def test_invalid_media_is_rejected(self):
        bad = self.tmp / "bad.bin"
        bad.write_bytes(b"not a video")
        config = make_config(self.tmp)

        class BadTransfer:
            def download(self, *args, **kwargs):
                return bad

        cache = BatchCache(config, BadTransfer())

        def bad_probe(path):
            raise BotError("No video stream found")

        cache.probe = bad_probe
        item = {
            "media": {"file_id": "bad"},
            "size": 11,
            "path": None,
            "name": "bad",
        }
        error = cache.cache_item(item, [])
        self.assertIn("Not a readable video", error)
        self.assertIsNone(item["path"])
        self.assertFalse(bad.exists())

    def test_overlong_video_is_rejected(self):
        long_file = self.tmp / "long.bin"
        long_file.write_bytes(b"long video")
        config = make_config(self.tmp, max_duration=60)

        class LongTransfer:
            def download(self, *args, **kwargs):
                return long_file

        def long_probe(path):
            return {"width": 640, "height": 360, "duration": 3600.0}

        cache = BatchCache(config, LongTransfer(), probe_fn=long_probe)
        item = {
            "media": {"file_id": "long"},
            "size": 10,
            "path": None,
            "name": "long",
        }
        error = cache.cache_item(item, [])
        self.assertIn("longer than", error)
        self.assertIsNone(item["path"])
        self.assertFalse(long_file.exists())


class PrefetchQueueTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)

    def test_double_buffering_pipeline(self):
        config = make_config(self.tmp)
        transfer = FakeTransfer(self.tmp)
        cache = BatchCache(config, transfer, probe_fn=info)

        items = [
            {"media": {"file_id": "v0"}, "size": 1024, "path": None, "name": "v0"},
            {"media": {"file_id": "v1"}, "size": 1024, "path": None, "name": "v1"},
            {"media": {"file_id": "v2"}, "size": 1024, "path": None, "name": "v2"},
        ]

        seen_on_disk = []

        with cache.prefetch(items, max_buffer=2) as queue:
            for idx, (item, error) in enumerate(queue):
                self.assertIsNone(error)
                self.assertIsNotNone(item["path"])
                self.assertTrue(Path(item["path"]).exists())

                # Count items currently on disk
                on_disk = sum(
                    1 for it in items if it.get("path") and Path(it["path"]).exists()
                )
                seen_on_disk.append(on_disk)

                # Simulate encoding and cleanup of current item
                Path(item["path"]).unlink()
                item["path"] = None

        # Verify double buffering: at any point, on_disk was at most 2
        for count in seen_on_disk:
            self.assertLessEqual(count, 2)

        # All 3 videos were processed
        self.assertEqual(len(seen_on_disk), 3)
        self.assertEqual(transfer.downloads, ["v0", "v1", "v2"])

    def test_prefetch_stop_halts_iteration(self):
        config = make_config(self.tmp)
        transfer = FakeTransfer(self.tmp)
        cache = BatchCache(config, transfer, probe_fn=info)

        items = [
            {"media": {"file_id": f"v{i}"}, "size": 1024, "path": None}
            for i in range(5)
        ]

        processed = 0
        queue = PrefetchQueue(cache, items, max_buffer=2)
        for item, error in queue:
            processed += 1
            if processed == 2:
                queue.stop()

        self.assertEqual(processed, 2)


if __name__ == "__main__":
    unittest.main()
