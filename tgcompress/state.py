"""Per-conversation state and its JSON persistence across shifts."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .settings import DEFAULT_SETTINGS, migrate_settings

VIEWS = (
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
    "help",
    "clear_confirm",
)

PUBLIC_ITEM_KEYS = (
    "media",
    "kind",
    "chat_id",
    "message_id",
    "size",
    "name",
    "filename",
    "duration",
)

PUBLIC_MESSAGE_KEYS = (
    "chat",
    "from",
    "message_id",
    "video",
    "video_note",
    "animation",
    "document",
)

RECEIPT_LIMIT = 100


def conversation_key(chat_id: int | None, user_id: int):
    return (chat_id, user_id) if chat_id is not None else user_id


class StateError(RuntimeError):
    """Persistent conversation state could not be written."""


def default_state(user_id: int, chat_id: int | None) -> dict:
    return {
        "user_id": user_id,
        "chat_id": chat_id,
        "mode": "single",
        "view": "main",
        "settings": dict(DEFAULT_SETTINGS),
        "queue": [],
        "working": [],
        "menu": None,
        "cards": [],
        "processing": False,
        "cancel": False,
        "summary": "",
        "note": "",
        "resume": False,
        "receipts": [],
        "pending": [],
    }


def public_item(item: dict) -> dict:
    return {key: item[key] for key in PUBLIC_ITEM_KEYS if key in item}


def public_message(message: dict) -> dict:
    return {key: message[key] for key in PUBLIC_MESSAGE_KEYS if key in message}


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock = threading.RLock()
        self.users: dict[tuple[int | None, int], dict] = {}

    def get(self, user_id: int, chat_id: int | None = None) -> dict:
        with self.lock:
            if chat_id is None:
                matches = [
                    state
                    for (saved_chat, saved_user), state in self.users.items()
                    if saved_user == user_id
                ]
                if len(matches) > 1:
                    raise StateError("chat_id is required for multiple conversations")
                if matches:
                    return matches[0]
            else:
                placeholder = self.users.pop((None, user_id), None)
                if placeholder is not None:
                    placeholder["chat_id"] = chat_id
                    self.users[(chat_id, user_id)] = placeholder
            key = (chat_id, user_id)
            state = self.users.get(key)
            if state is None:
                state = default_state(user_id, chat_id)
                self.users[key] = state
            return state

    def states(self) -> list[dict]:
        with self.lock:
            return list(self.users.values())

    def clear(self) -> None:
        with self.lock:
            self.users.clear()

    def add_pending(self, state: dict, message: dict) -> bool:
        message_id = int(message["message_id"])
        with self.lock:
            existing = state.get("pending", []) + state.get("queue", [])
            existing += state.get("working", [])
            if message_id in state.get("receipts", []) or any(
                int(item["message_id"]) == message_id for item in existing
            ):
                return False
            record = public_message(message)
            previous_resume = state.get("resume", False)
            state.setdefault("pending", []).append(record)
            state["resume"] = True
            try:
                self.save()
            except StateError:
                state["pending"].remove(record)
                state["resume"] = previous_resume
                raise
            return True

    def remove_pending(self, state: dict, message_id: int) -> bool:
        with self.lock:
            pending = state.get("pending", [])
            remaining = [m for m in pending if int(m["message_id"]) != int(message_id)]
            if len(remaining) == len(pending):
                return False
            state["pending"] = remaining
            state["resume"] = bool(
                remaining
                or state.get("working")
                or state.get("processing")
                or (state.get("resume") and state.get("queue"))
            )
            try:
                self.save()
            except StateError:
                state["pending"] = pending
                state["resume"] = True
                raise
            return True

    def record_receipt(self, state: dict, message_id: int) -> None:
        with self.lock:
            receipts = state.setdefault("receipts", [])
            message_id = int(message_id)
            if message_id in receipts:
                return
            receipts.append(message_id)
            del receipts[:-RECEIPT_LIMIT]
            try:
                self.save()
            except BaseException:
                receipts.remove(message_id)
                raise

    def save(self) -> None:
        with self.lock:
            try:
                payload = {}
                for (chat_id, user_id), state in self.users.items():
                    menu = state.get("menu")
                    cards = list(state.get("cards", []))
                    working = [
                        public_item(i)
                        for i in state.get("working", [])
                        if isinstance(i, dict) and "media" in i and "message_id" in i
                    ]
                    queue = [
                        public_item(i)
                        for i in state.get("queue", [])
                        if isinstance(i, dict) and "media" in i and "message_id" in i
                    ]
                    payload[f"{chat_id}:{user_id}"] = {
                        "user_id": state["user_id"],
                        "chat_id": state.get("chat_id"),
                        "mode": state["mode"],
                        "view": state["view"],
                        "settings": dict(state.get("settings", {})),
                        "summary": state.get("summary", ""),
                        "note": state.get("note", ""),
                        "queue": queue,
                        "working": working,
                        "menu": list(menu) if menu else None,
                        "cards": cards,
                        "resume": bool(state.get("resume")),
                        "receipts": list(state.get("receipts", []))[-RECEIPT_LIMIT:],
                        "pending": [
                            public_message(m) for m in state.get("pending", [])
                        ],
                    }
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload), encoding="utf-8")
                tmp.replace(self.path)
            except (OSError, TypeError, ValueError) as exc:
                print(f"state save failed: {type(exc).__name__}", flush=True)
                raise StateError(f"state save failed: {type(exc).__name__}") from exc

    def load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.clear()
        if not isinstance(payload, dict):
            return
        for uid, data in payload.items():
            if not isinstance(data, dict):
                continue
            try:
                user_id = int(data.get("user_id", str(uid).rsplit(":", 1)[-1]))
            except (TypeError, ValueError):
                continue
            chat_id = data.get("chat_id")
            try:
                chat_id = int(chat_id) if chat_id is not None else None
            except (TypeError, ValueError):
                continue
            state = default_state(user_id, chat_id)
            if data.get("mode") in ("single", "batch"):
                state["mode"] = data["mode"]
            if data.get("view") in VIEWS:
                state["view"] = data["view"]
            if "settings" in data and isinstance(data["settings"], dict):
                state["settings"].update(data["settings"])
                migrate_settings(state["settings"])
            state["summary"] = data.get("summary", "")
            state["note"] = data.get("note", "")

            menu = data.get("menu")
            if menu and isinstance(menu, (list, tuple)) and len(menu) == 2:
                state["menu"] = (menu[0], menu[1])
            try:
                state["cards"] = [
                    int(card_id) for card_id in data.get("cards", [])
                ][-100:]
            except (TypeError, ValueError):
                state["cards"] = []
            try:
                state["receipts"] = [
                    int(message_id) for message_id in data.get("receipts", [])
                ][-RECEIPT_LIMIT:]
            except (TypeError, ValueError):
                state["receipts"] = []

            seen_pending = set()
            for message in data.get("pending", []):
                if not isinstance(message, dict) or "message_id" not in message:
                    continue
                try:
                    message_id = int(message["message_id"])
                except (TypeError, ValueError):
                    continue
                if (
                    message_id in state["receipts"]
                    or message_id in seen_pending
                    or not any(key in message for key in PUBLIC_MESSAGE_KEYS[3:])
                ):
                    continue
                state["pending"].append(public_message(message))
                seen_pending.add(message_id)

            def restore_items(items) -> list[dict]:
                restored_items = []
                for item in items if isinstance(items, list) else []:
                    if not isinstance(item, dict) or not item.get("media"):
                        continue
                    try:
                        message_id = int(item["message_id"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if message_id in state["receipts"]:
                        continue
                    restored = public_item(item)
                    restored["message_id"] = message_id
                    restored["path"] = None
                    restored["status"] = ""
                    restored["delivered"] = False
                    restored["duration"] = float(item.get("duration") or 0)
                    restored_items.append(restored)
                return restored_items

            working_data = data.get("working", [])
            working_items = restore_items(working_data)
            queue_items = restore_items(data.get("queue", []))
            state["queue"] = working_items + queue_items
            state["working"] = []
            state["processing"] = False
            started = bool(data.get("resume") or working_data)
            state["resume"] = bool(state["pending"] or (started and state["queue"]))
            self.users[(chat_id, user_id)] = state
