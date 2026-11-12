#!/usr/bin/env python3
"""Free Telegram video compressor.

Long-polls Telegram from a GitHub Actions runner, compresses videos with
ffmpeg, and sends them back. See README.md for setup.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

TOKEN = os.environ.get("BOT_TOKEN", "")
API_BASE = os.environ.get("BOT_API_URL", "https://api.telegram.org").rstrip("/")
RUN_SECONDS = int(os.environ.get("BOT_RUN_SECONDS", "19500"))
RESERVE_SECONDS = int(os.environ.get("BOT_RESERVE_SECONDS", "300"))
MAX_INPUT_MB = int(os.environ.get("MAX_INPUT_MB", "20"))
MAX_OUTPUT_MB = int(os.environ.get("MAX_OUTPUT_MB", "50"))
MAX_DURATION = int(os.environ.get("MAX_DURATION", "3600"))
DISK_BUDGET_MB = int(os.environ.get("DISK_BUDGET_MB", "2048"))
ALLOWED = {
    int(x)
    for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",")
    if x
}

TMP = Path(tempfile.mkdtemp(prefix="tg-compress-"))
SESSION = requests.Session()
API_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
JOBS: set[threading.Thread] = set()
USERS: dict[int, dict] = {}

RESOLUTIONS = [
    ("original", "Original"),
    ("2160", "2160p"),
    ("1440", "1440p"),
    ("1080", "1080p"),
    ("720", "720p"),
    ("480", "480p"),
    ("360", "360p"),
    ("240", "240p"),
]
RESOLUTION_LABELS = dict(RESOLUTIONS)
RESOLUTION_CAPS = {
    "2160": 3840,
    "1440": 2560,
    "1080": 1920,
    "720": 1280,
    "480": 854,
    "360": 640,
    "240": 426,
}
QUALITIES = {
    "high": ("High", 22),
    "balanced": ("Balanced", 28),
    "small": ("Small", 33),
}
OUTPUTS = [
    ("auto", "Auto"),
    ("video", "Video"),
    ("document", "Document"),
    ("note", "Round video"),
]
OUTPUT_LABELS = dict(OUTPUTS)
DEFAULT_SETTINGS = {"resolution": "720", "quality": "balanced", "output": "auto"}
NOTE_MAX_SECONDS = 60
DISK_BUDGET = DISK_BUDGET_MB * 1024 * 1024

HELP = f"""🎬 Video compressor

1. Tap Single video or Multiple videos.
2. Send a video, a round video message, or a video file.
3. Multiple videos: send them all, then tap ✅ Done.

⚙️ Settings: resolution, quality, and output type.

