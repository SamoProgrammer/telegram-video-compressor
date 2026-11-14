import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tgcompress import app
from tgcompress.state import StateError, StateStore

from .support import make_config


class InlineWorkerPool:
    def __init__(self):
        self.dispatched = []

    def dispatch(self, target, *args, key=None):
        self.dispatched.append((target, args))
        target(*args)

    def join_all(self, stop_at):
        return False


class FakeUpdateApi:
    def __init__(self):
        self.batches = []
        self.shutdown_updates = []
        self.update_calls = []

    def delete_webhook(self):
        pass

    def get_updates(self, offset, timeout=50):
        self.update_calls.append((offset, timeout))
        if timeout == 0:
            return self.shutdown_updates
        return self.batches.pop(0) if self.batches else []


class AppTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)
        self.api = FakeUpdateApi()
        self.handlers = MagicMock()
        self.batch = MagicMock()
        self.batch.start.return_value = None
        self.batch.join_all.return_value = False
        self.pool = InlineWorkerPool()
        self.created_store = None

        def capture_store(config, store, *args):
            self.created_store = store
            return self.handlers

        patches = (
            patch.object(app, "BotApi", return_value=self.api),
            patch.object(app, "Handlers", side_effect=capture_store),
            patch.object(app, "BatchRunner", return_value=self.batch),
            patch.object(app, "WorkerPool", return_value=self.pool),
            patch.object(app, "MTProtoClient"),
            patch.object(app, "Transfer"),
            patch.object(app, "CardRenderer"),
            patch.object(app, "Pipeline"),
            patch.object(app, "BatchCache"),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        times = iter((0.0, 0.0, 0.0))
        clock = patch.object(app.time, "time", side_effect=lambda: next(times, 2.0))
        clock.start()
        self.addCleanup(clock.stop)

    def config(self):
        return make_config(
            self.tmp,
            run_seconds=1,
            reserve_seconds=0,
            work_dir=self.tmp / "work",
        )

    @staticmethod
    def media_message(message_id=5):
        return {
            "chat": {"id": 1},
            "from": {"id": 3},
            "message_id": message_id,
            "video": {"file_id": "x", "file_size": 1024, "duration": 1},
        }

    def write_state(self, *states):
        payload = {f"{s.get('chat_id')}:{s['user_id']}": s for s in states}
        (self.tmp / "state.json").write_text(json.dumps(payload))

    def test_new_single_is_pending_before_offset_acknowledgement(self):
        msg = self.media_message()
        self.api.batches = [[{"update_id": 9, "message": msg}]]

        app.run(self.config())

        payload = json.loads((self.tmp / "state.json").read_text())
        saved = next(iter(payload.values()))
        self.assertEqual(saved["pending"][0]["message_id"], 5)
        self.assertEqual(self.api.update_calls[-1], (10, 0))

    def test_pending_single_restores_and_duplicate_update_does_not_dispatch_twice(self):
        msg = self.media_message()
        self.write_state(
            {
                "user_id": 3,
                "chat_id": 1,
                "mode": "single",
                "resume": True,
                "pending": [msg],
            }
        )
        self.api.batches = [[{"update_id": 9, "message": msg}]]

        app.run(self.config())

        self.handlers.handle_message.assert_called_once()
        self.assertEqual(self.handlers.handle_message.call_args.args[0]["message_id"], 5)
        self.assertEqual(len(self.pool.dispatched), 1)

    def test_pending_save_failure_leaves_update_unacknowledged(self):
        self.api.batches = [[{"update_id": 9, "message": self.media_message()}]]

        with patch.object(StateStore, "save", side_effect=StateError("read-only")):
            app.run(self.config())

        self.assertEqual(self.api.update_calls, [(None, 50)])

    def test_only_started_batches_resume_on_startup(self):
        queued = {
            "media": {"file_id": "x"},
            "kind": "video",
            "chat_id": 20,
            "message_id": 5,
        }
        self.write_state(
            {
                "user_id": 3,
                "chat_id": 1,
                "mode": "batch",
                "resume": False,
                "queue": [dict(queued, chat_id=1)],
            },
            {
                "user_id": 3,
                "chat_id": 20,
                "mode": "batch",
                "resume": True,
                "queue": [queued],
            },
        )

        app.run(self.config())

        self.batch.start.assert_called_once()
        self.assertEqual(self.batch.start.call_args.args[0]["chat_id"], 20)

    def test_shutdown_saves_after_workers_join(self):
        self.write_state({"user_id": 3, "chat_id": 1})

        def finish_join(stop_at):
            self.created_store.get(3, 1)["note"] = "saved after join"
            return False

        self.batch.join_all.side_effect = finish_join
        app.run(self.config())

        payload = json.loads((self.tmp / "state.json").read_text())
        self.assertEqual(next(iter(payload.values()))["note"], "saved after join")

    def test_shutdown_confirmation_does_not_process_returned_updates(self):
        text_message = {
            "chat": {"id": 1},
            "from": {"id": 3},
            "message_id": 5,
            "text": "hello",
        }
        self.api.batches = [[{"update_id": 9, "message": text_message}]]
        self.api.shutdown_updates = [{"update_id": 100, "message": text_message}]

        app.run(self.config())

        self.assertEqual(self.api.update_calls[-1], (10, 0))
        self.handlers.handle_message.assert_called_once()


    def test_multiple_resumed_singles_fail_without_repeating_or_overtaking_fifo(self):
        messages = [
            self.media_message(1),
            self.media_message(2),
            self.media_message(3),
        ]
        self.write_state({
            "user_id": 3,
            "chat_id": 1,
            "mode": "single",
            "resume": True,
            "pending": messages,
        })
        attempted = []

        def handle(message, _deadline):
            message_id = message["message_id"]
            attempted.append(message_id)
            state = self.created_store.get(3, 1)
            self.created_store.remove_pending(state, message_id)
            if message_id != 2:
                self.created_store.record_receipt(state, message_id)

        self.handlers.handle_message.side_effect = handle
        app.run(self.config())

        self.assertEqual(attempted, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
