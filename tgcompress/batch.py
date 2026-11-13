"""Batch orchestration: bounded concurrency and pipelined double-buffering."""

from __future__ import annotations

import concurrent.futures
import threading
import time
from pathlib import Path

from .config import Config
from .errors import Defer
from .files import safe_unlink
from .pipeline import TRANSCODE_SEMAPHORE
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

    def start(self, state: dict, deadline: float | None = None) -> str | None:
        if state["processing"]:
            return "A batch is already running."
        if not state["queue"]:
            return "Queue is empty. Send videos first."
        if deadline and time.time() + 120 > deadline:
            return "The shift is ending. Start after the next shift."
        state["working"] = [dict(item, status="queued") for item in state["queue"]]
        state["queue"] = []
        state["processing"] = True
        state["cancel"] = False
        state["view"] = "main"
        state["summary"] = ""
        thread = threading.Thread(
            target=self.process,
            args=(state["user_id"], dict(state["settings"]), deadline),
            daemon=True,
        )
        with self.jobs_lock:
            self.jobs.add(thread)
        thread.start()
        self.store.save()
        return None

    def cancel(self, user_id: int) -> None:
        """Immediately abort active transcode and mark batch as cancelled."""
        state = self.store.get(user_id)
        state["cancel"] = True
        state["note"] = "Stopping after the current video."
        if hasattr(self.pipeline, "registry"):
            self.pipeline.registry.cancel(user_id)
            if state.get("chat_id"):
                self.pipeline.registry.cancel(state["chat_id"])

    def process(
        self, user_id: int, settings: dict, deadline: float | None = None
    ) -> None:
        state = self.store.get(user_id)
        items = state.get("working", [])
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
            upload_future: concurrent.futures.Future | None = None

            def do_download(target_item: dict) -> Path:
                p = self.pipeline.download_one(
                    target_item["chat_id"],
                    target_item["message_id"],
                    target_item["media"],
                    report=self.cards.report(state, target_item),
                )
                target_item["path"] = p
                return p

            def do_upload(
                cur_item: dict, cur_t_res: dict, cur_src: Path
            ) -> tuple[dict, dict]:
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
                )
                return cur_item, res

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
                    if not src or not Path(str(src)).exists():
                        if next_download_future is not None:
                            try:
                                src = next_download_future.result()
                            except Exception as exc:  # noqa: BLE001
                                failed = True
                                item["status"] = f"❌ {self.config.redact(exc)}"[:120]
                                self.cards.refresh(state)
                                next_download_future = None
                                continue
                            next_download_future = None
                        else:
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

                    # 3. Transcode video N (strict Semaphore(1) ensures 100% CPU)
                    try:
                        actual = self.pipeline.probe(src)
                        t_res = self.pipeline.transcode_one(
                            src=src,
                            kind=item["kind"],
                            settings=settings,
                            duration=actual["duration"],
                            deadline=deadline,
                            report=self.cards.report(state, item),
                            user_id=user_id,
                        )
                    except Defer:
                        safe_unlink(src, self.config.work_dir)
                        item["path"] = None
                        deferred = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        safe_unlink(src, self.config.work_dir)
                        item["path"] = None
                        if state.get("cancel"):
                            cancelled = True
                            break
                        failed = True
                        item["status"] = f"❌ {self.config.redact(exc)}"[:120]
                        self.cards.refresh(state)
                        continue

                    # 4. Await completion of video N-1's background upload
                    if upload_future is not None:
                        try:
                            prev_item, prev_res = upload_future.result()
                            if "error" in prev_res:
                                failed = True
                                prev_item["status"] = f"❌ {prev_res['error']}"[:120]
                            else:
                                prev_item["delivered"] = True
                                state_in += prev_res["in"]
                                state_out += prev_res["out"]
                            self.cards.refresh(state)
                        except Exception as exc:  # noqa: BLE001
                            failed = True
                            prev_item["status"] = f"❌ {self.config.redact(exc)}"[:120]
                            self.cards.refresh(state)
                        upload_future = None

                    # 5. Dispatch video N's upload to background
                    upload_future = upload_pool.submit(do_upload, item, t_res, src)

                # Await final upload
                if upload_future is not None:
                    try:
                        prev_item, prev_res = upload_future.result()
                        if "error" in prev_res:
                            failed = True
                            prev_item["status"] = f"❌ {prev_res['error']}"[:120]
                        else:
                            prev_item["delivered"] = True
                            state_in += prev_res["in"]
                            state_out += prev_res["out"]
                        self.cards.refresh(state)
                    except Exception as exc:  # noqa: BLE001
                        failed = True
                        prev_item["status"] = f"❌ {self.config.redact(exc)}"[:120]
                        self.cards.refresh(state)
            except Exception as exc:  # noqa: BLE001 - keep the rest queued
                print(f"batch: {self.config.redact(exc)}", flush=True)
            finally:
                download_pool.shutdown(wait=False, cancel_futures=True)
                upload_pool.shutdown(wait=True)
                if cancelled or deferred:
                    for i in items:
                        if not i.get("delivered") and i.get("path"):
                            safe_unlink(i["path"], self.config.work_dir)
                            i["path"] = None
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
                                user_id=user_id,
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
                    self.cards.refresh(state)
            except Exception as exc:  # noqa: BLE001 - keep the rest queued
                print(f"batch: {self.config.redact(exc)}", flush=True)

        delivered = sum(1 for i in items if i.get("delivered"))
        errors = [i for i in items if (i.get("status") or "").startswith("❌")]
        kept = [i for i in items if not i.get("delivered")]
        with self.store.lock:
            state["working"] = []
            state["queue"] = kept + state["queue"]
            state["processing"] = False
            state["cancel"] = False
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
            pieces.append("Send remaining videos again after the next shift.")
        state["summary"] = " ".join(pieces)
        try:
            self.cards.refresh(state)
        except Exception as exc:  # noqa: BLE001
            print(f"batch summary: {self.config.redact(exc)}", flush=True)
        with self.jobs_lock:
            self.jobs.discard(threading.current_thread())

    def join_all(self, stop_at: float) -> bool:
        """Wait for workers until stop_at; False if any is still alive."""
        with self.jobs_lock:
            jobs = list(self.jobs)
        for job in jobs:
            job.join(timeout=max(0, stop_at - time.time()))
        return any(job.is_alive() for job in jobs)
