"""Application wiring and the long-polling shift loop."""

from __future__ import annotations

import queue
import shutil
import sys
import threading
import time
from collections import deque
from collections.abc import Callable

from .batch import BatchRunner
from .config import Config
from .errors import BotError, Defer
from .handlers import Handlers
from .media import pick_media
from .mtproto import MTProtoClient
from .pipeline import Pipeline
from .queue import BatchCache
from .state import StateError, StateStore, conversation_key
from .telegram_api import BotApi
from .transfer import Transfer
from .ui import CardRenderer


class _Job:
    def __init__(self, target: Callable, args: tuple) -> None:
        self.target = target
        self.args = args
        self.done = threading.Event()

    def is_alive(self) -> bool:
        return not self.done.is_set()


class WorkerPool:
    """Bounded workers with FIFO queues per conversation and fair cross-key turns."""

    def __init__(self, max_workers: int = 4) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.workers: set[threading.Thread] = set()
        self._condition = threading.Condition()
        self._keys: queue.Queue = queue.Queue()
        self._queues: dict[object, deque[_Job]] = {}
        self._pending = 0
        self._closed = False
        for index in range(max_workers):
            thread = threading.Thread(
                target=self._worker, name=f"conversation-{index}", daemon=True
            )
            self.workers.add(thread)
            thread.start()

    def dispatch(self, target: Callable, *args, key=None) -> _Job:
        job = _Job(target, args)
        with self._condition:
            if self._closed:
                raise RuntimeError("Worker pool is closed")
            key = key if key is not None else object()
            self._pending += 1
            if key not in self._queues:
                self._queues[key] = deque()
                self._keys.put(key)
            self._queues[key].append(job)
            self._condition.notify_all()
        return job

    def _worker(self) -> None:
        while True:
            key = self._keys.get()
            if key is None:
                self._keys.task_done()
                return
            with self._condition:
                jobs = self._queues[key]
                job = jobs.popleft()
            try:
                job.target(*job.args)
            except Exception as exc:  # noqa: BLE001, process_single already contains its errors
                print(f"worker: {type(exc).__name__}", flush=True)
            finally:
                with self._condition:
                    if jobs:
                        self._keys.put(key)
                    else:
                        self._queues.pop(key, None)
                    self._pending -= 1
                    self._condition.notify_all()
                job.done.set()
                self._keys.task_done()

    def join_all(self, stop_at: float) -> bool:
        """Return whether work remains at stop_at; close idle workers otherwise."""
        with self._condition:
            while self._pending:
                remaining = stop_at - time.time()
                if remaining <= 0:
                    return True
                self._condition.wait(timeout=remaining)
        self.close()
        return False

    def close(self) -> None:
        with self._condition:
            if self._pending:
                raise RuntimeError("Cannot close workers with unfinished jobs")
            if self._closed:
                return
            self._closed = True
        for _ in self.workers:
            self._keys.put(None)
        for thread in self.workers:
            thread.join()
        self.workers.clear()


def _message_from_item(state: dict, item: dict) -> dict:
    kind = item.get("kind")
    if kind not in ("video", "video_note", "document"):
        kind = "video"
    return {
        "chat": {"id": item.get("chat_id", state.get("chat_id"))},
        "from": {"id": state["user_id"]},
        "message_id": item["message_id"],
        "_job_id": item.get("job_id"),
        kind: item["media"],
    }


