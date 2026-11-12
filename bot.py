#!/usr/bin/env python3
"""Free Telegram video compressor.

Long-polls Telegram from a GitHub Actions runner. The whole UX lives on
one self-updating control card per user: navigation, batch state and
per-phase percent progress are edited onto a single message, superseded
cards are deleted, and settings/queues persist to a state file that the
workflow caches between shifts. Large files travel over MTProto with the
bot's own token. See README.md.
"""

from __future__ import annotations

import asyncio
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
TELEGRAM_API_ID = int(os.environ.get("TELEGRAM_API_ID", "0") or 0)
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()
MT_UPLOAD_LIMIT = 1900 * 1024 * 1024
STATE_FILE = Path(
    os.environ.get("BOT_STATE_FILE", str(Path(tempfile.gettempdir()) / "bot-state.json"))
)
# One persistent MTProto auth session - login happens once; re-importing
# per download/shift triggers multi-hour flood waits on shared runner IPs.
MT_SESSION_FILE = Path(
    os.environ.get("BOT_MT_SESSION", str(Path(tempfile.gettempdir()) / "bot-mtproto.session"))
)
ALLOWED = {
    int(x)
    for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",")
    if x
}
ALLOWED_USERNAMES = {
    part.strip().lstrip("@").lower()
    for part in os.environ.get("ALLOWED_USERNAMES", "").replace(";", ",").split(",")
    if part.strip()
}

