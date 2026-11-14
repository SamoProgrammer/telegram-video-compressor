import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tgcompress import media
from tgcompress.state import StateError, StateStore, default_state


class StateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "state.json"
        self.store = StateStore(self.path)

    def test_default_state_has_full_settings_schema(self):
        state = default_state(1, 10)
        s = state["settings"]
        self.assertEqual(s["codec"], "h264")
        self.assertEqual(s["mode"], "crf")
        self.assertEqual(s["crf"], 26)
        self.assertEqual(s["target_size"], "20mb")
        self.assertEqual(s["resolution"], "720")
        self.assertEqual(s["fps"], "orig")
        self.assertEqual(s["audio"], "auto")
        self.assertEqual(s["tune"], "none")
        self.assertEqual(s["tonemap"], "auto")
        self.assertEqual(s["output"], "auto")
        self.assertEqual(media.DEFAULT_SETTINGS, s)

    def test_settings_and_queue_survive_save_load(self):
        state = self.store.get(1, 10)
        state["settings"]["resolution"] = "480"
        state["settings"]["quality"] = "small"
        state["settings"]["codec"] = "hevc"
        state["settings"]["mode"] = "size"
        state["settings"]["target_size"] = "50mb"
        state["settings"]["fps"] = "30"
        state["settings"]["audio"] = "aac"
        state["settings"]["tune"] = "film"
        state["settings"]["tonemap"] = "off"
        state["mode"] = "batch"
        state["queue"].append(
            {
                "media": {"file_id": "f"},
                "kind": "video",
                "chat_id": 10,
                "message_id": 22,
                "size": 1024,
                "name": "clip",
                "filename": "clip.mp4",
                "path": Path("/nowhere"),
                "duration": 4,
                "status": "",
                "delivered": False,
            }
        )
        self.store.save()
        self.store.clear()
        self.store.load()
        restored = self.store.get(1, 10)
        self.assertEqual(restored["settings"]["resolution"], "480")
        self.assertEqual(restored["settings"]["quality"], "small")
        self.assertEqual(restored["settings"]["codec"], "hevc")
        self.assertEqual(restored["settings"]["mode"], "size")
        self.assertEqual(restored["settings"]["target_size"], "50mb")
        self.assertEqual(restored["settings"]["fps"], "30")
        self.assertEqual(restored["settings"]["audio"], "aac_96k")
        self.assertEqual(restored["settings"]["tune"], "film")
        self.assertEqual(restored["settings"]["tonemap"], "off")
        self.assertEqual(restored["mode"], "batch")
        item = restored["queue"][0]
        self.assertEqual(item["message_id"], 22)
        self.assertIsNone(item["path"])
        self.assertEqual(restored["chat_id"], 10)

    def test_legacy_option_ids_are_migrated_on_load(self):
        first = self.store.get(1, 10)
        first["settings"].update(target_size="50pct", audio="opus")
        second = self.store.get(2, 10)
        second["settings"].update(target_size="25pct", audio="aac")
        self.store.save()
        self.store.clear()
        self.store.load()

        self.assertEqual(self.store.get(1)["settings"]["target_size"], "50%")
        self.assertEqual(self.store.get(1)["settings"]["audio"], "opus_64k")
        self.assertEqual(self.store.get(2)["settings"]["target_size"], "25%")
        self.assertEqual(self.store.get(2)["settings"]["audio"], "aac_96k")

    def test_menu_and_cards_survive_save_load(self):
        state = self.store.get(1, 10)
        state["menu"] = (10, 88)
        state["cards"] = [88]
        self.store.save()
        self.store.clear()
        self.store.load()
        restored = self.store.get(1, 10)
        self.assertEqual(restored["menu"], (10, 88))
        self.assertEqual(restored["cards"], [88])

    def test_unfinished_working_items_requeued_on_load(self):
        state = self.store.get(1, 10)
        state["processing"] = True
        state["working"] = [
            {
                "media": {"file_id": "w1"},
                "kind": "video",
                "chat_id": 10,
                "message_id": 101,
                "size": 2048,
                "name": "working_clip",
                "filename": "working_clip.mp4",
                "path": Path("/work/clip.mp4"),
                "duration": 5,
                "status": "compressing 50%",
                "delivered": False,
            }
        ]
        state["queue"] = [
            {
                "media": {"file_id": "q1"},
                "kind": "video",
                "chat_id": 10,
                "message_id": 102,
                "size": 1024,
                "name": "queued_clip",
                "filename": "queued_clip.mp4",
                "path": None,
                "duration": 3,
                "status": "",
                "delivered": False,
            }
        ]
        self.store.save()
        self.store.clear()
        self.store.load()
        restored = self.store.get(1, 10)
        self.assertFalse(restored["processing"])
        self.assertEqual(restored["working"], [])
        self.assertEqual(len(restored["queue"]), 2)
        # Working item was re-queued at the front of queue
        self.assertEqual(restored["queue"][0]["message_id"], 101)
        self.assertIsNone(restored["queue"][0]["path"])
        self.assertEqual(restored["queue"][0]["status"], "")
        self.assertEqual(restored["queue"][1]["message_id"], 102)

    def test_load_with_no_file_is_harmless(self):
        self.store.load()
        self.assertEqual(self.store.users, {})

    def test_private_state_fields_are_not_persisted(self):
        state = self.store.get(1, 10)
        state["processing"] = True
        state["working"] = [{"secret": True}]
        self.store.save()
        self.store.clear()
        self.store.load()
        restored = self.store.get(1, 10)
        self.assertFalse(restored["processing"])
        self.assertEqual(restored["working"], [])
    def test_states_are_isolated_by_chat_and_legacy_get_is_unambiguous(self):
        first = self.store.get(7, 10)
        second = self.store.get(7, 20)
        first["note"] = "first chat"
        second["note"] = "second chat"

        self.assertIsNot(first, second)
        with self.assertRaises(StateError):
            self.store.get(7)

        self.store.save()
        self.store.clear()
        self.store.load()
        self.assertEqual(self.store.get(7, 10)["note"], "first chat")
        self.assertEqual(self.store.get(7, 20)["note"], "second chat")

    def test_legacy_single_user_payload_uses_its_saved_chat_id(self):
        self.path.write_text(json.dumps({"7": {"user_id": 7, "chat_id": 44}}))

        self.store.load()

        self.assertEqual(self.store.get(7)["chat_id"], 44)
        self.assertIs(self.store.get(7), self.store.get(7, 44))

    def test_pending_cleanup_does_not_resume_idle_batch_queue(self):
        state = self.store.get(1, 10)
        state["mode"] = "batch"
        state["resume"] = False
        state["queue"].append(
            {
                "media": {"file_id": "batch"},
                "message_id": 20,
            }
        )
        message = {
            "chat": {"id": 10},
            "from": {"id": 1},
            "message_id": 30,
            "video": {"file_id": "single"},
        }
        state["pending"].append(message)

        self.assertTrue(self.store.remove_pending(state, 30))
        self.assertFalse(state["resume"])

    def test_pending_save_failure_rolls_back_add_and_remove(self):
        state = self.store.get(1, 10)
        message = {
            "chat": {"id": 10},
            "from": {"id": 1},
            "message_id": 40,
            "video": {"file_id": "clip"},
        }
        with patch.object(
            self.store, "save", side_effect=StateError("add: read-only")
        ):
            with self.assertRaises(StateError):
                self.store.add_pending(state, message)
        self.assertEqual(state["pending"], [])
        self.assertFalse(state["resume"])

        state["pending"].append(message)
        with patch.object(
            self.store, "save", side_effect=StateError("remove: read-only")
        ):
            with self.assertRaises(StateError):
                self.store.remove_pending(state, 40)
        self.assertEqual(state["pending"], [message])
        self.assertTrue(state["resume"])

    def test_receipt_save_failure_rolls_back_in_memory_receipt(self):
        state = self.store.get(1, 10)
        with patch.object(
            self.store, "save", side_effect=StateError("receipt: read-only")
        ):
            with self.assertRaises(StateError):
                self.store.record_receipt(state, 41)
        self.assertEqual(state["receipts"], [])

    def test_malformed_cards_do_not_abort_startup(self):
        payload = {
            "10:1": {
                "user_id": 1,
                "chat_id": 10,
                "mode": "single",
                "view": "main",
                "settings": {},
                "summary": "",
                "note": "",
                "queue": [],
                "working": [],
                "menu": None,
                "cards": ["broken"],
                "resume": False,
                "receipts": [],
                "pending": [],
            }
        }
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        self.store.load()
        self.assertEqual(self.store.get(1, 10)["cards"], [])

    def test_probe_metadata_and_job_ids_survive_recovery(self):
        state = self.store.get(1, 10)
        info = {"width": 640, "height": 360, "duration": 2.0, "fps": 30.0}
        state["queue"] = [{
            "media": {"file_id": "clip", "file_size": 1000},
            "kind": "video",
            "chat_id": 10,
            "message_id": 40,
            "size": 1000,
            "name": "clip",
            "filename": "clip.mp4",
            "duration": 2.0,
            "job_id": "queued-job",
            "info": info,
        }]
        pending = {
            "chat": {"id": 10},
            "from": {"id": 1},
            "message_id": 5,
            "_job_id": "pending-job",
            "video": {"file_id": "other", "file_size": 500},
        }
        self.store.add_pending(state, pending)

        restored = StateStore(self.path)
        restored.load()

        item = restored.get(1, 10)["queue"][0]
        self.assertEqual(item["job_id"], "queued-job")
        self.assertEqual(item["info"], info)
        self.assertTrue(restored.get(1, 10)["resume"])
        self.assertEqual(restored.get(1, 10)["pending"][0]["_job_id"], "pending-job")

    def test_save_creates_parents_and_reports_failures_without_secrets(self):
        nested = Path(self.dir.name) / "new" / "parents" / "state.json"
        store = StateStore(nested)
        store.get(1, 10)["note"] = "saved"
        store.save()
        self.assertTrue(nested.exists())

        output = io.StringIO()
        with patch.object(Path, "write_text", side_effect=OSError("secret-token")):
            with redirect_stdout(output), self.assertRaisesRegex(StateError, "OSError"):
                store.save()
        self.assertIn("state save failed", output.getvalue())
        self.assertNotIn("secret-token", output.getvalue())

    def test_only_started_work_is_resumable_after_reload(self):
        idle = self.store.get(1, 10)
        started = self.store.get(1, 20)
        for state, name in ((idle, "idle"), (started, "working")):
            state["queue"] = [
                {
                    "media": {"file_id": name},
                    "kind": "video",
                    "chat_id": state["chat_id"],
                    "message_id": 101 if name == "idle" else 102,
                    "size": 1,
                    "name": name,
                    "filename": f"{name}.mp4",
                    "duration": 1,
                }
            ]
        started["working"] = started["queue"]
        started["queue"] = []

        self.store.save()
        self.store.clear()
        self.store.load()

        self.assertFalse(self.store.get(1, 10)["resume"])
        self.assertTrue(self.store.get(1, 20)["resume"])

    def test_receipts_are_capped_and_pending_round_trip(self):
        state = self.store.get(1, 10)
        for message_id in range(105):
            self.store.record_receipt(state, message_id)
        message = {
            "chat": {"id": 10},
            "from": {"id": 1},
            "message_id": 1000,
            "video": {"file_id": "pending"},
        }
        self.assertTrue(self.store.add_pending(state, message))
        self.assertFalse(self.store.add_pending(state, message))

        self.store.clear()
        self.store.load()
        restored = self.store.get(1, 10)
        self.assertEqual(len(restored["receipts"]), 100)
        self.assertNotIn(0, restored["receipts"])
        self.assertIn(104, restored["receipts"])
        self.assertEqual(restored["pending"][0]["message_id"], 1000)
        self.assertTrue(restored["resume"])


if __name__ == "__main__":
    unittest.main()
