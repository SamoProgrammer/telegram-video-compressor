import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tgcompress.ui import card_kb, card_text, persistent_reply_keyboard

from .support import make_stack


class CardTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.stack = make_stack(Path(self.dir.name))

    def test_keyboards_are_valid_for_every_view(self):
        state = self.stack.store.get(70, 1)
        views = (
            "main",
            "settings",
            "codec",
            "mode",
            "quality",
            "res",
            "fps",
            "audio",
            "output",
            "tune",
            "clear_confirm",
            "help",
        )
        for view in views:
            state["view"] = view
            for mode in ("crf", "size"):
                state["settings"]["mode"] = mode
                rows = card_kb(state)
                self.assertTrue(rows, f"Empty keyboard for view {view}")
                for row in rows:
                    for button in row:
                        self.assertLessEqual(len(button["callback_data"].encode()), 64)
                        if button.get("style"):
                            self.assertIn(
                                button["style"], {"primary", "success", "danger"}
                            )

    def test_submenus_use_2col_and_have_breadcrumbs(self):
        state = self.stack.store.get(70, 1)
        submenus = (
            "codec",
            "mode",
            "quality",
            "res",
            "fps",
            "audio",
            "output",
            "tune",
        )
        for view in submenus:
            state["view"] = view
            rows = card_kb(state)
            # Max 2 buttons per row
            for row in rows:
                self.assertLessEqual(len(row), 2, f"Row has >2 buttons in view {view}")
            # Navigation row at bottom contains Back to Settings and Main Menu
            nav_row = rows[-1]
            callbacks = [b["callback_data"] for b in nav_row]
            self.assertIn("menu:settings", callbacks)
            self.assertIn("menu:main", callbacks)

            body = card_text(state, self.stack.config)
            self.assertIn("⚙️ Settings ›", body, f"Breadcrumb missing in view {view}")

    def test_processing_card_only_offers_cancel(self):
        state = self.stack.store.get(70, 1)
        state["processing"] = True
        keyboard = card_kb(state)
        self.assertEqual(keyboard[0][0]["callback_data"], "multi:cancel")
        self.assertIn("Working", card_text(state, self.stack.config))

    def test_batch_queue_is_rendered(self):
        state = self.stack.store.get(70, 1)
        state["mode"] = "batch"
        state["queue"] = [{"name": "clip", "size": 2 * 1048576, "status": ""}]
        body = card_text(state, self.stack.config)
        self.assertIn("Batch (1)", str(card_kb(state)))
        self.assertIn("clip — 2.0 MB · queued", body)

    def test_clear_confirm_card(self):
        state = self.stack.store.get(70, 1)
        state["view"] = "clear_confirm"
        state["queue"] = [{"name": "clip", "size": 1024, "status": ""}]
        body = card_text(state, self.stack.config)
        self.assertIn("Clear Batch Queue", body)
        keyboard = card_kb(state)
        callbacks = [b["callback_data"] for row in keyboard for b in row]
        self.assertIn("multi:clear_confirm", callbacks)
        self.assertIn("menu:main", callbacks)

    def test_large_batches_are_bounded_in_both_views(self):
        state = self.stack.store.get(70, 1)
        items = [
            {
                "name": f"clip-{index:03d}.mp4",
                "size": 2 * 1048576,
                "status": "⚙️ compressing " + "x" * 100,
            }
            for index in range(100)
        ]
        for processing in (False, True):
            with self.subTest(processing=processing):
                state["processing"] = processing
                state["working"] = items if processing else []
                state["queue"] = [] if processing else items
                state["mode"] = "batch"
                body = card_text(state, self.stack.config)
                self.assertLessEqual(len(body), 4000)
                self.assertIn("clip-000.mp4", body)
                self.assertIn("more", body)

    def test_concurrent_card_refreshes_are_serialized(self):
        state = self.stack.store.get(70, 1)
        original_call = self.stack.api.call
        counter_lock = threading.Lock()
        active = 0
        overlapped = False

        def tracked_call(method, files=None, **params):
            nonlocal active, overlapped
            with counter_lock:
                active += 1
                overlapped = overlapped or active > 1
            time.sleep(0.005)
            try:
                return original_call(method, files=files, **params)
            finally:
                with counter_lock:
                    active -= 1

        self.stack.api.call = tracked_call
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.stack.cards.refresh, state)
            second = pool.submit(self.stack.cards.refresh, state)
            first.result()
            second.result()

        self.assertFalse(overlapped)

    def test_persistent_reply_keyboard(self):
        kb_json = persistent_reply_keyboard()
        data = json.loads(kb_json)
        self.assertTrue(data.get("resize_keyboard"))
        self.assertTrue(data.get("is_persistent"))
        rows = data.get("keyboard", [])
        self.assertEqual(len(rows), 2)
        flat = [btn["text"] for row in rows for btn in row]
        self.assertIn("🎬 Single Mode", flat)
        self.assertIn("📚 Batch Mode", flat)
        self.assertIn("⚙️ Settings", flat)
        self.assertIn("ℹ️ Status / ID", flat)


if __name__ == "__main__":
    unittest.main()