def run(config: Config) -> None:
    if not config.token:
        print(
            "BOT_TOKEN is not set; shift skipped. "
            "Add the BOT_TOKEN secret and run the workflow again.",
            flush=True,
        )
        shutil.rmtree(config.work_dir, ignore_errors=True)
        return
    if not config.allowed_user_ids and not config.allowed_usernames:
        print(
            "WARNING: ALLOWED_USER_IDS and ALLOWED_USERNAMES are empty; "
            "anyone can use this bot.",
            flush=True,
        )

    store = StateStore(config.state_file)
    store.load()
    api = BotApi(config.token, config.api_base)
    mtproto = MTProtoClient(config)
    transfer = Transfer(config, api, mtproto)
    cards = CardRenderer(api, store, config)
    pipeline = Pipeline(config, transfer)
    cache = BatchCache(config, transfer)
    batch = BatchRunner(config, store, pipeline, cards)
    handlers = Handlers(config, store, cards, pipeline, batch, cache)
    worker_pool = WorkerPool()

    print(
        f"Bot started. API={config.api_base} shift={config.run_seconds}s "
        f"reserve={config.reserve_seconds}s mtproto={config.mt_ready}",
        flush=True,
    )
    try:
        api.delete_webhook()
    except BotError as exc:
        print(f"deleteWebhook: {config.redact(exc)}", flush=True)

    started = time.time()
    deadline = started + config.run_seconds - config.reserve_seconds
    offset = None

    def process_single(message: dict) -> bool:
        """Each message is dispatched once; the keyed pool enforces FIFO."""
        try:
            handlers.handle_message(message, deadline)
            return True
        except Defer:
            print("Deferred to the next shift.", flush=True)
            return False
        except BotError as exc:
            print(f"handle: {config.redact(exc)}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(
                f"handle: unexpected {type(exc).__name__}: "
                f"{config.redact(exc)}",
                flush=True,
            )
        return True

    def resume_singles(messages: list[dict]) -> None:
        for pending_message in messages:
            if not process_single(pending_message):
                break

    for state in store.states():
        if not state.get("resume"):
            continue
        if state.get("mode") == "batch":
            if state.get("queue"):
                try:
                    error = batch.start(state, deadline, state.get("chat_id"))
                except StateError as exc:
                    print(f"resume batch: {config.redact(exc)}", flush=True)
                    continue
                if error:
                    print(f"resume batch: {error}", flush=True)
            continue
        pending_by_id = {
            int(message["message_id"]): message
            for message in state.get("pending", [])
            if int(message["message_id"]) not in state.get("receipts", [])
        }
        messages = []
        for item in state.get("queue", []):
            message_id = int(item["message_id"])
            if message_id in state.get("receipts", []):
                continue
            messages.append(
                pending_by_id.pop(message_id, _message_from_item(state, item))
            )
        messages.extend(pending_by_id.values())
        if messages:
            worker_pool.dispatch(
                resume_singles,
                messages,
                key=conversation_key(state.get("chat_id"), state["user_id"]),
            )

    try:
        while time.time() < deadline:
            try:
                updates = api.get_updates(offset)
            except BotError as exc:
                message = config.redact(exc)
                print(f"getUpdates: {message}", flush=True)
                if "unauthorized" in message.lower() or "not found" in message.lower():
                    sys.exit("Bad BOT_TOKEN")
                time.sleep(5)
                continue
            for update in updates:
                if time.time() >= deadline:
                    print(
                        "Reserve reached; leaving the rest for the next shift.",
                        flush=True,
                    )
                    return
                if "message" in update:
                    msg = update["message"]
                    user = msg.get("from") or {}
                    user_id = user.get("id")
                    chat_id = (msg.get("chat") or {}).get("id")
                    text = (msg.get("text") or "").strip()

                    # Immediate cancellation if /cancel or cancel
                    if text.lower() in ("/cancel", "cancel"):
                        if hasattr(pipeline, "registry") and user_id:
                            pipeline.registry.cancel(conversation_key(chat_id, user_id))
                        try:
                            handlers.handle_message(msg, deadline)
                        except BotError as exc:
                            print(f"handle: {config.redact(exc)}", flush=True)
                        except Exception as exc:  # noqa: BLE001
                            print(
                                f"handle: unexpected {type(exc).__name__}: "
                                f"{config.redact(exc)}",
                                flush=True,
                            )
                        offset = update["update_id"] + 1
                        continue

                    # Commands and control keywords run immediately on main loop
                    if text.startswith("/") or text.lower() in ("done", "✅ done"):
                        try:
                            handlers.handle_message(msg, deadline)
                        except Defer:
                            print("Deferred to the next shift.", flush=True)
                            return
                        except BotError as exc:
                            print(f"handle: {config.redact(exc)}", flush=True)
                        except Exception as exc:  # noqa: BLE001
                            print(
                                f"handle: unexpected {type(exc).__name__}: "
                                f"{config.redact(exc)}",
                                flush=True,
                            )
                        offset = update["update_id"] + 1
                        continue

                    # Media in single-mode: persist before dispatch and acknowledgement.
                    found = pick_media(msg)
                    state = store.get(user_id, chat_id) if user_id else None
                    if found and (state is None or state.get("mode") != "batch"):
                        if state is None:
                            worker_pool.dispatch(
                                process_single,
                                msg,
                                key=conversation_key(chat_id, user_id),
                            )
                        else:
                            try:
                                is_new = store.add_pending(state, msg)
                            except StateError as exc:
                                print(f"pending: {config.redact(exc)}", flush=True)
                                return
                            if is_new:
                                worker_pool.dispatch(
                                    process_single,
                                    msg,
                                    key=conversation_key(chat_id, user_id),
                                )
                        offset = update["update_id"] + 1
                        continue

                    # Other messages (e.g. batch queueing, unknown text)
                    try:
                        handlers.handle_message(msg, deadline)
                    except Defer:
                        print("Deferred to the next shift.", flush=True)
                        return
                    except BotError as exc:
                        print(f"handle: {config.redact(exc)}", flush=True)
                    except Exception as exc:  # noqa: BLE001
                        print(
                            f"handle: unexpected {type(exc).__name__}: "
                            f"{config.redact(exc)}",
                            flush=True,
                        )
                elif "callback_query" in update:
                    cq = update["callback_query"]
                    if cq.get("data") == "multi:cancel":
                        cq_user = (cq.get("from") or {}).get("id")
                        cq_chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
                        if cq_user and hasattr(pipeline, "registry"):
                            pipeline.registry.cancel(conversation_key(cq_chat, cq_user))
                    try:
                        handlers.handle_callback(cq, deadline)
                    except BotError as exc:
                        print(f"callback: {config.redact(exc)}", flush=True)
                    except Exception as exc:  # noqa: BLE001
                        print(
                            f"callback: unexpected {type(exc).__name__}: "
                            f"{config.redact(exc)}",
                            flush=True,
                        )
                offset = update["update_id"] + 1
    finally:
        try:
            store.save()
        except StateError as exc:
            print(f"shift state: {config.redact(exc)}", flush=True)
        stop_at = started + config.run_seconds
        batch_alive = batch.join_all(stop_at)
        worker_alive = worker_pool.join_all(stop_at)
        try:
            store.save()
        except StateError as exc:
            print(f"shift state: {config.redact(exc)}", flush=True)
        if offset is not None:
            try:
                api.get_updates(offset, timeout=0)
            except BotError as exc:
                print(f"shutdown getUpdates: {config.redact(exc)}", flush=True)
        if batch_alive or worker_alive:
            print("Jobs still running; leaving temp files.", flush=True)
        else:
            transfer.close()
            shutil.rmtree(config.work_dir, ignore_errors=True)
        print("Shift over.", flush=True)


def main() -> None:
    run(Config.from_env())
