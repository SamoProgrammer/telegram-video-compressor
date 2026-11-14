"""Batch cache: double-buffering prefetcher and disk budget enforcement."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

from .config import Config
from .errors import BotError
from .files import safe_unlink
from .media import probe
from .transfer import Transfer

MAX_ON_DISK = 2


class BatchCache:
    def __init__(
        self,
        config: Config,
        transfer: Transfer,
        probe_fn=probe,
        max_on_disk: int = MAX_ON_DISK,
    ) -> None:
        self.config = config
        self.transfer = transfer
        self.probe = probe_fn
        self.max_on_disk = max_on_disk

    def used(self, queue: list) -> int:
        total = 0
        for item in queue:
            path = item.get("path")
            if path and Path(path).exists():
                total += Path(path).stat().st_size
        return total

    def cached_count(self, queue: list) -> int:
        return sum(
            1 for item in queue if item.get("path") and Path(item["path"]).exists()
        )

    def cache_item(self, item: dict, queue: list, on_percent=None) -> str | None:
        """Cache a queued video if within disk budget and double-buffer capacity.

        Eliminates inverted FIFO eviction: never evicts the queue head (queue[0]).
        Caps disk consumption to at most max_on_disk items on disk.
        """
        size = item.get("size", 0)
        if size > self.config.disk_budget_bytes:
            item["path"] = None
            return None

        # Keep 1 GB free for ffmpeg outputs and the rest of the runner.
        try:
            free = shutil.disk_usage(self.config.work_dir).free
            if free < size + 1024 * 1024 * 1024:
                item["path"] = None
                return None
        except OSError:
            pass

        # If already at max_on_disk items on disk, do not eagerly download upfront.
        if self.cached_count(queue) >= self.max_on_disk:
            item["path"] = None
            return None

        # If adding this item exceeds the disk budget, evict from tail (furthest from execution).
        # NEVER evict queue[0] (the head of the FIFO queue).
        while queue and self.used(queue) + size > self.config.disk_budget_bytes:
            victim = next((q for q in reversed(queue[1:]) if q.get("path")), None)
            if victim is None:
                item["path"] = None
                return None
            safe_unlink(victim["path"], self.config.work_dir)
            victim["path"] = None

        return self.download_and_probe(item, on_percent)

    def download_and_probe(self, item: dict, on_percent=None) -> str | None:
        try:
            path = self.transfer.download(
                item["media"]["file_id"],
                on_percent,
                chat_id=item.get("chat_id"),
                message_id=item.get("message_id"),
            )
        except BotError as exc:
            return f"Could not download: {self.config.redact(exc)}"
        try:
            info = self.probe(path)
        except BotError as exc:
            safe_unlink(path, self.config.work_dir)
            return f"Not a readable video: {self.config.redact(exc)}"
        item["path"] = path
        item["duration"] = info["duration"]
        if info["duration"] > self.config.max_duration:
            safe_unlink(path, self.config.work_dir)
            item["path"] = None
            return f"Video is longer than {self.config.max_duration // 60} min."
        return None

    def prefetch(self, items: list[dict], max_buffer: int = 2) -> PrefetchQueue:
        return PrefetchQueue(self, items, max_buffer=max_buffer)


class PrefetchQueue:
    """Standalone JIT prefetcher retained for API compatibility.

    Production BatchRunner owns its executor directly so shutdown joins transfers.
    Coordinates execution such that while item N is actively being processed/encoded,
    item N+1 is being prefetched in the background.
    Maintains at most max_buffer (default 2) items on disk simultaneously.
    """

    def __init__(
        self,
        cache: BatchCache,
        items: list[dict],
        max_buffer: int = MAX_ON_DISK,
    ) -> None:
        self.cache = cache
        self.items = items
        self.max_buffer = max_buffer
        self._threads: dict[int, threading.Thread] = {}
        self._errors: dict[int, str] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    def start_prefetch(self, index: int) -> None:
        with self._lock:
            if self._stop_event.is_set():
                return
            if index < 0 or index >= len(self.items):
                return
            if index in self._threads:
                return
            item = self.items[index]
            if item.get("path") and Path(item["path"]).exists():
                return

            def worker():
                error = self.cache.download_and_probe(item)
                if error:
                    with self._lock:
                        self._errors[index] = error

            t = threading.Thread(
                target=worker,
                name=f"prefetch-{index}",
                daemon=True,
            )
            self._threads[index] = t
            t.start()

    def get(self, index: int) -> tuple[dict, str | None]:
        if index < 0 or index >= len(self.items):
            raise IndexError("Index out of range")

        with self._lock:
            thread = self._threads.get(index)

        if thread and thread.is_alive():
            thread.join()

        item = self.items[index]
        with self._lock:
            error = self._errors.get(index)

        if not item.get("path") and not error:
            error = self.cache.download_and_probe(item)

        return item, error

    def stop(self) -> None:
        self._stop_event.set()

    def __iter__(self):
        if not self.items:
            return

        for i in range(len(self.items)):
            if self._stop_event.is_set():
                break
            item, error = self.get(i)
            # While consumer processes item i, prefetch next item in background
            if i + 1 < len(self.items) and self.max_buffer >= 2:
                self.start_prefetch(i + 1)
            yield item, error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
