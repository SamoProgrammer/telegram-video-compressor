"""Batch orchestration: bounded concurrency and pipelined double-buffering."""

from __future__ import annotations

import concurrent.futures
import shutil
import threading
import time
from pathlib import Path

from .config import Config
from .errors import BotError, Defer
from .files import safe_unlink
from .metrics import new_job_id
from .pipeline import TRANSCODE_SEMAPHORE
from .state import StateError, conversation_key
from .ui import CardRenderer


class BatchRunner:
    def __init__(
        self,
        config: Config,
        store,
        pipeline,
        cards: CardRenderer,
        transcode_semaphore: threading.Semaphore | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.pipeline = pipeline
        self.cards = cards
        self.transcode_semaphore = (
            transcode_semaphore
            or getattr(pipeline, "transcode_semaphore", None)
            or TRANSCODE_SEMAPHORE
        )
        self.jobs: set[threading.Thread] = set()
        self.jobs_lock = threading.Lock()

    def _check_disk_space(self, items: list[dict], incoming: int = 0) -> None:
        paths = {
            Path(value)
            for item in items
            for key in ("path", "_output_path")
            if (value := item.get(key))
        }
        used = 0
        for path in paths:
            try:
                used += path.stat().st_size
            except OSError:
                pass
        needed = used + incoming
        budget = self.config.disk_budget_bytes
        if needed > budget:
            raise BotError(
                f"Disk budget exceeded: batch files need {needed} bytes; "
                f"the limit is {budget} bytes."
            )
        try:
            free = shutil.disk_usage(self.config.work_dir).free
        except OSError:
            return
        if free < incoming + 1024 * 1024 * 1024:
            raise BotError("Insufficient free disk space; 1 GiB must remain available.")

    def _record_receipt(self, state: dict, message_id: int) -> None:
        try:
            self.store.record_receipt(state, message_id)
        except StateError as exc:
            print(f"batch receipt: {self.config.redact(exc)}", flush=True)

    def start(
        self,
        state: dict,
        deadline: float | None = None,
        chat_id: int | None = None,
    ) -> str | None:
        if state["processing"]:
            return "A batch is already running."
        if not state["queue"]:
            return "Queue is empty. Send videos first."
        if deadline and time.time() + 120 > deadline:
            return "The shift is ending. Start after the next shift."
        chat_id = state.get("chat_id") if chat_id is None else chat_id
        queued = state["queue"]
        state["working"] = [dict(item, status="queued") for item in queued]
        state["queue"] = []
        state["processing"] = True
        state["cancel"] = False
        state["resume"] = True
        state["view"] = "main"
        state["summary"] = ""
        try:
            self.store.save()
        except StateError:
            state["working"] = []
            state["queue"] = queued
            state["processing"] = False
            state["resume"] = True
            raise
        thread = threading.Thread(
            target=self.process,
            args=(state["user_id"], dict(state["settings"]), deadline, chat_id),
            daemon=True,
        )
        with self.jobs_lock:
            self.jobs.add(thread)
        thread.start()
        return None

    def cancel(self, user_id: int, chat_id: int | None = None) -> None:
        """Immediately abort active transcode and mark batch as cancelled."""
        state = self.store.get(user_id, chat_id)
        state["cancel"] = True
        state["note"] = "Stopping after the current video."
        if hasattr(self.pipeline, "registry"):
            self.pipeline.registry.cancel(
                conversation_key(state.get("chat_id"), user_id)
            )

    def process(
        self,
        user_id: int,
        settings: dict,
        deadline: float | None = None,
        chat_id: int | None = None,
    ) -> None:
        state = self.store.get(user_id, chat_id)
        receipts = set(state.get("receipts", []))
        items = [
            dict(item, job_id=item.get("job_id") or new_job_id())
            for item in state.get("working", [])
            if item.get("message_id") not in receipts
        ]
        state["working"] = items
        state["resume"] = True
        total = len(items)
        state_in = state_out = 0.0
        failed = cancelled = deferred = False

        # Check if pipeline supports decoupled stages for pipelined double-buffering
        has_pipelined_stages = (
            hasattr(self.pipeline, "download_one")
            and hasattr(self.pipeline, "transcode_one")
            and hasattr(self.pipeline, "upload_one")
        )

        if has_pipelined_stages:
            download_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            upload_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            next_download_future: concurrent.futures.Future | None = None
            next_download_item: dict | None = None
            upload_future: concurrent.futures.Future | None = None
            upload_item: dict | None = None

            def do_download(target_item: dict) -> Path:
                declared = max(0, int(target_item.get("size") or 0))
                self._check_disk_space(items, declared)
                p = self.pipeline.download_one(
                    target_item["chat_id"],
                    target_item["message_id"],
                    target_item["media"],
                    report=self.cards.report(state, target_item),
                    job_id=target_item["job_id"],
                )
                target_item["path"] = p
                try:
                    target_item["info"] = self.pipeline.probe(p)
                    target_item["duration"] = target_item["info"]["duration"]
                    self._check_disk_space(items)
                except BotError:
                    safe_unlink(p, self.config.work_dir)
                    target_item["path"] = None
                    raise
                return p

            def finish_download(future, cur_item: dict) -> Path | None:
                nonlocal failed
                if future.cancelled():
                    return None
                try:
                    return future.result()
                except Exception as exc:  # noqa: BLE001
                    failed = True
                    cur_item["status"] = (
                        f"❌ {type(exc).__name__}: "
                        f"{self.config.redact(exc)}"[:120]
                    )
                    self.cards.refresh(state)
                    return None

            def do_upload(
                cur_item: dict, cur_t_res: dict, cur_src: Path
            ) -> tuple[dict, dict]:
                try:
                    res = self.pipeline.upload_one(
                        chat_id=cur_item["chat_id"],
                        reply_to=cur_item["message_id"],
                        media=cur_item["media"],
                        src=cur_src,
                        out=cur_t_res["out"],
                        kind=cur_item["kind"],
                        out_kind=cur_t_res["out_kind"],
                        info=cur_t_res["info"],
                        settings=settings,
                        in_mb=cur_t_res["in_mb"],
                        out_mb=cur_t_res["out_mb"],
                        filename=cur_item["filename"],
                        fallback=cur_t_res["fallback"],
                        report=self.cards.report(state, cur_item),
                        job_id=cur_item["job_id"],
                    )
                    return cur_item, res
                finally:
                    cur_item["path"] = None
                    cur_item.pop("_output_path", None)

            def finish_upload(future, cur_item: dict) -> None:
                nonlocal failed, state_in, state_out
                try:
                    _, res = future.result()
                    if "error" in res:
                        failed = True
                        cur_item["status"] = f"❌ {res['error']}"[:120]
                    else:
                        cur_item["delivered"] = True
                        state_in += res["in"]
                        state_out += res["out"]
                        self._record_receipt(state, cur_item["message_id"])
                except Exception as exc:  # noqa: BLE001
                    failed = True
                    cur_item["status"] = f"❌ {self.config.redact(exc)}"[:120]
                # Delivery is committed before this repaint; a UI/state error
                # must not turn a delivered item into a retryable failure.
                try:
                    self.cards.refresh(state)
                except Exception as exc:  # noqa: BLE001
                    print(f"batch status: {self.config.redact(exc)}", flush=True)

            try:
                for idx, item in enumerate(items):
                    if state.get("cancel"):
                        cancelled = True
                        break

                    duration = float(item.get("duration") or 0)
                    if deadline and time.time() + duration + 120 > deadline:
                        deferred = True
                        item["status"] = "⏳ shift ended"
                        break

                    # 1. Ensure current item (video N) is downloaded
                    src = item.get("path")
                    if next_download_future is not None:
                        if next_download_future.cancelled():
                            next_download_future = None
                            next_download_item = None
                            src = do_download(item)
                        else:
                            src = finish_download(next_download_future, item)
                            next_download_future = None
                            next_download_item = None
                            if src is None:
                                continue
                    elif not src or not Path(str(src)).exists():
                        try:
                            src = do_download(item)
                        except Exception as exc:  # noqa: BLE001
                            failed = True
                            item["status"] = f"❌ {self.config.redact(exc)}"[:120]
                            self.cards.refresh(state)
                            continue

                    if state.get("cancel"):
                        cancelled = True
                        safe_unlink(src, self.config.work_dir)
                        item["path"] = None
                        break

                    # 2. Pipelined double-buffering: prefetch video N+1 in background
                    if idx + 1 < total and not state.get("cancel"):
                        next_item = items[idx + 1]
                        if (
                            not next_item.get("path")
                            or not Path(str(next_item["path"])).exists()
                        ):
                            next_download_future = download_pool.submit(
                                do_download, next_item
                            )
                            next_download_item = next_item

                    # 3. Transcode video N (strict Semaphore(1) ensures 100% CPU)
                    try:
                        actual = item.get("info") or self.pipeline.probe(src)
                        t_res = self.pipeline.transcode_one(
                            src=src,
                            kind=item["kind"],
                            settings=settings,
                            duration=actual["duration"],
                            deadline=deadline,
                            report=self.cards.report(state, item),
                            user_id=conversation_key(chat_id, user_id),
                            probe_info=actual,
                            job_id=item["job_id"],
                        )
                        item["_output_path"] = t_res["out"]
                        self._check_disk_space(items)
                    except Defer:
                        safe_unlink(src, self.config.work_dir)
                        safe_unlink(item.pop("_output_path", None), self.config.work_dir)
                        item["path"] = None
                        deferred = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        safe_unlink(item.pop("_output_path", None), self.config.work_dir)
                        safe_unlink(src, self.config.work_dir)
                        item["path"] = None
                        if state.get("cancel"):
                            cancelled = True
                            break
                        failed = True
                        item["status"] = (
                            f"❌ {type(exc).__name__}: "
                            f"{self.config.redact(exc)}"[:120]
                        )
                        self.cards.refresh(state)
                        continue

                    # 4. Await completion of video N-1's background upload
                    if upload_future is not None:
                        finish_upload(upload_future, upload_item)
                        upload_future = None
                        upload_item = None

                    # 5. Dispatch video N's upload to background
                    upload_future = upload_pool.submit(do_upload, item, t_res, src)
                    upload_item = item
            except Exception as exc:  # noqa: BLE001 - keep the rest queued
                print(
                    f"batch: {type(exc).__name__}: {self.config.redact(exc)}",
                    flush=True,
                )
            finally:
                download_pool.shutdown(wait=True, cancel_futures=True)
                upload_pool.shutdown(wait=True)
                if next_download_future is not None:
                    finish_download(next_download_future, next_download_item)
                    next_download_future = None
                    next_download_item = None
                if upload_future is not None:
                    finish_upload(upload_future, upload_item)
                    upload_future = None
                    upload_item = None
                if cancelled or deferred:
                    for i in items:
                        if not i.get("delivered"):
                            safe_unlink(i.get("path"), self.config.work_dir)
                            i["path"] = None
                            safe_unlink(
                                i.pop("_output_path", None), self.config.work_dir
                            )
        else:
            # Fallback for monolithic pipeline mocks
            try:
                for item in items:
                    if state.get("cancel"):
                        cancelled = True
                        break
                    duration = float(item.get("duration") or 0)
                    if deadline and time.time() + duration + 120 > deadline:
                        deferred = True
                        item["status"] = "⏳ shift ended"
                        break
                    try:
                        with self.transcode_semaphore:
                            result = self.pipeline.compress_one(
                                item["chat_id"],
                                item["message_id"],
                                item["media"],
                                item["kind"],
                                settings,
                                deadline=deadline,
                                path=item.get("path"),
                                filename=item["filename"],
                                report=self.cards.report(state, item),
                                user_id=conversation_key(chat_id, user_id),
                            )
                    except Defer:
                        item["path"] = None
                        deferred = True
                        break
                    item["path"] = None
                    if "error" in result:
                        failed = True
                        item["status"] = f"❌ {result['error']}"[:120]
                    else:
                        item["delivered"] = True
                        state_in += result["in"]
                        state_out += result["out"]
                        self._record_receipt(state, item["message_id"])
                    self.cards.refresh(state)
            except Exception as exc:  # noqa: BLE001 - keep the rest queued
                print(
                    f"batch: {type(exc).__name__}: {self.config.redact(exc)}",
                    flush=True,
                )

        delivered = sum(1 for i in items if i.get("delivered"))
        errors = [i for i in items if (i.get("status") or "").startswith("❌")]
        kept = [i for i in items if not i.get("delivered")]
        with self.store.lock:
            state["working"] = []
            state["queue"] = kept + state["queue"]
            state["processing"] = False
            state["cancel"] = False
            state["resume"] = deferred
            state["view"] = "main"
        pieces = []
        if cancelled:
            pieces.append(f"⏹ Cancelled — {delivered}/{total} delivered.")
        elif deferred:
            pieces.append(f"⏳ Shift ending — {delivered}/{total} delivered.")
        elif failed and not delivered:
            pieces.append(f"❌ Batch failed — {total} error(s).")
        else:
            pieces.append(f"✅ Delivered {delivered}/{total}.")
        if state_in:
            saved = 100 * (1 - state_out / state_in)
            pieces.append(
                f"{state_in:.1f} MB → {state_out:.1f} MB ({saved:.0f}% smaller)."
            )
        if errors:
            pieces.append(f"{len(errors)} failed — tap ▶️ Start to retry.")
        if deferred:
            pieces.append("Remaining videos resume after the next shift starts.")
        state["summary"] = " ".join(pieces)
        try:
            self.store.save()
        except StateError as exc:
            print(f"batch state: {self.config.redact(exc)}", flush=True)
        try:
            self.cards.refresh(state)
        except Exception as exc:  # noqa: BLE001
            print(f"batch summary: {self.config.redact(exc)}", flush=True)
        if hasattr(self.pipeline, "registry"):
            self.pipeline.registry.clear_cancel(
                conversation_key(state.get("chat_id"), user_id)
            )
        with self.jobs_lock:
            self.jobs.discard(threading.current_thread())

    def join_all(self, stop_at: float) -> bool:
        """Wait for workers until stop_at; False if any is still alive."""
        with self.jobs_lock:
            jobs = list(self.jobs)
        for job in jobs:
            job.join(timeout=max(0, stop_at - time.time()))
        return any(job.is_alive() for job in jobs)
