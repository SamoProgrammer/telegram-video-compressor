import tempfile
import unittest
from pathlib import Path

from tgcompress.state import StateStore, default_state


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
        self.assertEqual(restored["settings"]["audio"], "aac")
        self.assertEqual(restored["settings"]["tune"], "film")
        self.assertEqual(restored["settings"]["tonemap"], "off")
        self.assertEqual(restored["mode"], "batch")
        item = restored["queue"][0]
        self.assertEqual(item["message_id"], 22)
        self.assertIsNone(item["path"])
        self.assertEqual(restored["chat_id"], 10)

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


if __name__ == "__main__":
    unittest.main()
