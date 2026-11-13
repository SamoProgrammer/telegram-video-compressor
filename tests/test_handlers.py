import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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
        state = stack.store.users[3]
        self.assertFalse(state["processing"])
        self.assertEqual(state["working"], [])

    def test_second_start_deletes_old_card(self):
        stack = self.stack()
        stack.handlers.handle_message(message(text="/start"))
        first_card = stack.store.users[3]["menu"][1]
        stack.handlers.handle_message(message(message_id=9, text="/start"))
        new_card = stack.store.users[3]["menu"][1]
        self.assertGreater(new_card, first_card)
        deletes = [call for call in stack.api.calls if call[0] == "deleteMessage"]
        self.assertTrue(any(call[1]["message_id"] == first_card for call in deletes))

    def test_settings_navigation_edits_card_in_place_and_persists(self):
        stack = self.stack()
        stack.handlers.handle_message(message(text="/start"))
        first_card = stack.store.users[3]["menu"][1]

        # Callback into settings: must NOT send a new message, edits in-place
        stack.handlers.handle_callback(
            {
                "id": "c1",
                "from": {"id": 3},
                "message": {"message_id": first_card, "chat": {"id": 1}},
                "data": "menu:settings",
            }
        )
        state = stack.store.users[3]
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
        self.assertEqual(stack.store.users[3]["settings"]["resolution"], "1080")
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
        state = stack.store.users[3]
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
        state = stack.store.users[3]
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
        state = stack.store.users[3]
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
        card_id = stack.store.users[3]["menu"][1]

        options_to_test = [
            ("setmenu:codec", "codec", "set:codec:hevc", "codec", "hevc"),
            ("setmenu:mode", "mode", "set:mode:size", "mode", "size"),
            ("setmenu:quality", "quality", "set:size:50mb", "target_size", "50mb"),
            ("setmenu:quality", "quality", "set:crf:22", "crf", 22),
            ("setmenu:fps", "fps", "set:fps:30", "fps", "30"),
            ("setmenu:audio", "audio", "set:audio:opus", "audio", "opus"),
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
            state = stack.store.users[3]
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

    def test_process_registry_kill_on_cancel(self):
        stack = self.stack()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        stack.handlers.process_registry.register(3, mock_proc)
        self.assertIs(stack.handlers.process_registry.get(3), mock_proc)

        # Trigger cancel
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
        mock_proc.terminate.assert_called_once()
        self.assertIn("Cancelled", state["note"])

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


if __name__ == "__main__":
    unittest.main()
