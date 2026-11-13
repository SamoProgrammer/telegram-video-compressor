"""Per-user state and its JSON persistence across shifts."""

from __future__ import annotations

import json
import threading
from pathlib import Path

DEFAULT_SETTINGS = {
    "codec": "h264",
    "mode": "crf",
    "crf": 26,
    "target_size": "20mb",
    "resolution": "720",
    "fps": "orig",
    "audio": "auto",
    "tune": "none",
    "tonemap": "auto",
    "output": "auto",
    "quality": "balanced",
}

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
    }


def public_item(item: dict) -> dict:
    return {key: item[key] for key in PUBLIC_ITEM_KEYS if key in item}


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock = threading.Lock()
        self.users: dict[int, dict] = {}

    def get(self, user_id: int, chat_id: int | None = None) -> dict:
        with self.lock:
            state = self.users.get(user_id)
            if state is None:
                state = default_state(user_id, chat_id)
                self.users[user_id] = state
            if chat_id is not None:
                state["chat_id"] = chat_id
            return state

    def clear(self) -> None:
        with self.lock:
            self.users.clear()

    def save(self) -> None:
        with self.lock:
            payload = {}
            for uid, state in self.users.items():
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
                payload[str(uid)] = {
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
                }
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload))
                tmp.replace(self.path)
            except (OSError, TypeError, ValueError):
                pass

    def load(self) -> None:
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self.clear()
        for uid, data in payload.items():
            try:
                user_id = int(data.get("user_id", uid))
            except (TypeError, ValueError):
                continue
            state = default_state(user_id, data.get("chat_id"))
            if data.get("mode") in ("single", "batch"):
                state["mode"] = data["mode"]
            if data.get("view") in VIEWS:
                state["view"] = data["view"]
            if "settings" in data and isinstance(data["settings"], dict):
                state["settings"].update(data["settings"])
            state["summary"] = data.get("summary", "")
            state["note"] = data.get("note", "")

            menu = data.get("menu")
            if menu and isinstance(menu, (list, tuple)) and len(menu) == 2:
                state["menu"] = (menu[0], menu[1])
            else:
                state["menu"] = None
            state["cards"] = [int(c) for c in data.get("cards", [])]

            # Re-queue any unfinished working items into queue
            working_items = []
            for item in data.get("working", []):
                if item.get("media") and item.get("message_id"):
                    item.setdefault("path", None)
                    item.setdefault("status", "")
                    item["duration"] = float(item.get("duration") or 0)
                    restored = public_item(item)
                    restored["path"] = None
                    restored["status"] = ""
                    restored["delivered"] = False
                    working_items.append(restored)

            queue_items = []
            for item in data.get("queue", []):
                if item.get("media") and item.get("message_id"):
                    item.setdefault("path", None)
                    item.setdefault("status", "")
                    item["duration"] = float(item.get("duration") or 0)
                    restored = public_item(item)
                    restored["path"] = None
                    restored["status"] = ""
                    restored["delivered"] = False
                    queue_items.append(restored)

            state["queue"] = working_items + queue_items
            state["working"] = []
            state["processing"] = False
            self.users[user_id] = state
