"""The single control card: text, keyboards, and throttled repaints."""

from __future__ import annotations

import json
import time

from .config import Config
from .errors import BotError
from .media import settings_line

# Compression settings options
CODECS = [
    ("h264", "H.264"),
    ("hevc", "H.265 (HEVC)"),
    ("av1", "AV1 (SVT)"),
    ("vp9", "VP9"),
]

MODES = [
    ("crf", "Quality (CRF)"),
    ("size", "Target File Size"),
]

CRF_OPTIONS = [
    ("18", "18"),
    ("22", "22"),
    ("26", "26"),
    ("30", "30"),
    ("34", "34"),
]

TARGET_SIZES = [
    ("10mb", "10MB"),
    ("20mb", "20MB"),
    ("50mb", "50MB"),
    ("50pct", "50%"),
    ("25pct", "25%"),
]

RES_OPTIONS = [
    ("original", "Original"),
    ("2160", "2160p"),
    ("1440", "1440p"),
    ("1080", "1080p"),
    ("720", "720p"),
    ("480", "480p"),
    ("360", "360p"),
    ("240", "240p"),
]

FPS_OPTIONS = [
    ("orig", "Original"),
    ("30", "30 fps"),
    ("24", "24 fps"),
]

AUDIO_OPTIONS = [
    ("auto", "Auto"),
    ("copy", "Pass-through"),
    ("aac", "AAC 96k"),
    ("opus", "Opus 64k"),
    ("mono", "Mono"),
    ("mute", "Mute"),
]

OUTPUT_OPTIONS = [
    ("auto", "Auto"),
    ("video", "Video"),
    ("document", "Document"),
    ("note", "Round Note"),
]

TUNE_OPTIONS = [
    ("none", "Tune: None"),
    ("film", "Tune: Film"),
    ("animation", "Tune: Animation"),
]

TONEMAP_OPTIONS = [
    ("auto", "HDR Tonemap: Auto"),
    ("off", "HDR Tonemap: Off"),
]