TMP = Path(tempfile.mkdtemp(prefix="tg-compress-"))
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
# label, x264 preset, CRF — presets trade a little size for real speed.
QUALITIES = {
    "high": ("High", "veryfast", 22),
    "balanced": ("Balanced", "superfast", 25),
    "small": ("Small", "superfast", 30),
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

Everything happens on one card: this message is updated after every
event and old bot messages are deleted, so the chat stays clean.
Settings and batch lists survive shift changes.

1. Tap 🎬 Single or 📚 Multiple.
2. Send videos (video, round video message, or video file).
3. In a batch: keep sending, then tap ▶️ Start (or send `done`).

✖️ Cancel stops after the current video — everything unfinished goes
back to the queue. Failed videos stay queued; tap ▶️ Start to retry.
⚙️ Settings tune resolution, quality and output type — current values
are marked ✅ and stick forever.

Limits: {MAX_INPUT_MB} MB input without MTProto credentials (~2000 MB
with them, then uploads too), {MAX_DURATION // 60} min per video."""


class BotError(RuntimeError):
    pass


class Defer(Exception):
    """The current shift is too short for this video; leave it for the next."""


def redact(text: object) -> str:
    value = str(text)
    return value.replace(TOKEN, "***") if TOKEN else value


def is_allowed(user_id: int | None, username: str | None) -> bool:
    if not ALLOWED and not ALLOWED_USERNAMES:
        return True
    if user_id in ALLOWED:
        return True
    if username and username.lstrip("@").lower() in ALLOWED_USERNAMES:
        return True
    return False


def whoami(user_id: int | None, username: str | None) -> str:
    return f"{user_id}" + (f" (@{username})" if username else "")


def mt_ready() -> bool:
    return bool(TELEGRAM_API_ID and TELEGRAM_API_HASH)


def mt_upload_limit_bytes() -> int:
    return MT_UPLOAD_LIMIT if mt_ready() else MAX_OUTPUT_MB * 1024 * 1024


def mt_input_limit_bytes() -> int:
    return MT_UPLOAD_LIMIT if mt_ready() else MAX_INPUT_MB * 1024 * 1024


def api(method: str, files: dict | None = None, **params):
    url = f"{API_BASE}/bot{TOKEN}/{method}"
    clean = {
        key: ("true" if value is True else "false" if value is False else value)
        for key, value in params.items()
        if value is not None
    }
    try:
        # Per-call connection: card edits can fire from inside a streaming
        # upload, so api() must never wait on a lock the upload holds.
        response = requests.post(url, data=clean, files=files, timeout=600)
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


class Throttle:
    """Decides whether a percent-heavy stream may repaint the card now."""

    def __init__(self, interval: float = 2.5):
        self.interval = interval
        self.stamp = 0.0

    def allow(self, force: bool = False) -> bool:
        now = time.time()
        if force or now - self.stamp >= self.interval:
            self.stamp = now
            return True
        return False


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
            ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",
            ".3gp", ".3g2", ".ts", ".mts", ".m2ts", ".ogv",
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


def short(text: object, limit: int = 28) -> str:
    value = str(text)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def download(
    file_id: str,
    on_percent=None,
    chat_id: int | None = None,
    message_id: int | None = None,
) -> Path:
    dest = TMP / f"in-{time.time_ns()}.bin"
    try:
        info = api("getFile", file_id=file_id)
    except BotError as exc:
        # The cloud HTTP Bot API caps getFile at 20 MB; MTProto with the
        # same bot token does not.
        if (
            "too big" not in redact(exc).lower()
            or not mt_ready()
            or not chat_id
            or not message_id
        ):
            raise
        return mtproto_download(chat_id, message_id, dest, on_percent)
    file_path = info.get("file_path", "")
    # A local Bot API server returns an absolute path and no cap.
    if file_path and os.path.isabs(file_path) and os.path.exists(file_path):
        return Path(file_path)
    if not file_path:
        raise BotError("Telegram returned no file path")
    total = int(info.get("file_size") or 0)
    received = 0
    first = True
    try:
        with requests.get(
            f"{API_BASE}/file/bot{TOKEN}/{file_path}", stream=True, timeout=600
        ) as response:
            if response.status_code != 200:
                raise BotError(f"Download failed (HTTP {response.status_code})")
            with dest.open("wb") as handle:
                for part in response.iter_content(chunk_size=256 * 1024):
                    if not part:
                        continue
                    handle.write(part)
                    received += len(part)
                    if on_percent and total:
                        on_percent(min(100, received * 100 // total))
                    elif on_percent:
                        on_percent(None)
                        first = False
    except requests.RequestException:
        raise BotError("Download failed (network error)") from None
    return dest


def _hhmmss(value: str) -> float:
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return 0.0


def mt_regions(
    total: int, branches: int = 6, min_each: int = 8 * 1024 * 1024
) -> list:
    """Plan [start, length] regions on 512 KB-aligned offsets."""
    count = max(1, min(branches, max(1, total // min_each)))
    size = max(512 * 1024, (total // count) - (total // count) % (512 * 1024))
    plan = []
    offset = 0
    for _ in range(count - 1):
        plan.append((offset, size))
        offset += size
    if offset < total:
        plan.append((offset, total - offset))
    return plan


def mtproto_download(
    chat_id: int, message_id: int, dest: Path, on_percent=None
) -> Path:
    """Parallel download via MTProto with the bot's own token; no 20 MB cap.

    Fetches the original message (modern Bot API file_ids cannot be decoded
    by Telethon) and streams several connections in parallel. One persistent
    MTProto session (cached across shifts) is reused; parallel workers run
    on copies of that auth key, so Telegram never sees extra
    ImportBotAuthorization requests (those cause multi-hour flood waits).
    """
    from telethon import TelegramClient
    from telethon import errors as telethon_errors
    from telethon.tl.types import InputPeerUser
    from telethon import utils as telethon_utils

    chunk = 512 * 1024

    async def start(index: int, seed: bytes | None = None) -> TelegramClient:
        if index == 0:
            session = str(MT_SESSION_FILE)
        else:
            session_path = TMP / f"bot-m{index}.session"
            if seed:
                session_path.write_bytes(seed)
            else:
                shutil.copyfile(MT_SESSION_FILE, session_path)
            session = str(session_path)
        client = TelegramClient(session, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await client.start(bot_token=TOKEN)
        return client

    async def pump(client, location, dc_id, offset, length, handle, state, total):
        remaining = length
        async for piece in client.iter_download(
            location, dc_id=dc_id, offset=offset, request_size=chunk, file_size=total
        ):
            if remaining <= 0:
                break
            take = piece[:remaining]
            handle.write(take)
            remaining -= len(take)
            state["done"] += len(take)
            if on_percent:
                on_percent(min(100, int(state["done"] * 100 / total)))

    async def run() -> Path:
        clients = []
        try:
            main = await start(0)
            clients.append(main)
            # Bots may address users in their own chats without an access hash.
            peer = InputPeerUser(user_id=chat_id, access_hash=0)
            message = await main.get_messages(peer, ids=message_id)
            if not message or not message.document:
                raise BotError("Could not fetch the original video from Telegram")
            document = message.document
            dc_id, location = telethon_utils.get_input_location(document)
            total = document.size

            seed = None
            if MT_SESSION_FILE.exists():
                seed = MT_SESSION_FILE.read_bytes()

            regions = mt_regions(total)
            state = {"done": 0}
            if len(regions) == 1:
                with open(dest, "wb") as handle:
                    async for piece in main.iter_download(
                        location, dc_id=dc_id, request_size=chunk, file_size=total
                    ):
                        handle.write(piece)
                        state["done"] += len(piece)
                        if on_percent:
                            on_percent(min(100, int(state["done"] * 100 / total)))
            else:
                with open(dest, "wb") as handle:
                    handle.truncate(total)
                handles = []
                try:
                    for index, (offset, length) in enumerate(regions):
                        client = main if index == 0 else await start(index, seed)
                        clients.append(client)
                        handle = open(dest, "r+b")
                        handle.seek(offset)
                        handles.append(handle)
                    await asyncio.gather(
                        *(
                            pump(client, location, dc_id, offset, length, handle, state, total)
                            for (client, (offset, length), handle) in zip(
                                clients, regions, handles
                            )
                        )
                    )
                finally:
                    for handle in handles:
                        handle.close()
            if dest.stat().st_size != total:
                raise BotError("MTProto download came back truncated; try again")
            return dest
        except telethon_errors.FloodWaitError as exc:
            raise BotError(
                f"MTProto cooldown: Telegram asked for a {exc.seconds}s wait "
                "before more logins. Try small files now, big ones shortly."
            ) from None
        finally:
            for client in clients:
                await client.disconnect()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(run())
    finally:
        loop.close()


def settings_line(settings: dict) -> str:
    resolution = (
        "Original"
        if settings.get("resolution") == "original"
        else f"\u2264{settings.get('resolution', '720')}p"
    )
    quality = QUALITIES.get(settings.get("quality", "balanced"), QUALITIES["balanced"])
    output = OUTPUT_LABELS.get(settings.get("output", "auto"), "Auto")
    return f"{resolution} \u2022 {quality[0]} \u2022 {output}"


def target_kind(kind: str, settings: dict, duration: float) -> tuple:
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
    src: Path,
    dst: Path,
    kind: str,
    settings: dict | None = None,
    duration: float = 0.0,
    on_percent=None,
) -> dict:
    settings = settings or DEFAULT_SETTINGS
    # shortcut: HDR is flattened to SDR without tonemapping; add zscale if needed.
    info = probe(src)
    if info["duration"] > MAX_DURATION:
        raise BotError(
            f"Video is {info['duration'] / 60:.0f} min; limit is {MAX_DURATION // 60} min."
        )
    quality = QUALITIES.get(settings.get("quality", "balanced"), QUALITIES["balanced"])
    crf = quality[2]
    if kind == "video_note":
        crf = min(51, crf + 2)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
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
        quality[1],
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
    err_file = TMP / f"{dst.name}.err"
    timed_out = False
    process = None
    watchdog = None

    def kill_on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        if process:
            process.kill()

    try:
        with err_file.open("wb") as errors:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=errors, text=True
            )
            watchdog = threading.Timer(MAX_DURATION, kill_on_timeout)
            watchdog.start()
            for line in process.stdout:
                if not on_percent or duration <= 0:
                    continue
                if line.startswith("out_time="):
                    seconds = _hhmmss(line.split("=", 1)[1].strip())
                    on_percent(max(1, min(99, int(seconds * 100 / duration))))
                elif line.startswith("progress=end"):
                    on_percent(100)
            process.stdout.close()
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                raise BotError("Compression took too long to finish") from None
            if timed_out:
                raise BotError("Compression timed out")
            if process.returncode != 0:
                detail = err_file.read_text(errors="replace").strip().splitlines()
                raise BotError(f"ffmpeg failed: {detail[-1] if detail else 'unknown'}")
    except OSError as exc:
        raise BotError(f"ffmpeg is not installed: {exc}") from None
    finally:
        if watchdog:
            watchdog.cancel()
        err_file.unlink(missing_ok=True)
    return probe(dst)


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


def cache_item(item: dict, queue: list, on_percent=None) -> str | None:
    """Eagerly download a queued video; evict the oldest file if needed."""
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
        path = download(
            item["media"]["file_id"],
            on_percent,
            chat_id=item.get("chat_id"),
            message_id=item.get("message_id"),
        )
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


class UploadMeter:
    """Streams the upload body and reports real sent-byte percent."""

    def __init__(self, handle, total: int, on_percent):
        self.handle = handle
        self.total = total
        self.on_percent = on_percent
        self.sent = 0

    def read(self, size=-1):
        data = self.handle.read(size)
        if data and self.total:
            self.sent += len(data)
            if self.on_percent:
                self.on_percent(min(100, self.sent * 100 // self.total))
        return data

    def tell(self):
        return self.handle.tell()

    def seek(self, *args, **kwargs):
        return self.handle.seek(*args, **kwargs)

    def __len__(self):
        return self.total


def send_original(chat_id: int, message_id: int, media: dict, kind: str) -> None:
    reply = reply_params(message_id)
    if kind == "video_note":
        api(
            "sendVideoNote",
            chat_id=chat_id,
            video_note=media["file_id"],
            reply_parameters=reply,
        )
    elif kind == "document":
        api(
            "sendDocument",
            chat_id=chat_id,
            document=media["file_id"],
            reply_parameters=reply,
        )
    else:
        api("sendVideo", chat_id=chat_id, video=media["file_id"], reply_parameters=reply)


def send_result(
    chat_id: int,
    message_id: int,
    path: Path,
    kind: str,
    info: dict,
    caption: str,
    filename: str = "compressed.mp4",
    on_percent=None,
) -> None:
    if path.stat().st_size > mt_upload_limit_bytes():
        mtproto_upload(
            chat_id, message_id, path, kind, caption, filename, info, on_percent
        )
        return
    reply = reply_params(message_id)
    duration = max(1, int(info["duration"]))
    with path.open("rb") as handle:
        body = UploadMeter(handle, path.stat().st_size, on_percent) if on_percent else handle
        if kind == "video_note":
            api(
                "sendVideoNote",
                files={"video_note": (filename, body, "video/mp4")},
                chat_id=chat_id,
                duration=duration,
                length=int(info["width"]),
                reply_parameters=reply,
            )
        elif kind == "document":
            api(
                "sendDocument",
                files={"document": (filename, body, "video/mp4")},
                chat_id=chat_id,
                caption=caption,
                reply_parameters=reply,
            )
        else:
            api(
                "sendVideo",
                files={"video": (filename, body, "video/mp4")},
                chat_id=chat_id,
                caption=caption,
                supports_streaming="true",
                width=int(info["width"]),
                height=int(info["height"]),
                duration=duration,
                reply_parameters=reply,
            )


def mtproto_upload(
    chat_id: int,
    message_id: int,
    path: Path,
    kind: str,
    caption: str,
    filename: str,
    info: dict,
    on_percent=None,
) -> None:
    """Upload via MTProto with the bot's own token; no 50 MB cap."""
    from telethon import TelegramClient, Button
    from telethon.tl.types import (
        DocumentAttributeFilename,
        DocumentAttributeVideo,
    )

    async def run() -> None:
        client = TelegramClient(
            str(TMP / "bot.session"), TELEGRAM_API_ID, TELEGRAM_API_HASH
        )
        await client.start(bot_token=TOKEN)
        try:
            duration = max(1, int(info["duration"]))
            width = int(info["width"])
            height = int(info["height"])
            if kind == "video_note":
                attributes = [
                    DocumentAttributeVideo(duration, width, height, round_message=True),
                    DocumentAttributeFilename(filename),
                ]
            elif kind == "video":
                attributes = [
                    DocumentAttributeVideo(
                        duration, width, height, supports_streaming=True
                    ),
                    DocumentAttributeFilename(filename),
                ]
            else:
                attributes = [DocumentAttributeFilename(filename)]

            def callback(current: int, total: int) -> None:
                if on_percent and total:
                    on_percent(min(100, int(current * 100 / total)))

            await client.send_file(
                chat_id,
                str(path),
                caption=caption,
                attributes=attributes,
                force_document=kind == "document",
                supports_streaming=kind == "video",
                reply_to=message_id,
                progress_callback=callback,
            )
        finally:
            await client.disconnect()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()


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


def get_state(user_id: int, chat_id: int | None = None) -> dict:
    with STATE_LOCK:
        state = USERS.get(user_id)
        if state is None:
            state = default_state(user_id, chat_id)
            USERS[user_id] = state
        if chat_id is not None:
            state["chat_id"] = chat_id
        return state


def _public_item(item: dict) -> dict:
    keep = (
        "media",
        "kind",
        "chat_id",
        "message_id",
        "size",
        "name",
        "filename",
        "duration",
    )
    return {key: item[key] for key in keep if key in item}


def save_state() -> None:
    try:
        payload = {}
        for uid, state in USERS.items():
            payload[str(uid)] = {
                "user_id": state["user_id"],
                "chat_id": state.get("chat_id"),
                "mode": state["mode"],
                "view": state["view"],
                "settings": state["settings"],
                "summary": state.get("summary", ""),
                "note": state.get("note", ""),
                "queue": [_public_item(i) for i in state.get("queue", [])],
            }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(STATE_FILE)
    except (OSError, TypeError, ValueError):
        pass


def load_state() -> None:
    try:
        payload = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return
    USERS.clear()
    for uid, data in payload.items():
        try:
            user_id = int(data.get("user_id", uid))
        except (TypeError, ValueError):
            continue
        state = default_state(user_id, data.get("chat_id"))
        if data.get("mode") in ("single", "batch"):
            state["mode"] = data["mode"]
        if data.get("view") in ("main", "settings", "res", "quality", "output", "help"):
            state["view"] = data["view"]
        for key in ("resolution", "quality", "output"):
            if key in data.get("settings", {}):
                state["settings"][key] = data["settings"][key]
        state["summary"] = data.get("summary", "")
        state["note"] = data.get("note", "")
        for item in data.get("queue", []):
            if item.get("media") and item.get("message_id"):
                item.setdefault("path", None)
                item.setdefault("status", "")
                item["duration"] = float(item.get("duration") or 0)
                restored = _public_item(item)
                restored["path"] = None
                restored["status"] = ""
                restored["delivered"] = False
                state["queue"].append(restored)
        USERS[user_id] = state


def settings_block(state: dict) -> str:
    settings = state["settings"]
    resolution = (
        "Original"
        if settings["resolution"] == "original"
        else f"≤{settings['resolution']}p"
    )
    quality = QUALITIES.get(settings["quality"], QUALITIES["balanced"])
    output = OUTPUT_LABELS.get(settings["output"], "Auto")
    return (
        f"⚙️ Settings\n\nResolution: {resolution} (never upscales)\n"
        f"Quality: {quality[0]} ({quality[1]}, CRF {quality[2]})\n"
        f"Output: {output}\n\nValues are marked ✅ and persist between shifts."
    )


def _views_text(state: dict) -> str:
    view = state["view"]
    settings = state["settings"]
    if view == "res":
        return "📐 Resolution\n\nLong side cap. Original keeps the source size."
    if view == "quality":
        return "🎚 Quality — faster preset = faster encodes, bigger file."
    if view == "output":
        return "📦 Output\n\nAuto sends the same kind of message you sent."
    if view == "help":
        return HELP
    return settings_block(state)


def _rows(items: list) -> list:
    lines = []
    for number, item in enumerate(items, 1):
        size = item.get("size", 0) / 1048576
        status = item.get("status") or "queued"
        lines.append(
            f"{number}. {short(item.get('name', 'video'), 24)} — {size:.1f} MB · {status}"
        )
    return lines


def card_text(state: dict) -> str:
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
        lines.append(_views_text(state))
    return "\n".join(lines)


def card_kb(state: dict) -> list:
    if state["processing"]:
        return [[btn("✖️ Cancel", "multi:cancel", "danger")]]
    view = state["view"]
    if view == "res":
        return choice_rows(
            RESOLUTIONS, state["settings"]["resolution"], "set:res:"
        )
    if view == "quality":
        options = [
            (key, f"{value[0]} (CRF {value[2]})") for key, value in QUALITIES.items()
        ]
        return choice_rows(options, state["settings"]["quality"], "set:q:")
    if view == "output":
        return choice_rows(OUTPUTS, state["settings"]["output"], "set:out:")
    if view == "help":
        return [[btn("⬅️ Menu", "menu:main")]]
    if view == "settings":
        settings = state["settings"]
        resolution = (
            "Original"
            if settings["resolution"] == "original"
            else f"{settings['resolution']}p"
        )
        quality = QUALITIES.get(settings["quality"], QUALITIES["balanced"])
        output = OUTPUT_LABELS.get(settings["output"], "Auto")
        return [
            [btn(f"📐 Resolution · {resolution}", "setmenu:res", "primary")],
            [btn(f"🎚 Quality · {quality[0]}", "setmenu:quality", "primary")],
            [btn(f"📦 Output · {output}", "setmenu:output", "primary")],
            [btn("⬅️ Menu", "menu:main")],
        ]
    rows = [
        [
            btn(
                "🎬 Single" if state["mode"] != "batch" else "🎬 Single",
                "menu:single",
                "success" if state["mode"] == "single" else "primary",
            ),
            btn(
                f"📚 Batch ({len(state['queue'])})"
                if state["queue"]
                else "📚 Batch",
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


def choice_rows(options: list, current: str, prefix: str) -> list:
    items = [
        btn(
            ("✅ " if key == current else "") + label,
            f"{prefix}{key}",
            "success" if key == current else None,
        )
        for key, label in options
    ]
    rows = [items[i : i + 3] for i in range(0, len(items), 3)]
    rows.append([btn("⬅️ Settings", "menu:settings")])
    return rows


def card_refresh(state: dict) -> None:
    """Edit THE card in place; send a fresh one when needed; delete the rest.

    Repositioning is driven by events: handles/callbacks clear state["menu"],
    which forces a fresh send at the bottom of the chat and deletes the old
    card, so the card is always the latest message after any user action.
    """
    chat_id = state.get("chat_id")
    if not chat_id:
        return
    body, keys = card_text(state), card_kb(state)
    menu = state.get("menu")
    if menu and menu[0] == chat_id:
        try:
            api(
                "editMessageText",
                chat_id=chat_id,
                message_id=menu[1],
                text=body,
                reply_markup=markup(keys),
            )
        except BotError as exc:
            if "not modified" not in redact(exc).lower():
                menu = None  # stale/deleted/failed edit → send a fresh card
    if not menu or menu[0] != chat_id:
        sent = api(
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
            api("deleteMessage", chat_id=chat_id, message_id=old_id)
        except BotError:
            pass
    state["cards"] = [keep_id]
    save_state()


def make_report(state: dict, item: dict):
    """report(status, percent) → live row + throttled card repaint."""
    gate = Throttle()

    def report(status: str, percent: int | None = None):
        text = status if percent is None else f"{status} {percent}%"
        if item.get("status") == text:
            return
        item["status"] = text
        if gate.allow(force=percent is None):
            card_refresh(state)

    return report


def compress_one(
    chat_id: int,
    reply_to: int,
    media: dict,
    kind: str,
    settings: dict,
    deadline: float | None = None,
    path: Path | None = None,
    filename: str = "compressed.mp4",
    report=None,
) -> dict:
    def say(phase: str, percent: int | None = None):
        if report:
            report(phase, percent)

    say("⏳ downloading")
    src = path
    out = None
    try:
        if src is None or not src.exists():
            src = download(
                media["file_id"],
                lambda pct: say("⏳ downloading", pct),
                chat_id=chat_id,
                message_id=reply_to,
            )
        actual = probe(src)
        if deadline and time.time() + actual["duration"] + 120 > deadline:
            say("⏳ shift too short — retried next shift")
            raise Defer
        out_kind, fallback = target_kind(kind, settings, actual["duration"])
        say("⚙️ compressing")
        out = TMP / f"out-{time.time_ns()}.mp4"
        info = compress(
            src,
            out,
            out_kind,
            settings,
            duration=actual["duration"],
            on_percent=lambda pct: say("⚙️ compressing", pct),
        )
        in_mb = src.stat().st_size / 1048576
        out_mb = out.stat().st_size / 1048576
        if out.stat().st_size > mt_upload_limit_bytes():
            limit = "~2000 MB" if mt_ready() else f"{MAX_OUTPUT_MB} MB"
            raise BotError(
                f"Compressed file is {out_mb:.1f} MB; the upload limit is "
                f"{limit}. Choose a lower resolution or quality."
            )
        if out_mb >= in_mb * 0.98:
            try:
                send_original(chat_id, reply_to, media, out_kind)
            except BotError:
                if out_kind == kind:
                    raise
                send_original(chat_id, reply_to, media, kind)
            say(f"✅ sent original ({in_mb:.1f} MB)")
        else:
            saved = 100 * (1 - out_mb / in_mb)
            caption = (
                f"✅ {in_mb:.1f} MB → {out_mb:.1f} MB ({saved:.0f}% smaller) • "
                f"H.264 • {settings_line(settings)}"
            )
            if fallback:
                caption += (
                    "\nRound video is limited to 60 s, so this was sent as a video."
                )
            say("📤 uploading")
            send_result(
                chat_id,
                reply_to,
                out,
                out_kind,
                info,
                caption,
                filename,
                on_percent=lambda pct: say("📤 uploading", pct),
            )
            say(f"✅ {in_mb:.1f} → {out_mb:.1f} MB ({saved:.0f}%)")
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
        say(f"❌ {redact(exc)[:120]}")
        return {"error": redact(exc)}
    except Exception as exc:  # noqa: BLE001 - never kill the shift
        say(f"❌ unexpected error")
        print(f"compress_one: {type(exc).__name__}: {redact(exc)}", flush=True)
        return {"error": redact(exc)}
    finally:
        for path in (src, out):
            if path and TMP in path.parents:
                path.unlink(missing_ok=True)


def start_batch(state: dict, deadline: float | None = None) -> str | None:
    if state["processing"]:
        return "A batch is already running."
    if not state["queue"]:
        return "Queue is empty. Send videos first."
    if deadline and time.time() + 120 > deadline:
        return "The shift is ending. Start after the next shift."
    state["working"] = [dict(item, status="queued") for item in state["queue"]]
    state["queue"] = []
    state["processing"] = True
    state["cancel"] = False
    state["view"] = "main"
    state["summary"] = ""
    thread = threading.Thread(
        target=process_batch, args=(state["user_id"], dict(state["settings"]), deadline), daemon=True
    )
    with STATE_LOCK:
        JOBS.add(thread)
    thread.start()
    save_state()
    return None


def process_batch(user_id: int, settings: dict, deadline: float | None = None) -> None:
    state = get_state(user_id)
    items = state.get("working", [])
    total = len(items)
    state_in = state_out = 0.0
    failed = cancelled = deferred = False
    index = 0
    try:
        for index, item in enumerate(items):
            if state.get("cancel"):
                cancelled = True
                break
            duration = float(item.get("duration") or 0)
            if deadline and time.time() + duration + 120 > deadline:
                deferred = True
                item["status"] = "⏳ shift ended"
                break
            try:
                result = compress_one(
                    item["chat_id"],
                    item["message_id"],
                    item["media"],
                    item["kind"],
                    settings,
                    deadline=deadline,
                    path=item.get("path"),
                    filename=item["filename"],
                    report=make_report(state, item),
                )
            except Defer:
                item["path"] = None
                deferred = True
                break
            item["path"] = None
            if "error" in result:
                failed = True
                item["status"] = f"❌ {result['error']}"[:120]
            else:
                item["delivered"] = True
                state_in += result["in"]
                state_out += result["out"]
            card_refresh(state)
    except Exception as exc:  # noqa: BLE001 - keep the rest queued
        print(f"batch: {redact(exc)}", flush=True)
    delivered = sum(1 for i in items if i.get("delivered"))
    errors = [i for i in items if (i.get("status") or "").startswith("❌")]
    kept = [i for i in items if not i.get("delivered")]
    with STATE_LOCK:
        state["working"] = []
        state["queue"] = kept + state["queue"]
        state["processing"] = False
        state["cancel"] = False
        state["view"] = "main"
    pieces = []
    if cancelled:
        pieces.append(f"⏹ Cancelled — {delivered}/{total} delivered.")
    elif deferred:
        pieces.append(f"⏳ Shift ending — {delivered}/{total} delivered.")
    elif failed and not delivered:
        pieces.append(f"❌ Batch failed — {total} error(s).")
    else:
        pieces.append(f"✅ Delivered {delivered}/{total}.")
    if state_in:
        saved = 100 * (1 - state_out / state_in)
        pieces.append(f"{state_in:.1f} MB → {state_out:.1f} MB ({saved:.0f}% smaller).")
    if errors:
        pieces.append(f"{len(errors)} failed — tap ▶️ Start to retry.")
    if deferred:
        pieces.append("Send remaining videos again after the next shift.")
    state["summary"] = " ".join(pieces)
    try:
        card_refresh(state)
    except Exception as exc:  # noqa: BLE001
        print(f"batch summary: {redact(exc)}", flush=True)
    with STATE_LOCK:
        JOBS.discard(threading.current_thread())


def handle_callback(cq: dict, deadline: float | None = None) -> None:
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
            if not is_allowed(user_id, username):
                answer(f"Private bot. Your ID: {whoami(user_id, username)}", True)
                return
        state = get_state(user_id, chat_id)
        state["menu"] = None  # reposition under the latest activity
        settings = state["settings"]
        if data == "menu:main":
            state["view"] = "main"
            state["mode"] = "single"
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
        elif data == "setmenu:res":
            state["view"] = "res"
        elif data == "setmenu:quality":
            state["view"] = "quality"
        elif data == "setmenu:output":
            state["view"] = "output"
        elif data.startswith("set:res:"):
            value = data.split(":", 2)[2]
            if value not in RESOLUTION_LABELS:
                answer("Unknown option.", True)
                return
            settings["resolution"] = value
            state["view"] = "settings"
        elif data.startswith("set:q:"):
            value = data.split(":", 2)[2]
            if value not in QUALITIES:
                answer("Unknown option.", True)
                return
            settings["quality"] = value
            state["view"] = "settings"
        elif data.startswith("set:out:"):
            value = data.split(":", 2)[2]
            if value not in OUTPUT_LABELS:
                answer("Unknown option.", True)
                return
            settings["output"] = value
            state["view"] = "settings"
        elif data == "multi:done":
            error = start_batch(state, deadline)
            answer(error or "Starting…", alert=bool(error))
            card_refresh(state)
            return
        elif data == "multi:clear":
            if state["processing"]:
                answer("Wait for the batch to finish.", True)
                return
            for item in state["queue"]:
                safe_unlink(item.get("path"))
            state["queue"].clear()
            state["summary"] = ""
            state["note"] = ""
        elif data == "multi:cancel":
            if state["processing"]:
                state["cancel"] = True
                answer("Stopping after the current video.")
            else:
                answer("Nothing is running.")
        else:
            answer("This menu is outdated. Send /start.", True)
            return
        card_refresh(state)
    finally:
        answer()


def handle(message: dict, deadline: float | None = None) -> None:
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]
    user = message.get("from") or {}
    user_id = user.get("id")
    username = user.get("username") or ""

    if ALLOWED and user_id not in ALLOWED:
        if not is_allowed(user_id, username):
            api(
                "sendMessage",
                chat_id=chat_id,
                text=(
                    "This bot is private.\n"
                    f"Your user ID: {whoami(user_id, username)}\n"
                    "Ask the owner to add it to ALLOWED_USER_IDS or ALLOWED_USERNAMES."
                ),
                reply_parameters=reply_params(message_id),
            )
            return

    state = get_state(user_id, chat_id)
    state["menu"] = None  # reposition: the card is always the latest message
    text = (message.get("text") or "").strip()

    if text.startswith("/start"):
        state["mode"] = "single"
        state["view"] = "main"
        state["note"] = ""
        state["menu"] = None  # fresh card
        card_refresh(state)
        return
    if text.startswith("/help"):
        state["view"] = "help"
        card_refresh(state)
        return
    if text.startswith("/settings"):
        state["view"] = "settings"
        card_refresh(state)
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
            state["note"] = "Stopping after the current video."
        else:
            state["mode"] = "single"
            state["note"] = ""
        card_refresh(state)
        return
    if text.lower() in ("done", "/done", "✅ done"):
        error = start_batch(state, deadline)
        state["note"] = error or ""
        card_refresh(state)
        return

    found = pick_media(message)
    if not found:
        api(
            "sendMessage",
            chat_id=chat_id,
            text="Send a video, a round video message, or a video file.",
            reply_parameters=reply_params(message_id),
        )
        return

    media, kind = found
    size = int(media.get("file_size") or 0)
    duration = float(media.get("duration") or 0)
    if size > mt_input_limit_bytes():
        if mt_ready():
            reason = (
                f"This video is {size / 1048576:.0f} MB; the MTProto bot cap is "
                "~2000 MB. Split it and send the parts."
            )
        else:
            reason = (
                f"This video is {size / 1048576:.1f} MB. Telegram's cloud Bot API "
                f"caps downloads at {MAX_INPUT_MB} MB. Set the TELEGRAM_API_ID and "
                "TELEGRAM_API_HASH secrets to unlock ~2 GB, or send a shorter clip."
            )
        api(
            "sendMessage",
            chat_id=chat_id,
            text=reason,
            reply_parameters=reply_params(message_id),
        )
        return
    if state["processing"]:
        api(
            "sendMessage",
            chat_id=chat_id,
            text="⏳ The batch is still running — videos after ✖️ Cancel start a new batch.",
            reply_parameters=reply_params(message_id),
        )
        return

    item = {
        "media": media,
        "kind": kind,
        "chat_id": chat_id,
        "message_id": message_id,
        "size": size,
        "name": short(media_name(media, kind)),
        "filename": filename_for(media, kind),
        "path": None,
        "duration": duration,
        "status": "",
        "delivered": False,
    }

    if state["mode"] == "batch":
        report = make_report(state, item)
        report("⏳ downloading")
        state["queue"].append(item)
        card_refresh(state)
        others = [q for q in state["queue"] if q is not item]
        error = cache_item(item, others, lambda pct: report("⏳ downloading", pct))
        if error:
            safe_unlink(item.get("path"))
            state["queue"].remove(item)
            state["note"] = f"❌ {error}"
            card_refresh(state)
            return
        item["status"] = ""
        state["note"] = ""
        card_refresh(state)
        return

    if deadline and duration and time.time() + duration + 120 > deadline:
        state["note"] = "⏳ Not enough time left in this shift; queued for the next."
        card_refresh(state)
        raise Defer

    state["working"] = [item]
    state["processing"] = True
    card_refresh(state)
    try:
        result = compress_one(
            chat_id,
            message_id,
            media,
            kind,
            state["settings"],
            deadline=deadline,
            report=make_report(state, item),
        )
        if "error" in result:
            item["status"] = f"❌ {result['error']}"[:120]
    except Defer:
        state["working"] = []
        state["processing"] = False
        state["note"] = "⏳ Shift too short — send it again after the next shift starts."
        card_refresh(state)
        raise
    finally:
        state["processing"] = False
    state["working"] = []
    if "error" not in result:
        state["note"] = ""
    else:
        state["note"] = f"{result['error']}"[:180]
    card_refresh(state)


def main() -> None:
    if not TOKEN:
        # A missing token must not fail the scheduled shift; GitHub emails on
        # every failed run and cron keeps retrying every 5 hours.
        print(
            "BOT_TOKEN is not set; shift skipped. "
            "Add the BOT_TOKEN secret and run the workflow again.",
            flush=True,
        )
        return
    if not ALLOWED and not ALLOWED_USERNAMES:
        print(
            "WARNING: ALLOWED_USER_IDS and ALLOWED_USERNAMES are empty; "
            "anyone can use this bot.",
            flush=True,
        )
    load_state()
    print(
        f"Bot started. API={API_BASE} shift={RUN_SECONDS}s "
        f"reserve={RESERVE_SECONDS}s mtproto={mt_ready()}",
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
                            f"callback: unexpected {type(exc).__name__}: {redact(exc)}",
                            flush=True,
                        )
                offset = update["update_id"] + 1
    finally:
        save_state()
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
