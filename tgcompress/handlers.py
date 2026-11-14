"""Telegram update handlers: messages, commands, and callback buttons."""

from __future__ import annotations

import time

from .config import Config
from .errors import BotError, Defer
from .files import safe_unlink
from .media import (
    OUTPUT_LABELS,
    QUALITIES,
    RESOLUTION_LABELS,
    filename_for,
    media_name,
    pick_media,
)
from .settings import canonical_audio, canonical_target_size
from .state import StateError, conversation_key
from .telegram_api import reply_parameters
from .ui import (
    AUDIO_OPTIONS,
    CODECS,
    FPS_OPTIONS,
    MODES,
    OUTPUT_OPTIONS,
    RES_OPTIONS,
    TARGET_SIZES,
    TUNE_OPTIONS,
    persistent_reply_keyboard,
    short,
)


def whoami(user_id: int | None, username: str | None) -> str:
    return f"{user_id}" + (f" (@{username})" if username else "")


class Handlers:
    def __init__(
        self,
        config: Config,
        store,
        cards,
        pipeline,
        batch,
        cache,
    ) -> None:
        self.config = config
        self.store = store
        self.cards = cards
        self.pipeline = pipeline
        self.batch = batch
        self.cache = cache

    def _deny(self, chat_id: int, message_id: int, user_id, username) -> None:
        self.cards.api.call(
            "sendMessage",
            chat_id=chat_id,
            text=(
                "This bot is private.\n"
                f"Your user ID: {whoami(user_id, username)}\n"
                "Ask the owner to add it to ALLOWED_USER_IDS or ALLOWED_USERNAMES."
            ),
            reply_parameters=reply_parameters(message_id),
        )

    def handle_message(self, message: dict, deadline: float | None = None) -> None:
        chat_id = message["chat"]["id"]
        message_id = message["message_id"]
        user = message.get("from") or {}
        user_id = user.get("id")
        username = user.get("username") or ""

        if not self.config.is_allowed(user_id, username):
            self._deny(chat_id, message_id, user_id, username)
            return

        state = self.store.get(user_id, chat_id)
        state["menu"] = None  # reposition: the card is always the latest message
        text = (message.get("text") or "").strip()

        if text.startswith("/start"):
            state["mode"] = "single"
            state["view"] = "main"
            state["note"] = ""
            state["menu"] = None  # fresh card
            self.cards.api.call(
                "sendMessage",
                chat_id=chat_id,
                text="🎬 Video Compressor ready. Use the navigation below or send a video.",
                reply_markup=persistent_reply_keyboard(),
            )
            self.cards.refresh(state)
            return
        if text in ("🎬 Single Mode", "🎬 Single", "/single"):
            state["mode"] = "single"
            state["view"] = "main"
            state["note"] = ""
            self.cards.refresh(state)
            return
        if text in ("📚 Batch Mode", "📚 Batch", "📚 Multiple", "/batch"):
            state["mode"] = "batch"
            state["view"] = "main"
            state["note"] = ""
            self.cards.refresh(state)
            return
        if text.startswith("/help"):
            state["view"] = "help"
            self.cards.refresh(state)
            return
        if text in ("⚙️ Settings", "/settings"):
            state["view"] = "settings"
            self.cards.refresh(state)
            return
        if text.startswith("/id") or text in ("ℹ️ Status / ID", "/status", "status"):
            queue_len = len(state.get("queue", []))
            self.cards.api.call(
                "sendMessage",
                chat_id=chat_id,
                text=(
                    f"user_id: {user_id}\n"
                    f"chat_id: {chat_id}\n"
                    f"mode: {state.get('mode', 'single')}\n"
                    f"queue: {queue_len} video(s)"
                ),
                reply_parameters=reply_parameters(message_id),
            )
            return
        if text.lower() in ("/cancel", "cancel"):
            if state["processing"]:
                state["cancel"] = True
                if hasattr(self.batch, "cancel"):
                    self.batch.cancel(user_id, chat_id)
                if hasattr(self.pipeline, "registry"):
                    self.pipeline.registry.cancel(conversation_key(chat_id, user_id))
                state["note"] = "⏹ Cancelled by user"
            else:
                state["mode"] = "single"
                state["note"] = ""
            self.cards.refresh(state)
            return
        if text.lower() in ("done", "/done", "✅ done"):
            error = self.batch.start(state, deadline)
            state["note"] = error or ""
            self.cards.refresh(state)
            return

        found = pick_media(message)
        if not found:
            self.cards.api.call(
                "sendMessage",
                chat_id=chat_id,
                text="Send a video, a round video message, or a video file.",
                reply_parameters=reply_parameters(message_id),
            )
            return
        if message_id in state.get("receipts", []):
            return
        if state["mode"] == "batch" and any(
            item.get("message_id") == message_id
            for item in state.get("queue", []) + state.get("working", [])
        ):
            return

        media_item, kind = found
        size = int(media_item.get("file_size") or 0)
        duration = float(media_item.get("duration") or 0)
        if size > self.config.input_limit_bytes:
            if self.config.mt_ready:
                reason = (
                    f"This video is {size / 1048576:.0f} MB; the MTProto bot cap is "
                    "~2000 MB. Split it and send the parts."
                )
            else:
                reason = (
                    f"This video is {size / 1048576:.1f} MB. Telegram's cloud Bot API "
                    f"caps downloads at {self.config.max_input_mb} MB. Set the "
                    "TELEGRAM_API_ID and TELEGRAM_API_HASH secrets to unlock ~2 GB, "
                    "or send a shorter clip."
                )
            self.cards.api.call(
                "sendMessage",
                chat_id=chat_id,
                text=reason,
                reply_parameters=reply_parameters(message_id),
            )
            if state["mode"] == "single":
                self.store.remove_pending(state, message_id)
            return
        if state["processing"]:
            self.cards.api.call(
                "sendMessage",
                chat_id=chat_id,
                text=(
                    "⏳ The batch is still running — videos after ✖️ Cancel "
                    "start a new batch."
                ),
                reply_parameters=reply_parameters(message_id),
            )
            if state["mode"] == "single":
                self.store.remove_pending(state, message_id)
            return

        item = {
            "media": media_item,
            "kind": kind,
            "chat_id": chat_id,
            "message_id": message_id,
            "size": size,
            "name": short(media_name(media_item, kind)),
            "filename": filename_for(media_item, kind),
            "path": None,
            "duration": duration,
            "status": "",
            "delivered": False,
        }

        if state["mode"] == "batch":
            report = self.cards.report(state, item)
            report("⏳ downloading")
            state["queue"].append(item)
            self.cards.refresh(state)
            others = [q for q in state["queue"] if q is not item]
            error = self.cache.cache_item(
                item, others, lambda pct: report("⏳ downloading", pct)
            )
            if error:
                safe_unlink(item.get("path"), self.config.work_dir)
                state["queue"].remove(item)
                state["note"] = f"❌ {error}"
                self.cards.refresh(state)
                return
            item["status"] = ""
            state["note"] = ""
            self.cards.refresh(state)
            return

        if deadline and duration and time.time() + duration + 120 > deadline:
            state["queue"] = [item] + [
                queued
                for queued in state["queue"]
                if queued.get("message_id") != message_id
            ]
            state["working"] = []
            state["resume"] = True
            state["note"] = (
                "⏳ Not enough time left in this shift; queued for the next."
            )
            self.store.save()
            self.cards.refresh(state)
            raise Defer

        state["queue"] = [
            queued
            for queued in state["queue"]
            if queued.get("message_id") != message_id
        ]
        state["working"] = [item]
        state["processing"] = True
        state["resume"] = True
        self.store.save()
        self.cards.refresh(state)
        try:
            result = self.pipeline.compress_one(
                chat_id,
                message_id,
                media_item,
                kind,
                state["settings"],
                deadline=deadline,
                report=self.cards.report(state, item),
                user_id=conversation_key(chat_id, user_id),
            )
            if "error" in result:
                if state.get("cancel"):
                    item["status"] = "⏹ Cancelled by user"
                else:
                    item["status"] = f"❌ {result['error']}"[:120]
        except Defer:
            state["queue"] = [item] + [
                queued
                for queued in state["queue"]
                if queued.get("message_id") != message_id
            ]
            state["working"] = []
            state["processing"] = False
            state["resume"] = True
            state["note"] = "⏳ Shift too short — queued for the next shift."
            self.store.save()
            self.cards.refresh(state)
            raise
        finally:
            state["processing"] = False
        state["working"] = []
        if state.get("cancel"):
            state["note"] = "⏹ Cancelled by user"
            state["cancel"] = False
        elif "error" not in result:
            state["note"] = ""
        else:
            state["note"] = f"{result['error']}"[:180]
        state["resume"] = False
        if "error" not in result:
            try:
                self.store.record_receipt(state, message_id)
            except StateError as exc:
                print(f"single receipt: {self.config.redact(exc)}", flush=True)
                state["note"] = f"❌ Delivery receipt failed: {exc}"[:180]
                self.cards.refresh(state)
                return
        self.store.remove_pending(state, message_id)
        self.cards.refresh(state)

    def handle_callback(self, cq: dict, deadline: float | None = None) -> None:
        cq_id = cq["id"]
        user = cq.get("from") or {}
        user_id = user.get("id")
        username = user.get("username") or ""
        message = cq.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        data = cq.get("data") or ""
        answered = False

        def answer(text: str | None = None, alert: bool = False) -> None:
            nonlocal answered
            if answered:
                return
            answered = True
            try:
                self.cards.api.call(
                    "answerCallbackQuery",
                    callback_query_id=cq_id,
                    text=text,
                    show_alert=alert,
                )
            except BotError as exc:
                print(
                    f"answerCallbackQuery: {self.config.redact(exc)}",
                    flush=True,
                )

        try:
            if not self.config.is_allowed(user_id, username):
                answer(f"Private bot. Your ID: {whoami(user_id, username)}", True)
                return
            state = self.store.get(user_id, chat_id)
            # NEVER set state["menu"] = None here; keep the card in place
            settings = state["settings"]

            if data == "menu:main":
                state["view"] = "main"
                # Do NOT reset state["mode"] to preserve single/batch mode
            elif data == "menu:single":
                state["mode"] = "single"
                state["view"] = "main"
            elif data == "menu:multi":
                state["mode"] = "batch"
                state["view"] = "main"
            elif data == "menu:settings":
                state["view"] = "settings"
            elif data == "menu:help":
                state["view"] = "help"
            elif data == "setmenu:codec":
                state["view"] = "codec"
            elif data == "setmenu:mode":
                state["view"] = "mode"
            elif data == "setmenu:quality":
                state["view"] = "quality"
            elif data == "setmenu:res":
                state["view"] = "res"
            elif data == "setmenu:fps":
                state["view"] = "fps"
            elif data == "setmenu:audio":
                state["view"] = "audio"
            elif data == "setmenu:output":
                state["view"] = "output"
            elif data == "setmenu:tune":
                state["view"] = "tune"
            elif data.startswith("set:codec:"):
                value = data.split(":", 2)[2]
                if value not in dict(CODECS):
                    answer("Unknown option.", True)
                    return
                settings["codec"] = value
                answer("Option updated")
            elif data.startswith(("set:mode:", "set:m:")):
                value = data.split(":", 2)[2]
                if value not in dict(MODES):
                    answer("Unknown option.", True)
                    return
                settings["mode"] = value
                answer("Option updated")
            elif data.startswith("set:crf:"):
                value = data.split(":", 2)[2]
                try:
                    settings["crf"] = int(value)
                    answer("Option updated")
                except ValueError:
                    answer("Unknown option.", True)
                    return
            elif data.startswith("set:q:"):
                value = data.split(":", 2)[2]
                if value in QUALITIES:
                    settings["quality"] = value
                    settings["crf"] = QUALITIES[value][2]
                    answer("Option updated")
                elif value.isdigit():
                    settings["crf"] = int(value)
                    answer("Option updated")
                else:
                    answer("Unknown option.", True)
                    return
            elif data.startswith("set:size:"):
                value = canonical_target_size(data.split(":", 2)[2])
                if value not in dict(TARGET_SIZES):
                    answer("Unknown option.", True)
                    return
                settings["target_size"] = value
                answer("Option updated")
            elif data.startswith("set:res:"):
                value = data.split(":", 2)[2]
                if value not in RESOLUTION_LABELS and value not in dict(RES_OPTIONS):
                    answer("Unknown option.", True)
                    return
                settings["resolution"] = value
                answer("Option updated")
            elif data.startswith("set:fps:"):
                value = data.split(":", 2)[2]
                if value not in dict(FPS_OPTIONS):
                    answer("Unknown option.", True)
                    return
                settings["fps"] = value
                answer("Option updated")
            elif data.startswith("set:audio:"):
                value = canonical_audio(data.split(":", 2)[2])
                if value not in dict(AUDIO_OPTIONS):
                    answer("Unknown option.", True)
                    return
                settings["audio"] = value
                answer("Option updated")
            elif data.startswith("set:out:"):
                value = data.split(":", 2)[2]
                if value not in OUTPUT_LABELS and value not in dict(OUTPUT_OPTIONS):
                    answer("Unknown option.", True)
                    return
                settings["output"] = value
                answer("Option updated")
            elif data.startswith("set:tune:"):
                value = data.split(":", 2)[2]
                if value not in dict(TUNE_OPTIONS):
                    answer("Unknown option.", True)
                    return
                settings["tune"] = value
                codec = settings.get("codec")
                if value == "none" or codec == "h264":
                    answer("Option updated")
                elif codec == "hevc" and value == "animation":
                    answer("Option updated")
                elif codec == "hevc":
                    answer("Saved; Film tuning applies to H.264 only")
                else:
                    answer("Saved; this codec does not use encoder tuning")
            elif data.startswith("set:tonemap:"):
                value = data.split(":", 2)[2]
                if value not in ("auto", "on", "off"):
                    answer("Unknown option.", True)
                    return
                settings["tonemap"] = value
                answer("Option updated")
            elif data == "multi:done":
                error = self.batch.start(state, deadline)
                answer(error or "Starting…", alert=bool(error))
                self.cards.refresh(state)
                return
            elif data == "multi:clear":
                if state["processing"]:
                    answer("Wait for the batch to finish.", True)
                    return
                if not state.get("queue"):
                    answer("Queue is empty.", True)
                    return
                state["view"] = "clear_confirm"
                answer()
            elif data == "multi:clear_confirm":
                if state["processing"]:
                    answer("Wait for the batch to finish.", True)
                    return
                for item in state.get("queue", []):
                    safe_unlink(item.get("path"), self.config.work_dir)
                state["queue"].clear()
                state["summary"] = ""
                state["note"] = "Batch queue cleared."
                state["view"] = "main"
                answer("Queue cleared")
            elif data in ("multi:cancel", "cancel"):
                if state["processing"]:
                    state["cancel"] = True
                    if hasattr(self.batch, "cancel"):
                        self.batch.cancel(user_id, chat_id)
                    if hasattr(self.pipeline, "registry"):
                        self.pipeline.registry.cancel(conversation_key(chat_id, user_id))
                    state["note"] = "⏹ Cancelled by user"
                    answer("Stopping after the current video.")
                else:
                    answer("Nothing is running.")
            else:
                answer("This menu is outdated. Send /start.", True)
                return
            self.cards.refresh(state)
        finally:
            answer()