def persistent_reply_keyboard() -> str:
    """Persistent bottom reply keyboard for primary bot navigation."""
    return json.dumps(
        {
            "keyboard": [
                [{"text": "🎬 Single Mode"}, {"text": "📚 Batch Mode"}],
                [{"text": "⚙️ Settings"}, {"text": "ℹ️ Status / ID"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        }
    )


reply_keyboard_markup = persistent_reply_keyboard


def help_text(config: Config) -> str:
    return f"""🎬 Video compressor

Everything happens on one card: this message is updated after every
event and old bot messages are deleted, so the chat stays clean.
Settings and batch lists survive shift changes.

1. Tap 🎬 Single or 📚 Multiple.
2. Send videos (video, round video message, or video file).
3. In a batch: keep sending, then tap ▶️ Start (or send `done`).

✖️ Cancel stops after the current video — everything unfinished goes
back to the queue. Failed videos stay queued; tap ▶️ Start to retry.
⚙️ Settings tune codec, mode, resolution, framerate, audio, and advanced tuning.
Current values are marked ✅ and stick forever.

Limits: {config.max_input_mb} MB input without MTProto credentials
(~2000 MB with them, then uploads too), {config.max_duration // 60} min
per video."""


def btn(text: str, data: str, style: str | None = None) -> dict:
    button = {"text": text, "callback_data": data}
    if style:
        button["style"] = style
    return button


def markup(rows: list) -> str:
    return json.dumps({"inline_keyboard": rows})


def short(text: object, limit: int = 28) -> str:
    value = str(text)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def settings_nav_buttons() -> list[dict]:
    return [
        btn("◀ Back to Settings", "menu:settings"),
        btn("🏠 Main Menu", "menu:main"),
    ]


def make_2col_keyboard(
    options: list[tuple[str, str]],
    current: str,
    prefix: str,
    include_nav: bool = True,
) -> list[list[dict]]:
    items = [
        btn(
            ("✅ " if str(key) == str(current) else "") + label,
            f"{prefix}{key}",
            "success" if str(key) == str(current) else None,
        )
        for key, label in options
    ]
    rows: list[list[dict]] = [items[i : i + 2] for i in range(0, len(items), 2)]
    if include_nav:
        rows.append(settings_nav_buttons())
    return rows


def choice_rows(options: list, current: str, prefix: str) -> list:
    return make_2col_keyboard(options, current, prefix, include_nav=True)


def settings_block(state: dict) -> str:
    s = state.get("settings", {})
    codec = dict(CODECS).get(s.get("codec", "h264"), "H.264")
    mode = "Quality (CRF)" if s.get("mode", "crf") == "crf" else "Target File Size"
    if s.get("mode", "crf") == "size":
        target = s.get("target_size", "20mb")
        q_text = f"Target Size: {dict(TARGET_SIZES).get(target, target.upper())}"
    else:
        q_text = f"CRF: {s.get('crf', 26)}"
    res = (
        "Original"
        if s.get("resolution") == "original"
        else f"≤{s.get('resolution', '720')}p"
    )
    fps = "Original" if s.get("fps", "orig") == "orig" else f"{s.get('fps')} fps"
    audio = dict(AUDIO_OPTIONS).get(s.get("audio", "auto"), "Auto")
    output = dict(OUTPUT_OPTIONS).get(s.get("output", "auto"), "Auto")
    tune = s.get("tune", "none").capitalize()
    tonemap = s.get("tonemap", "auto").capitalize()
    return (
        "⚙️ Settings Overview\n\n"
        f"🎞 Codec: {codec}\n"
        f"🎯 Mode: {mode}\n"
        f"🎚 {q_text}\n"
        f"📐 Resolution: {res} (never upscales)\n"
        f"⏱ Framerate: {fps}\n"
        f"🔊 Audio: {audio}\n"
        f"📦 Output: {output}\n"
        f"🎨 Advanced: Tune {tune} · HDR {tonemap}\n\n"
        "Values are marked ✅ and persist between shifts."
    )


def _views_text(state: dict, config: Config) -> str:
    view = state.get("view", "main")
    s = state.get("settings", {})
    if view == "codec":
        return (
            "⚙️ Settings › Video Codec\n\n"
            "Choose video compression codec:\n"
            "• H.264: Universally supported across all devices\n"
            "• H.265 (HEVC): Modern high efficiency, 30-50% smaller\n"
            "• AV1 (SVT): Next-generation open codec, best compression\n"
            "• VP9: High compatibility for web and YouTube"
        )
    if view == "mode":
        return (
            "⚙️ Settings › Compression Mode\n\n"
            "Choose rate control strategy:\n"
            "• Quality (CRF): Keeps visual fidelity constant\n"
            "• Target File Size: Dynamically calculates bitrate to hit a target"
        )
    if view == "quality":
        if s.get("mode") == "size":
            return (
                "⚙️ Settings › Target File Size\n\n"
                "Select target output file size or percentage of source video."
            )
        return (
            "⚙️ Settings › Quality (CRF)\n\n"
            "Constant Rate Factor (CRF):\n"
            "Lower number = higher quality, larger file.\n"
            "• 18: Near lossless (large)\n"
            "• 22: High quality\n"
            "• 26: Balanced (recommended)\n"
            "• 30: Small size\n"
            "• 34: Maximum compression"
        )
    if view == "res":
        return (
            "⚙️ Settings › Resolution\n\n"
            "Long side resolution cap. Videos are never upscaled.\n"
            "Original keeps the source dimensions."
        )
    if view == "fps":
        return (
            "⚙️ Settings › Framerate\n\n"
            "Cap output frame rate. Reducing 60fps to 30fps saves substantial space.\n"
            "Original preserves the source framerate."
        )
    if view == "audio":
        return (
            "⚙️ Settings › Audio\n\n"
            "Select audio encoding:\n"
            "• Auto: Optimized default for selected container\n"
            "• Pass-through: Copy original audio without re-encoding\n"
            "• AAC 96k: Efficient stereo speech & music\n"
            "• Opus 64k: High efficiency speech\n"
            "• Mono: Convert to single audio channel\n"
            "• Mute: Strip all audio tracks"
        )
    if view == "output":
        return (
            "⚙️ Settings › Output Type\n\n"
            "Select message format:\n"
            "• Auto: Match incoming message kind\n"
            "• Video: Regular streamable video\n"
            "• Document: Uncompressed file attachment\n"
            "• Round Note: Circular video message (capped at 60s)"
        )
    if view == "tune":
        return (
            "⚙️ Settings › Advanced Tuning\n\n"
            "Content tuning and HDR color handling:\n"
            "• Film / Animation: Optimizes encoder rate distortion\n"
            "• HDR Tonemap: Convert HDR (BT.2020) color spaces to standard SDR"
        )
    if view == "clear_confirm":
        count = len(state.get("queue", []))
        return (
            "⚠️ Clear Batch Queue\n\n"
            f"Are you sure you want to remove all {count} queued video(s)?\n"
            "This will cancel all pending batch conversions."
        )
    if view == "help":
        return help_text(config)
    return settings_block(state)


def _rows(items: list) -> list[str]:
    lines = []
    for number, item in enumerate(items, 1):
        size = item.get("size", 0) / 1048576
        status = item.get("status") or "queued"
        lines.append(
            f"{number}. {short(item.get('name', 'video'), 24)} — "
            f"{size:.1f} MB · {status}"
        )
    return lines


def card_text(state: dict, config: Config) -> str:
    lines = ["🎬 Video Compressor", f"Settings: {settings_line(state['settings'])}"]
    if state["processing"]:
        lines.append(
            "⏳ Working — only one video runs at a time. Tap ✖️ Cancel to stop."
        )
    if state["summary"]:
        lines.append(state["summary"])
    if state["note"]:
        lines.append(f"ℹ️ {state['note']}")
    lines.append("")
    view = state["view"]
    if state["processing"]:
        lines.append("📚 Batch")
        lines.extend(_rows(state["working"]) or ["starting…"])
        if state["cancel"]:
            lines.append("Stopping after the current video…")
        return "\n".join(lines)
    if view == "main":
        if state["mode"] == "batch":
            if state["queue"]:
                lines.append("📚 Batch — videos to compress:")
                lines.extend(_rows(state["queue"]))
                lines.append("Send more, or tap ▶️ Start.")
            else:
                lines.append("📚 Send videos, then tap ▶️ Start (or send `done`).")
        else:
            lines.append("Send one video whenever ready — or switch to 📚 batch mode.")
    else:
        lines.append(_views_text(state, config))
    return "\n".join(lines)


def settings_main_kb(state: dict) -> list[list[dict]]:
    s = state["settings"]
    codec_label = dict(CODECS).get(s.get("codec", "h264"), "H.264")
    mode_val = s.get("mode", "crf")
    mode_label = "CRF" if mode_val == "crf" else "Target Size"
    if mode_val == "size":
        target = s.get("target_size", "20mb")
        q_label = dict(TARGET_SIZES).get(target, target.upper())
    else:
        crf = s.get("crf", 26)
        q_label = f"CRF {crf}"
    res = s.get("resolution", "720")
    res_label = "Original" if res == "original" else f"{res}p"
    fps = s.get("fps", "orig")
    fps_label = "Original" if fps == "orig" else f"{fps}fps"
    audio_label = dict(AUDIO_OPTIONS).get(s.get("audio", "auto"), "Auto")
    out_label = dict(OUTPUT_OPTIONS).get(s.get("output", "auto"), "Auto")
    tune_val = s.get("tune", "none")
    tune_label = "None" if tune_val == "none" else tune_val.capitalize()

    return [
        [
            btn(f"🎞 Codec · {codec_label}", "setmenu:codec", "primary"),
            btn(f"🎯 Mode · {mode_label}", "setmenu:mode", "primary"),
        ],
        [
            btn(f"🎚 Quality · {q_label}", "setmenu:quality", "primary"),
            btn(f"📐 Res · {res_label}", "setmenu:res", "primary"),
        ],
        [
            btn(f"⏱ FPS · {fps_label}", "setmenu:fps", "primary"),
            btn(f"🔊 Audio · {audio_label}", "setmenu:audio", "primary"),
        ],
        [
            btn(f"📦 Output · {out_label}", "setmenu:output", "primary"),
            btn(f"🎨 Tune · {tune_label}", "setmenu:tune", "primary"),
        ],
        [btn("🏠 Main Menu", "menu:main")],
    ]


def card_kb(state: dict) -> list:
    if state["processing"]:
        return [[btn("✖️ Cancel", "multi:cancel", "danger")]]
    view = state["view"]
    s = state["settings"]

    if view == "codec":
        return make_2col_keyboard(CODECS, s.get("codec", "h264"), "set:codec:")
    if view == "mode":
        return make_2col_keyboard(MODES, s.get("mode", "crf"), "set:mode:")
    if view == "quality":
        if s.get("mode") == "size":
            return make_2col_keyboard(
                TARGET_SIZES, s.get("target_size", "20mb"), "set:size:"
            )
        return make_2col_keyboard(CRF_OPTIONS, str(s.get("crf", 26)), "set:crf:")
    if view == "res":
        return make_2col_keyboard(RES_OPTIONS, s.get("resolution", "720"), "set:res:")
    if view == "fps":
        return make_2col_keyboard(FPS_OPTIONS, s.get("fps", "orig"), "set:fps:")
    if view == "audio":
        return make_2col_keyboard(AUDIO_OPTIONS, s.get("audio", "auto"), "set:audio:")
    if view == "output":
        return make_2col_keyboard(OUTPUT_OPTIONS, s.get("output", "auto"), "set:out:")
    if view == "tune":
        buttons = []
        for key, label in TUNE_OPTIONS:
            cur = s.get("tune", "none")
            buttons.append(
                btn(
                    ("✅ " if key == cur else "") + label,
                    f"set:tune:{key}",
                    "success" if key == cur else None,
                )
            )
        for key, label in TONEMAP_OPTIONS:
            cur = s.get("tonemap", "auto")
            buttons.append(
                btn(
                    ("✅ " if key == cur else "") + label,
                    f"set:tonemap:{key}",
                    "success" if key == cur else None,
                )
            )
        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        rows.append(settings_nav_buttons())
        return rows
    if view == "clear_confirm":
        return [
            [
                btn("⚠️ Confirm Clear", "multi:clear_confirm", "danger"),
                btn("Cancel", "menu:main"),
            ]
        ]
    if view == "help":
        return [[btn("🏠 Main Menu", "menu:main")]]
    if view == "settings":
        return settings_main_kb(state)

    rows = [
        [
            btn(
                "🎬 Single",
                "menu:single",
                "success" if state["mode"] == "single" else "primary",
            ),
            btn(
                f"📚 Batch ({len(state['queue'])})" if state["queue"] else "📚 Batch",
                "menu:multi",
                "success" if state["mode"] == "batch" else "primary",
            ),
        ]
    ]
    if state["mode"] == "batch" and state["queue"]:
        rows.append(
            [
                btn("▶️ Start", "multi:done", "success"),
                btn("🗑 Clear", "multi:clear", "danger"),
            ]
        )
    rows.append([btn("⚙️ Settings", "menu:settings"), btn("❓ Help", "menu:help")])
    return rows


class Throttle:
    """Decides whether a percent-heavy stream may repaint the card now."""

    def __init__(self, interval: float = 2.5) -> None:
        self.interval = interval
        self.stamp = 0.0

    def allow(self, force: bool = False) -> bool:
        now = time.time()
        if force or now - self.stamp >= self.interval:
            self.stamp = now
            return True
        return False


class CardRenderer:
    def __init__(self, api, store, config: Config) -> None:
        self.api = api
        self.store = store
        self.config = config

    def refresh(self, state: dict) -> None:
        """Edit THE card in place; send a fresh one when needed; delete the rest.

        Repositioning is driven by events: handlers clear state["menu"] on incoming
        user messages, which forces a fresh send at the bottom of the chat and deletes
        the old card. On callbacks, state["menu"] is kept intact to edit in-place.
        """
        chat_id = state.get("chat_id")
        if not chat_id:
            return
        body = card_text(state, self.config)
        keys = card_kb(state)
        menu = state.get("menu")
        if menu and menu[0] == chat_id:
            try:
                self.api.call(
                    "editMessageText",
                    chat_id=chat_id,
                    message_id=menu[1],
                    text=body,
                    reply_markup=markup(keys),
                )
            except BotError as exc:
                if "not modified" not in self.config.redact(exc).lower():
                    menu = None  # stale/deleted/failed edit → send a fresh card
        if not menu or menu[0] != chat_id:
            sent = self.api.call(
                "sendMessage",
                chat_id=chat_id,
                text=body,
                reply_markup=markup(keys),
            )
            state["cards"].append(sent["message_id"])
            state["menu"] = (chat_id, sent["message_id"])
        keep_id = state["menu"][1]
        for old_id in [cid for cid in state["cards"] if cid != keep_id]:
            try:
                self.api.call("deleteMessage", chat_id=chat_id, message_id=old_id)
            except BotError:
                pass
        state["cards"] = [keep_id]
        self.store.save()

    def report(self, state: dict, item: dict):
        """report(status, percent) → live row + throttled card repaint."""
        gate = Throttle()

        def report(status: str, percent: int | None = None) -> None:
            text = status if percent is None else f"{status} {percent}%"
            if item.get("status") == text:
                return
            item["status"] = text
            if gate.allow(force=percent is None):
                self.refresh(state)

        return report