Limits: {MAX_INPUT_MB} MB input, {MAX_DURATION // 60} min.
For bigger videos, run the bot with a local Bot API server (see README)."""


class BotError(RuntimeError):
    pass


class Defer(Exception):
    """The current shift is too short for this video; leave it for the next."""


def redact(text: object) -> str:
    value = str(text)
    return value.replace(TOKEN, "***") if TOKEN else value


def api(method: str, files: dict | None = None, **params):
    url = f"{API_BASE}/bot{TOKEN}/{method}"
    clean = {
        key: ("true" if value is True else "false" if value is False else value)
        for key, value in params.items()
        if value is not None
    }
    try:
        with API_LOCK:
            response = SESSION.post(url, data=clean, files=files, timeout=600)
    except requests.RequestException:
        raise BotError(f"{method}: network error") from None
    try:
        payload = response.json()
    except ValueError:
        raise BotError(f"{method}: HTTP {response.status_code}") from None
    if not payload.get("ok"):
        description = payload.get("description") or f"HTTP {response.status_code}"
        raise BotError(f"{method}: {description}")
    return payload["result"]


def get_updates(offset: int | None):
    params = {
        "timeout": 50,
        "allowed_updates": json.dumps(["message", "callback_query"]),
    }
    if offset is not None:
        params["offset"] = offset
    try:
        response = requests.get(
            f"{API_BASE}/bot{TOKEN}/getUpdates", params=params, timeout=70
        )
    except requests.RequestException:
        raise BotError("getUpdates: network error") from None
    try:
        payload = response.json()
    except ValueError:
        raise BotError(f"getUpdates: HTTP {response.status_code}") from None
    if not payload.get("ok"):
        description = payload.get("description") or f"HTTP {response.status_code}"
        raise BotError(f"getUpdates: {description}")
    return payload["result"]


def download(file_id: str) -> Path:
    info = api("getFile", file_id=file_id)
    file_path = info.get("file_path", "")
    # A local Bot API server returns an absolute path and no 20 MB download cap.
    if file_path and os.path.isabs(file_path) and os.path.exists(file_path):
        return Path(file_path)
    if not file_path:
        raise BotError("Telegram returned no file path")
    dest = TMP / f"in-{time.time_ns()}.bin"
    try:
        with requests.get(
            f"{API_BASE}/file/bot{TOKEN}/{file_path}", stream=True, timeout=600
        ) as response:
            if response.status_code != 200:
                raise BotError(f"Download failed (HTTP {response.status_code})")
            with dest.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    handle.write(chunk)
    except requests.RequestException:
        raise BotError("Download failed (network error)") from None
    return dest


def probe(path: Path) -> dict:
    try:
        raw = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            text=True,
            timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise BotError(f"Could not read video: {exc}") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise BotError("Could not read video metadata") from None
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video:
        raise BotError("No video stream found")
    duration = (
        data.get("format", {}).get("duration") or video.get("duration") or 0
    )
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "duration": duration,
        "has_audio": audio is not None,
    }


def resolution_label(value: str) -> str:
    return "Original" if value == "original" else f"≤{value}p"


def settings_line(settings: dict) -> str:
    resolution = resolution_label(settings.get("resolution", "720"))
    quality = QUALITIES.get(settings.get("quality", "balanced"), ("Balanced", 28))[0]
    output = OUTPUT_LABELS.get(settings.get("output", "auto"), "Auto")
    return f"{resolution} • {quality} • {output}"


def target_kind(kind: str, settings: dict, duration: float) -> tuple[str, bool]:
    output = settings.get("output", "auto")
    if output == "auto":
        out = kind
    elif output == "note":
        out = "video_note"
    elif output == "document":
        out = "document"
    else:
        out = "video"
    if out == "video_note" and duration > NOTE_MAX_SECONDS:
        return "video", True
    return out, False


def video_filter(kind: str, settings: dict | None = None) -> str:
    settings = settings or DEFAULT_SETTINGS
    resolution = settings.get("resolution", "720")
    if kind == "video_note":
        cap = 640 if resolution == "original" else min(int(resolution), 640)
        return (
            f"crop='min(iw,ih)':'min(iw,ih)',"
            f"scale='min({cap},iw)':'min({cap},ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2"
        )
    if resolution == "original":
        return "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    cap = RESOLUTION_CAPS.get(resolution, 1280)
    return (
        f"scale='min({cap},iw)':'min({cap},ih)':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2"
    )


def compress(
    src: Path, dst: Path, kind: str, settings: dict | None = None
) -> dict:
    settings = settings or DEFAULT_SETTINGS
    # shortcut: HDR is flattened to SDR without tonemapping; add zscale if needed.
    info = probe(src)
    if info["duration"] > MAX_DURATION:
        raise BotError(
            f"Video is {info['duration'] / 60:.0f} min; "
            f"limit is {MAX_DURATION // 60} min."
        )
    crf = QUALITIES.get(settings.get("quality", "balanced"), ("Balanced", 28))[1]
    if kind == "video_note":
        crf = min(51, crf + 2)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        video_filter(kind, settings),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(crf),
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-max_muxing_queue_size",
        "1024",
        str(dst),
    ]
    try:
        subprocess.run(
            cmd,
            check=True,
            timeout=MAX_DURATION,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired:
        raise BotError("Compression timed out") from None
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise BotError(f"ffmpeg failed: {detail[-1] if detail else 'unknown'}") from None
    except OSError as exc:
        raise BotError(f"ffmpeg is not installed: {exc}") from None
    return probe(dst)


def pick_media(message: dict):
    if "video_note" in message:
        return message["video_note"], "video_note"
    if "video" in message:
        return message["video"], "video"
    if "animation" in message:
        return message["animation"], "video"
    document = message.get("document")
    if document:
        name = (document.get("file_name") or "").lower()
        mime = (document.get("mime_type") or "").lower()
        suffixes = {
            ".mp4",
            ".mov",
            ".mkv",
            ".webm",
            ".avi",
            ".m4v",
            ".3gp",
            ".3g2",
            ".ts",
            ".mts",
            ".m2ts",
            ".ogv",
        }
        if mime.startswith("video/") or Path(name).suffix in suffixes:
            return document, "document"
    return None


def reply_params(message_id: int) -> str:
    return json.dumps(
        {"message_id": message_id, "allow_sending_without_reply": True}
    )


def btn(text: str, data: str, style: str | None = None) -> dict:
    button = {"text": text, "callback_data": data}
    if style:
        button["style"] = style
    return button


def markup(rows: list) -> str:
    return json.dumps({"inline_keyboard": rows})


def edit_status(chat_id: int, message_id: int, text: str) -> None:
    try:
        api("editMessageText", chat_id=chat_id, message_id=message_id, text=text)
    except BotError:
        pass


def render_text(
    chat_id: int, message_id: int, text: str, keyboard: list | None = None
) -> int:
    """Edit a text menu; if it is a media message, send a new one instead."""
    reply_markup = markup(keyboard) if keyboard else None
    try:
        api(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )
        return message_id
    except BotError as exc:
        if "not modified" in redact(exc).lower():
            return message_id
        result = api(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
        )
        return result["message_id"]


def send_original(
    chat_id: int,
    message_id: int,
    media: dict,
    kind: str,
    keyboard: list | None = None,
) -> None:
    reply = reply_params(message_id)
    reply_markup = markup(keyboard) if keyboard else None
    if kind == "video_note":
        api(
            "sendVideoNote",
            chat_id=chat_id,
            video_note=media["file_id"],
            reply_parameters=reply,
            reply_markup=reply_markup,
        )
    elif kind == "document":
        api(
            "sendDocument",
            chat_id=chat_id,
            document=media["file_id"],
            reply_parameters=reply,
            reply_markup=reply_markup,
        )
    else:
        api(
            "sendVideo",
            chat_id=chat_id,
            video=media["file_id"],
            reply_parameters=reply,
            reply_markup=reply_markup,
        )


def send_result(
    chat_id: int,
    message_id: int,
    path: Path,
    kind: str,
    info: dict,
    caption: str,
    filename: str = "compressed.mp4",
    keyboard: list | None = None,
) -> None:
    reply = reply_params(message_id)
    reply_markup = markup(keyboard) if keyboard else None
    duration = max(1, int(info["duration"]))
    with path.open("rb") as handle:
        if kind == "video_note":
            api(
                "sendVideoNote",
                files={"video_note": (filename, handle, "video/mp4")},
                chat_id=chat_id,
                duration=duration,
                length=int(info["width"]),
                reply_parameters=reply,
                reply_markup=reply_markup,
            )
        elif kind == "document":
            api(
                "sendDocument",
                files={"document": (filename, handle, "video/mp4")},
                chat_id=chat_id,
                caption=caption,
                reply_parameters=reply,
                reply_markup=reply_markup,
            )
        else:
            api(
                "sendVideo",
                files={"video": (filename, handle, "video/mp4")},
                chat_id=chat_id,
                caption=caption,
                supports_streaming="true",
                width=int(info["width"]),
                height=int(info["height"]),
                duration=duration,
                reply_parameters=reply,
                reply_markup=reply_markup,
            )


def get_state(user_id: int, chat_id: int | None = None) -> dict:
    with STATE_LOCK:
        state = USERS.get(user_id)
        if state is None:
            state = {
                "mode": "single",
                "settings": dict(DEFAULT_SETTINGS),
                "queue": [],
                "processing": False,
                "cancel": False,
                "menu": None,
            }
            USERS[user_id] = state
        if chat_id is not None:
            state["chat_id"] = chat_id
        return state


def short(text: object, limit: int = 42) -> str:
    value = str(text)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def media_name(media: dict, kind: str) -> str:
    if kind == "video_note":
        return "round video"
    return media.get("file_name") or ("video file" if kind == "document" else "video")


def filename_for(media: dict, kind: str) -> str:
    if kind == "video_note":
        return "video-note-compressed.mp4"
    if kind == "document":
        name = Path(media.get("file_name") or "video.mp4")
        return f"{name.stem}-compressed.mp4"
    return "compressed.mp4"


def safe_unlink(path: Path | None) -> None:
    if path and TMP in path.parents:
        path.unlink(missing_ok=True)


def queue_used(queue: list) -> int:
    total = 0
    for item in queue:
        path = item.get("path")
        if path and path.exists():
            total += path.stat().st_size
    return total


def cache_item(item: dict, queue: list) -> str | None:
    """Download a queued video and evict the oldest cached file if needed."""
    size = item["size"]
    if size > DISK_BUDGET:
        item["path"] = None
        return None
    # Keep 1 GB free for ffmpeg outputs and the rest of the runner.
    if shutil.disk_usage(TMP).free < size + 1024 * 1024 * 1024:
        item["path"] = None
        return None
    while queue and queue_used(queue) + size > DISK_BUDGET:
        victim = next((q for q in queue if q.get("path")), None)
        if victim is None:
            break
        safe_unlink(victim["path"])
        victim["path"] = None
    try:
        path = download(item["media"]["file_id"])
    except BotError as exc:
        return f"Could not download: {redact(exc)}"
    try:
        info = probe(path)
    except BotError as exc:
        safe_unlink(path)
        return f"Not a readable video: {redact(exc)}"
    item["path"] = path
    item["duration"] = info["duration"]
    if info["duration"] > MAX_DURATION:
        safe_unlink(path)
        item["path"] = None
        return f"Video is longer than {MAX_DURATION // 60} min."
    return None


def compress_one(
    chat_id: int,
    reply_to: int,
    media: dict,
    kind: str,
    settings: dict,
    deadline: float | None = None,
    path: Path | None = None,
    keyboard: list | None = None,
    filename: str = "compressed.mp4",
) -> dict:
    status = api(
        "sendMessage",
        chat_id=chat_id,
        text="⏳ Downloading…",
        reply_parameters=reply_params(reply_to),
    )
    status_id = status["message_id"]
    src = path
    out = None
    try:
        if src is None or not src.exists():
            src = download(media["file_id"])
        actual = probe(src)
        if deadline and time.time() + actual["duration"] + 120 > deadline:
            edit_status(chat_id, status_id, "⏳ Not enough time left in this shift.")
            raise Defer
        out_kind, fallback = target_kind(kind, settings, actual["duration"])
        edit_status(chat_id, status_id, "⚙️ Compressing…")
        out = TMP / f"out-{time.time_ns()}.mp4"
        info = compress(src, out, out_kind, settings)
        in_mb = src.stat().st_size / 1048576
        out_mb = out.stat().st_size / 1048576
        if out_mb >= in_mb * 0.98:
            try:
                send_original(chat_id, reply_to, media, out_kind, keyboard)
            except BotError:
                if out_kind == kind:
                    raise
                send_original(chat_id, reply_to, media, kind, keyboard)
            edit_status(
                chat_id,
                status_id,
                f"Already efficient ({in_mb:.1f} MB). Sent the original.",
            )
        else:
            if out.stat().st_size > MAX_OUTPUT_MB * 1024 * 1024:
                raise BotError(
                    f"Compressed file is {out_mb:.1f} MB; the upload limit is "
                    f"{MAX_OUTPUT_MB} MB. Choose a lower resolution or quality."
                )
            caption = (
                f"✅ {in_mb:.1f} MB → {out_mb:.1f} MB "
                f"({100 * (1 - out_mb / in_mb):.0f}% smaller) • H.264 • "
                f"{settings_line(settings)}"
            )
            if fallback:
                caption += (
                    "\nRound video is limited to 60 s, so this was sent as a video."
                )
            edit_status(chat_id, status_id, "📤 Uploading…")
            send_result(
                chat_id, reply_to, out, out_kind, info, caption, filename, keyboard
            )
            edit_status(chat_id, status_id, caption)
        return {
            "in": in_mb,
            "out": out_mb if out_mb < in_mb * 0.98 else in_mb,
            "saved": out_mb < in_mb * 0.98,
            "kind": out_kind,
            "fallback": fallback,
        }
    except Defer:
        raise
    except BotError as exc:
        edit_status(chat_id, status_id, f"❌ {redact(exc)[:350]}")
        return {"error": redact(exc)}
    except Exception as exc:  # noqa: BLE001 - never kill the shift
        edit_status(chat_id, status_id, f"❌ Unexpected error: {redact(exc)[:300]}")
        return {"error": redact(exc)}
    finally:
        for path in (src, out):
            if path and TMP in path.parents:
                path.unlink(missing_ok=True)


def main_text(state: dict) -> str:
    queued = (
        f"\n📚 {len(state['queue'])} video(s) queued." if state["queue"] else ""
    )
    return (
        "🎬 Video Compressor\n\n"
        "Compress videos to H.264 with ffmpeg.\n"
        f"Settings: {settings_line(state['settings'])}{queued}\n\n"
        "Choose how to send videos."
    )


def main_kb(state: dict) -> list:
    label = (
        f"📚 Multiple videos ({len(state['queue'])})"
        if state["queue"]
        else "📚 Multiple videos"
    )
    return [
        [
            btn("🎬 Single video", "menu:single", "primary"),
            btn(label, "menu:multi", "primary"),
        ],
        [btn("⚙️ Settings", "menu:settings"), btn("❓ Help", "menu:help")],
    ]


def settings_text(state: dict) -> str:
    settings = state["settings"]
    resolution = resolution_label(settings["resolution"])
    quality = QUALITIES.get(settings["quality"], ("Balanced", 28))
    output = OUTPUT_LABELS.get(settings["output"], "Auto")
    return (
        "⚙️ Settings\n\n"
        f"Resolution: {resolution}\n"
        f"Quality: {quality[0]} (CRF {quality[1]})\n"
        f"Output: {output}\n\n"
        "Resolution never upscales. Output Auto keeps the input type."
    )


def settings_kb(state: dict) -> list:
    settings = state["settings"]
    resolution = resolution_label(settings["resolution"])
    quality = QUALITIES.get(settings["quality"], ("Balanced", 28))[0]
    output = OUTPUT_LABELS.get(settings["output"], "Auto")
    return [
        [btn(f"Resolution · {resolution}", "setmenu:res", "primary")],
        [btn(f"Quality · {quality}", "setmenu:quality", "primary")],
        [btn(f"Output · {output}", "setmenu:output", "primary")],
        [btn("⬅️ Back", "menu:main")],
    ]


def choice_kb(options: list, current: str, prefix: str) -> list:
    items = [
        btn(
            ("✅ " if key == current else "") + label,
            f"{prefix}{key}",
            "success" if key == current else None,
        )
        for key, label in options
    ]
    rows = [items[i : i + 2] for i in range(0, len(items), 2)]
    rows.append([btn("⬅️ Back", "menu:settings")])
    return rows


def queue_text(state: dict) -> str:
    items = state["queue"]
    total = sum(item["size"] for item in items) / 1048576
    lines = [f"📚 Batch — {len(items)} video(s), {total:.1f} MB"]
    if items:
        lines.append("")
    for number, item in enumerate(items[:15], 1):
        lines.append(
            f"{number}. {short(item['name'], 32)} — "
            f"{item['size'] / 1048576:.1f} MB"
        )
    if len(items) > 15:
        lines.append(f"…and {len(items) - 15} more")
    lines += [
        "",
        f"Settings: {settings_line(state['settings'])}",
        "",
        "Send more videos, then tap ✅ Done.",
    ]
    return "\n".join(lines)


def queue_kb(state: dict) -> list:
    return [
        [
            btn("✅ Done", "multi:done", "success"),
            btn("🗑 Clear", "multi:clear", "danger"),
        ],
        [btn("⚙️ Settings", "menu:settings"), btn("🏠 Menu", "menu:main")],
    ]


def result_kb() -> list:
    return [
        [btn("🔁 Another video", "menu:single", "primary")],
        [btn("⚙️ Settings", "menu:settings"), btn("🏠 Menu", "menu:main")],
    ]


def new_batch_kb() -> list:
    return [
        [btn("🔁 New batch", "menu:multi", "primary")],
        [btn("⚙️ Settings", "menu:settings"), btn("🏠 Menu", "menu:main")],
    ]


def start_batch(
    state: dict,
    user_id: int,
    chat_id: int,
    menu_id: int,
    deadline: float | None,
) -> str | None:
    if state["processing"]:
        return "A batch is still processing."
    if not state["queue"]:
        return "Queue is empty. Send videos first."
    if deadline and time.time() + 120 > deadline:
        return "The shift is ending. Try after the next shift starts."
    items = list(state["queue"])
    state["queue"] = []
    state["processing"] = True
    state["cancel"] = False
    state["menu"] = (chat_id, menu_id)
    thread = threading.Thread(
        target=process_batch,
        args=(user_id, chat_id, menu_id, items, dict(state["settings"]), deadline),
        daemon=True,
    )
    with STATE_LOCK:
        JOBS.add(thread)
    thread.start()
    return None


def process_batch(
    user_id: int,
    chat_id: int,
    menu_id: int,
    items: list,
    settings: dict,
    deadline: float | None = None,
) -> None:
    state = get_state(user_id, chat_id)
    total = len(items)
    stats = {
        "ok": 0,
        "failed": 0,
        "in": 0.0,
        "out": 0.0,
        "cancelled": False,
        "deferred": False,
        "remaining": [],
    }
    index = 0
    try:
        for index, item in enumerate(items):
            if state.get("cancel"):
                stats["cancelled"] = True
                stats["remaining"].extend(items[index:])
                break
            duration = float(item.get("duration") or 0)
            if deadline and time.time() + duration + 120 > deadline:
                stats["deferred"] = True
                stats["remaining"].extend(items[index:])
                break
            render_text(
                chat_id,
                menu_id,
                f"⚙️ Processing {index + 1}/{total} — {short(item['name'])}\n\n"
                "Cancel stops after the current video.",
                [
                    [btn("✖️ Cancel", "multi:cancel", "danger")],
                    [btn("🏠 Menu", "menu:main")],
                ],
            )
            try:
                result = compress_one(
                    chat_id,
                    item["message_id"],
                    item["media"],
                    item["kind"],
                    settings,
                    deadline=deadline,
                    path=item.get("path"),
                    filename=item["filename"],
                )
            except Defer:
                item["path"] = None
                stats["deferred"] = True
                stats["remaining"].extend(items[index:])
                break
            if "error" in result:
                item["path"] = None
                stats["failed"] += 1
                stats["remaining"].append(item)
            else:
                stats["ok"] += 1
                stats["in"] += result["in"]
                stats["out"] += result["out"]
    except Exception as exc:  # noqa: BLE001 - keep the rest of the queue
        print(f"batch: {redact(exc)}", flush=True)
        stats["remaining"].extend(items[index:])
    finally:
        with STATE_LOCK:
            state["queue"] = stats["remaining"] + state["queue"]
            state["processing"] = False
            state["cancel"] = False
        done = stats["ok"] + stats["failed"]
        if stats["cancelled"]:
            head = f"⏹ Cancelled — {done}/{total} processed."
        elif stats["deferred"]:
            head = f"⏳ Shift ending — {done}/{total} processed."
        elif stats["failed"] and not stats["ok"]:
            head = f"❌ Batch failed — {stats['failed']} error(s)."
        else:
            head = f"✅ Batch complete — {stats['ok']}/{total} compressed."
        body = ""
        if stats["in"]:
            saved = 100 * (1 - stats["out"] / stats["in"])
            body += (
                f"\n{stats['in']:.1f} MB → {stats['out']:.1f} MB "
                f"({saved:.0f}% smaller)."
            )
        if stats["failed"]:
            body += f"\n❌ {stats['failed']} failed. Tap ✅ Done to retry."
        if stats["deferred"]:
            body += "\nSend the remaining videos again after the next shift starts."
        if stats["cancelled"] and state["queue"]:
            body += "\nTap ✅ Done to continue."
        try:
            render_text(
                chat_id,
                menu_id,
                head + body,
                queue_kb(state) if state["queue"] else new_batch_kb(),
            )
        except Exception as exc:  # noqa: BLE001 - summary is best effort
            print(f"batch summary: {redact(exc)}", flush=True)
        with STATE_LOCK:
            JOBS.discard(threading.current_thread())


def handle_callback(cq: dict, deadline: float | None = None) -> None:
    cq_id = cq["id"]
    user_id = (cq.get("from") or {}).get("id")
    message = cq.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    data = cq.get("data") or ""
    answered = False

    def answer(text: str | None = None, alert: bool = False) -> None:
        nonlocal answered
        if answered:
            return
        answered = True
        try:
            api(
                "answerCallbackQuery",
                callback_query_id=cq_id,
                text=text,
                show_alert=alert,
            )
        except BotError as exc:
            print(f"answerCallbackQuery: {redact(exc)}", flush=True)

    try:
        if ALLOWED and user_id not in ALLOWED:
            answer("This bot is private.", True)
            return
        if chat_id is None or message_id is None:
            answer("This menu is outdated. Send /start.", True)
            return
        state = get_state(user_id, chat_id)
        settings = state["settings"]
        if data == "menu:main":
            state["mode"] = "single"
            render_text(chat_id, message_id, main_text(state), main_kb(state))
        elif data == "menu:help":
            render_text(
                chat_id, message_id, HELP, [[btn("⬅️ Back", "menu:main")]]
            )
        elif data == "menu:single":
            state["mode"] = "single"
            render_text(
                chat_id,
                message_id,
                "🎬 Single video\n\n"
                "Send one video, round video message, or video file.\n"
                f"Settings: {settings_line(settings)}",
                [
                    [
                        btn("⚙️ Settings", "menu:settings"),
                        btn("🏠 Menu", "menu:main"),
                    ]
                ],
            )
            answer("Send a video")
        elif data == "menu:multi":
            if state["processing"]:
                answer("A batch is still processing.", True)
            else:
                state["mode"] = "multi"
                menu_id = render_text(
                    chat_id, message_id, queue_text(state), queue_kb(state)
                )
                state["menu"] = (chat_id, menu_id)
                answer()
        elif data == "menu:settings":
            render_text(
                chat_id, message_id, settings_text(state), settings_kb(state)
            )
        elif data == "setmenu:res":
            render_text(
                chat_id,
                message_id,
                "📐 Resolution\n\nLong side cap. Original keeps the source size.",
                choice_kb(RESOLUTIONS, settings["resolution"], "set:res:"),
            )
        elif data == "setmenu:quality":
            options = [
                (key, f"{value[0]} (CRF {value[1]})")
                for key, value in QUALITIES.items()
            ]
            render_text(
                chat_id,
                message_id,
                "🎚 Quality\n\nHigher quality means a bigger file.",
                choice_kb(options, settings["quality"], "set:q:"),
            )
        elif data == "setmenu:output":
            render_text(
                chat_id,
                message_id,
                "📦 Output\n\nAuto sends the same kind of message you sent.",
                choice_kb(OUTPUTS, settings["output"], "set:out:"),
            )
        elif data.startswith("set:res:"):
            value = data.split(":", 2)[2]
            if value not in RESOLUTION_LABELS:
                answer("Unknown setting.", True)
            else:
                settings["resolution"] = value
                render_text(
                    chat_id, message_id, settings_text(state), settings_kb(state)
                )
                answer(f"Resolution: {RESOLUTION_LABELS[value]}")
        elif data.startswith("set:q:"):
            value = data.split(":", 2)[2]
            if value not in QUALITIES:
                answer("Unknown setting.", True)
            else:
                settings["quality"] = value
                render_text(
                    chat_id, message_id, settings_text(state), settings_kb(state)
                )
                answer(f"Quality: {QUALITIES[value][0]}")
        elif data.startswith("set:out:"):
            value = data.split(":", 2)[2]
            if value not in OUTPUT_LABELS:
                answer("Unknown setting.", True)
            else:
                settings["output"] = value
                render_text(
                    chat_id, message_id, settings_text(state), settings_kb(state)
                )
                answer(f"Output: {OUTPUT_LABELS[value]}")
        elif data == "multi:done":
            menu = state.get("menu")
            menu_id = menu[1] if menu and menu[0] == chat_id else message_id
            error = start_batch(state, user_id, chat_id, menu_id, deadline)
            answer(error or "Starting…", alert=bool(error))
        elif data == "multi:clear":
            if state["processing"]:
                answer("A batch is still processing.", True)
            else:
                for item in state["queue"]:
                    safe_unlink(item.get("path"))
                state["queue"].clear()
                menu_id = render_text(
                    chat_id, message_id, queue_text(state), queue_kb(state)
                )
                state["menu"] = (chat_id, menu_id)
                answer("Queue cleared")
        elif data == "multi:cancel":
            if state["processing"]:
                state["cancel"] = True
                answer("Stopping after the current video.")
            else:
                answer("Nothing is running.")
        else:
            answer("This menu is outdated. Send /start.", True)
    finally:
        answer()


def handle(message: dict, deadline: float | None = None) -> None:
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]
    user_id = (message.get("from") or {}).get("id")

    if ALLOWED and user_id not in ALLOWED:
        api(
            "sendMessage",
            chat_id=chat_id,
            text="This bot is private.",
            reply_parameters=reply_params(message_id),
        )
        return

    text = (message.get("text") or "").strip()
    state = get_state(user_id, chat_id)

    if text.startswith("/start"):
        state["mode"] = "single"
        api(
            "sendMessage",
            chat_id=chat_id,
            text=main_text(state),
            reply_markup=markup(main_kb(state)),
            reply_parameters=reply_params(message_id),
        )
        return
    if text.startswith("/help"):
        api(
            "sendMessage",
            chat_id=chat_id,
            text=HELP,
            reply_markup=markup([[btn("🏠 Menu", "menu:main")]]),
            reply_parameters=reply_params(message_id),
        )
        return
    if text.startswith("/settings"):
        api(
            "sendMessage",
            chat_id=chat_id,
            text=settings_text(state),
            reply_markup=markup(settings_kb(state)),
            reply_parameters=reply_params(message_id),
        )
        return
    if text.startswith("/id"):
        api(
            "sendMessage",
            chat_id=chat_id,
            text=f"user_id: {user_id}\nchat_id: {chat_id}",
            reply_parameters=reply_params(message_id),
        )
        return
    if text.lower() in ("/cancel", "cancel"):
        if state["processing"]:
            state["cancel"] = True
            reply = "Stopping after the current video."
        elif state["queue"]:
            state["mode"] = "single"
            reply = "Cancelled. Your queue is kept; open Multiple videos to resume."
        else:
            reply = "Nothing is running."
        api(
            "sendMessage",
            chat_id=chat_id,
            text=reply,
            reply_parameters=reply_params(message_id),
        )
        return
    if text.lower() in ("done", "/done", "✅ done"):
        if state["processing"]:
            reply = "A batch is still processing."
        elif not state["queue"]:
            reply = "Queue is empty. Send videos first."
        else:
            menu = state.get("menu")
            if menu:
                menu_id = menu[1]
            else:
                sent = api(
                    "sendMessage", chat_id=chat_id, text="⚙️ Starting batch…"
                )
                menu_id = sent["message_id"]
            error = start_batch(state, user_id, chat_id, menu_id, deadline)
            reply = error or "Starting…"
        api(
            "sendMessage",
            chat_id=chat_id,
            text=reply,
            reply_parameters=reply_params(message_id),
        )
        return

    found = pick_media(message)
    if not found:
        api(
            "sendMessage",
            chat_id=chat_id,
            text="Send a video, a round video message, or a video file.",
            reply_markup=markup([[btn("🏠 Menu", "menu:main")]]),
            reply_parameters=reply_params(message_id),
        )
        return

    media, kind = found
    size = int(media.get("file_size") or 0)
    duration = float(media.get("duration") or 0)
    if size > MAX_INPUT_MB * 1024 * 1024:
        api(
            "sendMessage",
            chat_id=chat_id,
            text=(
                f"This video is {size / 1048576:.1f} MB. Telegram's cloud Bot API "
                f"only lets bots download files up to {MAX_INPUT_MB} MB. "
                "Send a shorter clip or a round video message, or run the bot "
                "with a local Bot API server."
            ),
            reply_parameters=reply_params(message_id),
        )
        return
    if state["processing"]:
        api(
            "sendMessage",
            chat_id=chat_id,
            text="⏳ Wait for the current batch to finish, or send /cancel.",
            reply_parameters=reply_params(message_id),
        )
        return

    if state["mode"] == "multi":
        name = media_name(media, kind)
        item = {
            "media": media,
            "kind": kind,
            "message_id": message_id,
            "size": size,
            "name": short(name),
            "filename": filename_for(media, kind),
            "path": None,
            "duration": duration,
        }
        sent = api(
            "sendMessage",
            chat_id=chat_id,
            text=f"⏳ Adding {short(name)}…",
            reply_parameters=reply_params(message_id),
        )
        menu_id = sent["message_id"]
        error = cache_item(item, state["queue"])
        if error:
            render_text(
                chat_id,
                menu_id,
                f"❌ {error}",
                [[btn("🏠 Menu", "menu:main")]],
            )
            return
        state["queue"].append(item)
        state["menu"] = (chat_id, menu_id)
        render_text(chat_id, menu_id, queue_text(state), queue_kb(state))
        return

    if deadline and duration and time.time() + duration + 120 > deadline:
        api(
            "sendMessage",
            chat_id=chat_id,
            text="⏳ Not enough time left in this shift.",
            reply_parameters=reply_params(message_id),
        )
        raise Defer

    state["processing"] = True
    try:
        compress_one(
            chat_id,
            message_id,
            media,
            kind,
            dict(state["settings"]),
            deadline=deadline,
            keyboard=result_kb(),
        )
    finally:
        state["processing"] = False


def main() -> None:
    if not TOKEN:
        sys.exit("BOT_TOKEN is not set")
    if not ALLOWED:
        print(
            "WARNING: ALLOWED_USER_IDS is empty; anyone can use this bot.",
            flush=True,
        )
    print(
        f"Bot started. API={API_BASE} shift={RUN_SECONDS}s reserve={RESERVE_SECONDS}s",
        flush=True,
    )
    try:
        api("deleteWebhook", drop_pending_updates=False)
    except BotError as exc:
        print(f"deleteWebhook: {redact(exc)}", flush=True)
    started = time.time()
    deadline = started + RUN_SECONDS - RESERVE_SECONDS
    offset = None
    try:
        while time.time() < deadline:
            try:
                updates = get_updates(offset)
            except BotError as exc:
                message = redact(exc)
                print(f"getUpdates: {message}", flush=True)
                if "unauthorized" in message.lower() or "not found" in message.lower():
                    sys.exit("Bad BOT_TOKEN")
                time.sleep(5)
                continue
            for update in updates:
                if time.time() >= deadline:
                    print(
                        "Reserve reached; leaving the rest for the next shift.",
                        flush=True,
                    )
                    return
                if "message" in update:
                    try:
                        handle(update["message"], deadline)
                    except Defer:
                        print("Deferred to the next shift.", flush=True)
                        return
                    except BotError as exc:
                        print(f"handle: {redact(exc)}", flush=True)
                    except Exception as exc:  # noqa: BLE001
                        print(
                            f"handle: unexpected {type(exc).__name__}: {redact(exc)}",
                            flush=True,
                        )
                elif "callback_query" in update:
                    try:
                        handle_callback(update["callback_query"], deadline)
                    except BotError as exc:
                        print(f"callback: {redact(exc)}", flush=True)
                    except Exception as exc:  # noqa: BLE001
                        print(
                            f"callback: unexpected {type(exc).__name__}: "
                            f"{redact(exc)}",
                            flush=True,
                        )
                offset = update["update_id"] + 1
    finally:
        with STATE_LOCK:
            jobs = list(JOBS)
        stop = started + RUN_SECONDS
        for job in jobs:
            job.join(timeout=max(0, stop - time.time()))
        if any(job.is_alive() for job in jobs):
            print("Jobs still running; leaving temp files.", flush=True)
        else:
            shutil.rmtree(TMP, ignore_errors=True)
        print("Shift over.", flush=True)


if __name__ == "__main__":
    main()
