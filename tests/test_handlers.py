import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tgcompress import media
from tgcompress.state import StateError

from .support import make_stack


def message(chat_id=1, message_id=2, user_id=3, username=None, **extra):
    user = {"id": user_id}
    if username:
        user["username"] = username
    data = {
        "chat": {"id": chat_id},
        "message_id": message_id,
        "from": user,
    }
    data.update(extra)
    return data


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)

    def stack(self, **overrides):
        return make_stack(self.tmp, **overrides)

    def test_single_lifecycle_uses_one_card(self):
        stack = self.stack()

        def fake_compress_one(*args, **kwargs):
            stack.api.call("sendVideo", chat_id=1, video="compressed")
            return {
                "in": 10.0,
                "out": 2.0,
                "saved": True,
                "kind": "video",
                "fallback": False,
            }

        stack.pipeline.compress_one = fake_compress_one
        stack.handlers.handle_message(message(text="/start"))
        sends = [call for call in stack.api.calls if call[0] == "sendMessage"]
        # /start sends persistent reply keyboard and the single control card
        self.assertEqual(len(sends), 2)
        self.assertIn("Send one video", sends[1][1]["text"])

        stack.api.calls.clear()
        stack.handlers.handle_message(
            message(
                message_id=5,
                video={"file_id": "x", "file_size": 1024, "duration": 1},
            )
        )
        methods = [call[0] for call in stack.api.calls]
        self.assertIn("sendVideo", methods)
        self.assertNotIn("sendDocument", methods)
        self.assertTrue(any(call[0] == "editMessageText" for call in stack.api.calls))
        state = stack.store.get(3, 1)
        self.assertFalse(state["processing"])
        self.assertEqual(state["working"], [])

    def test_second_start_deletes_old_card(self):
        stack = self.stack()
        stack.handlers.handle_message(message(text="/start"))
        first_card = stack.store.get(3, 1)["menu"][1]
        stack.handlers.handle_message(message(message_id=9, text="/start"))
        new_card = stack.store.get(3, 1)["menu"][1]
        self.assertGreater(new_card, first_card)
        deletes = [call for call in stack.api.calls if call[0] == "deleteMessage"]
        self.assertTrue(any(call[1]["message_id"] == first_card for call in deletes))

    def test_settings_navigation_edits_card_in_place_and_persists(self):
        stack = self.stack()
        stack.handlers.handle_message(message(text="/start"))
        first_card = stack.store.get(3, 1)["menu"][1]

        # Callback into settings: must NOT send a new message, edits in-place
        stack.handlers.handle_callback(
            {
                "id": "c1",
                "from": {"id": 3},
                "message": {"message_id": first_card, "chat": {"id": 1}},
                "data": "menu:settings",
            }
        )
        state = stack.store.get(3, 1)
        self.assertEqual(state["menu"][1], first_card)
        edits = [call for call in stack.api.calls if call[0] == "editMessageText"]
        self.assertTrue(any("Settings" in call[1]["text"] for call in edits))

        # Callback into resolution submenu
        stack.handlers.handle_callback(
            {
                "id": "c2",
                "from": {"id": 3},
                "message": {"message_id": first_card, "chat": {"id": 1}},
                "data": "setmenu:res",
            }
        )
        self.assertEqual(state["menu"][1], first_card)
        self.assertEqual(state["view"], "res")

        # Pick 1080p: updates setting in-place and answers toast
        stack.handlers.handle_callback(
            {
                "id": "c3",
                "from": {"id": 3},
                "message": {"message_id": first_card, "chat": {"id": 1}},
                "data": "set:res:1080",
            }
        )
        self.assertEqual(stack.store.get(3, 1)["settings"]["resolution"], "1080")
        self.assertEqual(state["menu"][1], first_card)
        self.assertEqual(state["view"], "res")  # Stays on screen to show checkmark
        answers = [
            p
            for m, p in stack.api.calls
            if m == "answerCallbackQuery" and p.get("callback_query_id") == "c3"
        ]
        self.assertEqual(answers[0]["text"], "Option updated")
        self.assertIn('"1080"', stack.config.state_file.read_text())

    def test_persistent_reply_keyboard_navigation(self):
        stack = self.stack()
        stack.handlers.handle_message(message(text="/start"))
        state = stack.store.get(3, 1)
        self.assertEqual(state["mode"], "single")

        # User taps [ 📚 Batch Mode ]
        stack.handlers.handle_message(message(message_id=10, text="📚 Batch Mode"))
        self.assertEqual(state["mode"], "batch")
        self.assertEqual(state["view"], "main")

        # User taps [ 🎬 Single Mode ]
        stack.handlers.handle_message(message(message_id=11, text="🎬 Single Mode"))
        self.assertEqual(state["mode"], "single")

        # User taps [ ⚙️ Settings ]
        stack.handlers.handle_message(message(message_id=12, text="⚙️ Settings"))
        self.assertEqual(state["view"], "settings")

        # User taps [ ℹ️ Status / ID ]
        stack.handlers.handle_message(message(message_id=13, text="ℹ️ Status / ID"))
        sends = [call for call in stack.api.calls if call[0] == "sendMessage"]
        self.assertIn("user_id: 3", sends[-1][1]["text"])

    def test_main_menu_preserves_batch_mode(self):
        stack = self.stack()
        stack.handlers.handle_message(message(text="/start"))
        state = stack.store.get(3, 1)
        state["mode"] = "batch"

        # Navigate to settings
        stack.handlers.handle_callback(
            {
                "id": "c1",
                "from": {"id": 3},
                "message": {"message_id": state["menu"][1], "chat": {"id": 1}},
                "data": "menu:settings",
            }
        )
        self.assertEqual(state["view"], "settings")

        # Return to main menu via menu:main
        stack.handlers.handle_callback(
            {
                "id": "c2",
                "from": {"id": 3},
                "message": {"message_id": state["menu"][1], "chat": {"id": 1}},
                "data": "menu:main",
            }
        )
        self.assertEqual(state["view"], "main")
        self.assertEqual(state["mode"], "batch")  # Preserved!

    def test_clear_confirmation_flow(self):
        stack = self.stack()
        stack.handlers.handle_message(message(text="/start"))
        state = stack.store.get(3, 1)
        state["mode"] = "batch"
        state["queue"] = [
            {"name": "clip", "size": 1024, "message_id": 99, "path": None}
        ]

        # Tap 🗑 Clear
        stack.handlers.handle_callback(
            {
                "id": "c1",
                "from": {"id": 3},
                "message": {"message_id": state["menu"][1], "chat": {"id": 1}},
                "data": "multi:clear",
            }
        )
        self.assertEqual(state["view"], "clear_confirm")
        self.assertEqual(len(state["queue"]), 1)  # Not cleared yet!

        # Tap Cancel
        stack.handlers.handle_callback(
            {
                "id": "c2",
                "from": {"id": 3},
                "message": {"message_id": state["menu"][1], "chat": {"id": 1}},
                "data": "menu:main",
            }
        )
        self.assertEqual(state["view"], "main")
        self.assertEqual(len(state["queue"]), 1)

        # Tap 🗑 Clear again
        stack.handlers.handle_callback(
            {
                "id": "c3",
                "from": {"id": 3},
                "message": {"message_id": state["menu"][1], "chat": {"id": 1}},
                "data": "multi:clear",
            }
        )
        self.assertEqual(state["view"], "clear_confirm")

        # Tap ⚠️ Confirm Clear
        stack.handlers.handle_callback(
            {
                "id": "c4",
                "from": {"id": 3},
                "message": {"message_id": state["menu"][1], "chat": {"id": 1}},
                "data": "multi:clear_confirm",
            }
        )
        self.assertEqual(state["view"], "main")
        self.assertEqual(len(state["queue"]), 0)

    def test_all_settings_submenus_and_updates(self):
        stack = self.stack()
        stack.handlers.handle_message(message(text="/start"))
        card_id = stack.store.get(3, 1)["menu"][1]

        options_to_test = [
            ("setmenu:codec", "codec", "set:codec:hevc", "codec", "hevc"),
            ("setmenu:mode", "mode", "set:mode:size", "mode", "size"),
            ("setmenu:quality", "quality", "set:size:50mb", "target_size", "50mb"),
            ("setmenu:quality", "quality", "set:size:50pct", "target_size", "50%"),
            ("setmenu:quality", "quality", "set:size:25%", "target_size", "25%"),
            ("setmenu:quality", "quality", "set:crf:22", "crf", 22),
            ("setmenu:fps", "fps", "set:fps:30", "fps", "30"),
            ("setmenu:audio", "audio", "set:audio:opus", "audio", "opus_64k"),
            ("setmenu:audio", "audio", "set:audio:aac", "audio", "aac_96k"),
            (
                "setmenu:audio",
                "audio",
                "set:audio:opus_64k",
                "audio",
                "opus_64k",
            ),
            ("setmenu:output", "output", "set:out:document", "output", "document"),
            ("setmenu:tune", "tune", "set:tune:animation", "tune", "animation"),
            ("setmenu:tune", "tune", "set:tonemap:off", "tonemap", "off"),
        ]

        for (
            menu_data,
            expected_view,
            opt_data,
            setting_key,
            expected_val,
        ) in options_to_test:
            stack.handlers.handle_callback(
                {
                    "id": f"m_{menu_data}",
                    "from": {"id": 3},
                    "message": {"message_id": card_id, "chat": {"id": 1}},
                    "data": menu_data,
                }
            )
            state = stack.store.get(3, 1)
            self.assertEqual(state["view"], expected_view)

            stack.handlers.handle_callback(
                {
                    "id": f"o_{opt_data}",
                    "from": {"id": 3},
                    "message": {"message_id": card_id, "chat": {"id": 1}},
                    "data": opt_data,
                }
            )
            self.assertEqual(state["settings"][setting_key], expected_val)

    def test_option_keyboards_use_canonical_ids_and_limit_tune_claims(self):
        stack = self.stack()
        stack.handlers.handle_message(message(text="/start"))
        state = stack.store.get(3, 1)

        state["view"] = "quality"
        state["settings"]["mode"] = "size"
        stack.cards.refresh(state)
        keyboard = stack.api.calls[-1][1]["reply_markup"]
        self.assertIn("set:size:50%", keyboard)
        self.assertIn("set:size:25%", keyboard)
        self.assertNotIn("set:size:50pct", keyboard)
        self.assertNotIn("set:size:25pct", keyboard)

        state["view"] = "audio"
        stack.cards.refresh(state)
        keyboard = stack.api.calls[-1][1]["reply_markup"]
        self.assertIn("set:audio:aac_96k", keyboard)
        self.assertIn("set:audio:opus_64k", keyboard)
        self.assertNotIn("set:audio:aac\"", keyboard)
        self.assertNotIn("set:audio:opus\"", keyboard)

        state["view"] = "tune"
        state["settings"]["codec"] = "av1"
        stack.cards.refresh(state)
        self.assertIn("H.264/H.265", stack.api.calls[-1][1]["text"])

    def test_cancel_kills_only_users_process(self):
        stack = self.stack()
        own_proc = MagicMock()
        other_proc = MagicMock()
        stack.pipeline.registry.register((1, 3), own_proc)
        media.register_process("other-user", other_proc)
        self.addCleanup(media.unregister_process, "other-user")

        state = stack.store.get(3, 1)
        state["processing"] = True
        state["menu"] = (1, 5)

        stack.handlers.handle_callback(
            {
                "id": "can",
                "from": {"id": 3},
                "message": {"message_id": 5, "chat": {"id": 1}},
                "data": "multi:cancel",
            }
        )

        self.assertTrue(state["cancel"])
        own_proc.kill.assert_called_once()
        other_proc.kill.assert_not_called()
        self.assertIn("Cancelled", state["note"])

    def test_cancel_is_scoped_to_conversation(self):
        stack = self.stack()
        own_proc = MagicMock()
        same_user_other_chat_proc = MagicMock()
        stack.pipeline.registry.register((1, 3), own_proc)
        stack.pipeline.registry.register((2, 3), same_user_other_chat_proc)

        state = stack.store.get(3, 1)
        state["processing"] = True
        state["menu"] = (1, 5)
        stack.handlers.handle_callback(
            {
                "id": "can",
                "from": {"id": 3},
                "message": {"message_id": 5, "chat": {"id": 1}},
                "data": "multi:cancel",
            }
        )

        own_proc.kill.assert_called_once()
        same_user_other_chat_proc.kill.assert_not_called()

    def test_single_success_survives_receipt_save_failure(self):
        stack = self.stack()
        stack.pipeline.compress_one = lambda *args, **kwargs: {
            "in": 1.0,
            "out": 0.5,
            "saved": True,
            "kind": "video",
            "fallback": False,
        }
        pending_message = {
            "chat": {"id": 1},
            "from": {"id": 3},
            "message_id": 5,
            "video": {"file_id": "x", "file_size": 1024, "duration": 1},
        }
        state = stack.store.get(3, 1)
        state["pending"].append(pending_message)

        with patch.object(
            stack.store,
            "record_receipt",
            side_effect=StateError("receipt: read-only"),
        ):
            stack.handlers.handle_message(message(message_id=5, video={
                "file_id": "x", "file_size": 1024, "duration": 1
            }))

        self.assertIn("receipt: read-only", state["note"])
        self.assertEqual(state["pending"], [pending_message])

    def test_done_with_empty_queue_alerts(self):
        stack = self.stack()
        stack.handlers.handle_message(message(text="/start"))
        stack.handlers.handle_callback(
            {
                "id": "c",
                "from": {"id": 3},
                "message": {"message_id": 1, "chat": {"id": 1}},
                "data": "multi:done",
            }
        )
        answers = [p for m, p in stack.api.calls if m == "answerCallbackQuery"]
        self.assertTrue(answers[0]["show_alert"])
        self.assertIn("empty", answers[0]["text"].lower())

    def test_stale_callback_alerts(self):
        stack = self.stack()
        stack.handlers.handle_callback(
            {
                "id": "c",
                "from": {"id": 3},
                "message": {"message_id": 1, "chat": {"id": 1}},
                "data": "menu:gone",
            }
        )
        answers = [p for m, p in stack.api.calls if m == "answerCallbackQuery"]
        self.assertTrue(answers[0]["show_alert"])

    def test_deny_message_shows_identity(self):
        stack = self.stack(allowed_user_ids=frozenset({1}))
        stack.handlers.handle_message(
            message(
                chat_id=2,
                message_id=3,
                user_id=9,
                username="Nope",
                text="hi",
            )
        )
        reply = stack.api.calls[0][1]["text"]
        self.assertIn("Your user ID: 9 (@Nope)", reply)

    def test_username_only_allow_list_denies_unknown_user(self):
        stack = self.stack(allowed_usernames=frozenset({"owner"}))
        stack.handlers.handle_message(
            message(user_id=9, username="stranger", text="/start")
        )
        self.assertIn("This bot is private", stack.api.calls[0][1]["text"])

    def test_username_only_allow_list_admits_known_user(self):
        stack = self.stack(allowed_usernames=frozenset({"owner"}))
        stack.handlers.handle_message(
            message(user_id=9, username="@Owner", text="/start")
        )
        texts = [
            call[1]["text"] for call in stack.api.calls if call[0] == "sendMessage"
        ]
        self.assertTrue(any("Send one video" in t for t in texts))

    def test_big_video_accepted_when_mtproto_ready(self):
        stack = self.stack(
            telegram_api_id=1,
            telegram_api_hash="hash",
        )
        stack.pipeline.compress_one = lambda *args, **kwargs: {
            "in": 1.0,
            "out": 0.5,
            "saved": True,
            "kind": "video",
            "fallback": False,
        }
        stack.handlers.handle_message(
            message(
                user_id=9,
                video={
                    "file_id": "big",
                    "file_size": 36 * 1024 * 1024,
                    "duration": 60,
                },
            )
        )
        texts = [p.get("text", "") for m, p in stack.api.calls if m == "sendMessage"]
        self.assertFalse(any("cloud Bot API" in text for text in texts))

    def test_video_over_mtproto_cap_is_rejected(self):
        stack = self.stack(telegram_api_id=1, telegram_api_hash="hash")
        stack.handlers.handle_message(
            message(
                user_id=9,
                video={"file_id": "huge", "file_size": 3 * 1024**3, "duration": 60},
            )
        )
        texts = [p.get("text", "") for m, p in stack.api.calls if m == "sendMessage"]
        self.assertTrue(texts and "2000 MB" in texts[0])

    def test_oversize_without_mtproto_explains_the_secret(self):
        stack = self.stack()
        stack.handlers.handle_message(
            message(
                user_id=9,
                video={
                    "file_id": "big",
                    "file_size": 36 * 1024 * 1024,
                    "duration": 60,
                },
            )
        )
        texts = [p.get("text", "") for m, p in stack.api.calls if m == "sendMessage"]
        self.assertIn("TELEGRAM_API_ID", texts[0])
    def test_single_receipt_suppresses_duplicate_and_clears_pending(self):
        stack = self.stack()
        calls = []

        def compress(*args, **kwargs):
            calls.append(args)
            return {
                "in": 10.0,
                "out": 2.0,
                "saved": True,
                "kind": "video",
                "fallback": False,
            }

        stack.pipeline.compress_one = compress
        msg = message(
            message_id=5,
            video={"file_id": "x", "file_size": 1024, "duration": 1},
        )
        state = stack.store.get(3, 1)
        stack.store.add_pending(state, msg)

        stack.handlers.handle_message(msg)
        stack.handlers.handle_message(msg)

        self.assertEqual(len(calls), 1)
        self.assertEqual(state["receipts"], [5])
        self.assertEqual(state["pending"], [])
        self.assertFalse(state["resume"])

    def test_single_defer_leaves_durable_resumable_work_and_pending(self):
        from tgcompress.errors import Defer

        stack = self.stack()
        stack.pipeline.compress_one = MagicMock(side_effect=Defer())
        msg = message(
            message_id=5,
            video={"file_id": "x", "file_size": 1024, "duration": 1},
        )
        state = stack.store.get(3, 1)
        stack.store.add_pending(state, msg)
        state["queue"] = [
            {
                "media": {"file_id": "other"},
                "kind": "video",
                "chat_id": 1,
                "message_id": 6,
                "size": 1,
                "name": "other",
                "filename": "other.mp4",
                "duration": 1,
            }
        ]

        with self.assertRaises(Defer):
            stack.handlers.handle_message(msg)

        stack.store.clear()
        stack.store.load()
        restored = stack.store.get(3, 1)
        self.assertTrue(restored["resume"])
        self.assertEqual(restored["working"], [])
        self.assertEqual([i["message_id"] for i in restored["queue"]], [5, 6])
        self.assertEqual([m["message_id"] for m in restored["pending"]], [5])

    def test_permanent_single_failure_clears_resume_and_pending(self):
        stack = self.stack()
        stack.pipeline.compress_one = MagicMock(return_value={"error": "bad input"})
        msg = message(
            message_id=5,
            video={"file_id": "x", "file_size": 1024, "duration": 1},
        )
        state = stack.store.get(3, 1)
        stack.store.add_pending(state, msg)

        stack.handlers.handle_message(msg)

        self.assertFalse(state["resume"])
        self.assertEqual(state["pending"], [])
        self.assertEqual(state["receipts"], [])

    def test_duplicate_undelivered_batch_media_is_not_queued_twice(self):
        stack = self.stack()
        stack.cache.cache_item = lambda *args, **kwargs: None
        state = stack.store.get(3, 1)
        state["mode"] = "batch"
        msg = message(
            message_id=5,
            video={"file_id": "x", "file_size": 1024, "duration": 1},
        )

        stack.handlers.handle_message(msg)
        stack.handlers.handle_message(msg)

        self.assertEqual(len(state["queue"]), 1)
        self.assertEqual(state["queue"][0]["message_id"], 5)


if __name__ == "__main__":
    unittest.main()
