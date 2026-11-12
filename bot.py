#!/usr/bin/env python3
"""Free Telegram video compressor.

Long-polls Telegram from a GitHub Actions runner, compresses forwarded
videos with ffmpeg, and sends them back. See README.md for setup.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

TOKEN = os.environ.get("BOT_TOKEN", "")
API_BASE = os.environ.get("BOT_API_URL", "https://api.telegram.org").rstrip("/")
RUN_SECONDS = int(os.environ.get("BOT_RUN_SECONDS", "19500"))
RESERVE_SECONDS = int(os.environ.get("BOT_RESERVE_SECONDS", "300"))
MAX_INPUT_MB = int(os.environ.get("MAX_INPUT_MB", "20"))
MAX_DURATION = int(os.environ.get("MAX_DURATION", "3600"))
ALLOWED = {
    int(x)
    for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",")
    if x
}

TMP = Path(tempfile.mkdtemp(prefix="tg-compress-"))
SESSION = requests.Session()

HELP = f"""I compress videos to up to 720p H.264/AAC.

Send or forward:
- a video
- a round video message
- a video file (MP4/MOV/MKV/WebM)

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
    params = {"timeout": 50, "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = offset
    try:
        response = SESSION.get(
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
        with SESSION.get(
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
    data = json.loads(raw)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video:
        raise BotError("No video stream found")
    duration = (
        data.get("format", {}).get("duration") or video.get("duration") or 0
    )
    return {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "duration": float(duration),
        "has_audio": audio is not None,
    }


def video_filter(kind: str) -> str:
    if kind == "video_note":
        return "crop='min(iw,ih)':'min(iw,ih)',scale=480:480"
    return (
        "scale='min(1280,iw)':'min(1280,ih)':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2"
    )


def compress(src: Path, dst: Path, kind: str) -> dict:
    # shortcut: HDR is flattened to SDR without tonemapping; add zscale if needed.
    info = probe(src)
    if info["duration"] > MAX_DURATION:
        raise BotError(
            f"Video is {info['duration'] / 60:.0f} min; "
            f"limit is {MAX_DURATION // 60} min."
        )
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
        video_filter(kind),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "30" if kind == "video_note" else "28",
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


def edit_status(chat_id: int, message_id: int, text: str) -> None:
    try:
        api("editMessageText", chat_id=chat_id, message_id=message_id, text=text)
    except BotError:
        pass


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
        api(
            "sendVideo",
            chat_id=chat_id,
            video=media["file_id"],
            reply_parameters=reply,
        )


def send_result(
    chat_id: int,
    message_id: int,
    path: Path,
    kind: str,
    info: dict,
    caption: str,
    filename: str = "compressed.mp4",
) -> None:
    reply = reply_params(message_id)
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
            )
        elif kind == "document":
            api(
                "sendDocument",
                files={"document": (filename, handle, "video/mp4")},
                chat_id=chat_id,
                caption=caption,
                reply_parameters=reply,
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
            )


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

    text = message.get("text") or ""
    found = pick_media(message)
    if not found:
        if text.startswith("/start") or text.startswith("/help"):
            api(
                "sendMessage",
                chat_id=chat_id,
                text=HELP,
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
        api(
            "sendMessage",
            chat_id=chat_id,
            text="Forward a video, a round video message, or a video file.",
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
    if deadline and duration and time.time() + duration + 120 > deadline:
        api(
            "sendMessage",
            chat_id=chat_id,
            text="⏳ Queued for the next free shift. Telegram keeps it for 24 hours.",
            reply_parameters=reply_params(message_id),
        )
        raise Defer

    status = api(
        "sendMessage",
        chat_id=chat_id,
        text="⏳ Downloading…",
        reply_parameters=reply_params(message_id),
    )
    status_id = status["message_id"]
    src = None
    out = None
    try:
        src = download(media["file_id"])
        actual = probe(src)
        if deadline and time.time() + actual["duration"] + 120 > deadline:
            edit_status(
                chat_id,
                status_id,
                "⏳ Queued for the next free shift. Telegram keeps it for 24 hours.",
            )
            raise Defer
        edit_status(chat_id, status_id, "⚙️ Compressing…")
        out = TMP / f"out-{time.time_ns()}.mp4"
        info = compress(src, out, kind)
        in_mb = src.stat().st_size / 1048576
        out_mb = out.stat().st_size / 1048576
        if out_mb >= in_mb * 0.98:
            send_original(chat_id, message_id, media, kind)
            edit_status(
                chat_id,
                status_id,
                f"Already efficient ({in_mb:.1f} MB). Sent the original.",
            )
        else:
            caption = (
                f"✅ {in_mb:.1f} MB → {out_mb:.1f} MB "
                f"({100 * (1 - out_mb / in_mb):.0f}% smaller) • ≤720p H.264"
            )
            filename = "compressed.mp4"
            if kind == "document":
                original = Path(media.get("file_name") or "video.mp4")
                filename = f"{original.stem}-compressed.mp4"
            elif kind == "video_note":
                filename = "video-note-compressed.mp4"
            edit_status(chat_id, status_id, "📤 Uploading…")
            send_result(chat_id, message_id, out, kind, info, caption, filename)
            edit_status(chat_id, status_id, caption)
    except Defer:
        raise
    except BotError as exc:
        edit_status(chat_id, status_id, f"❌ {redact(exc)[:350]}")
    except Exception as exc:  # noqa: BLE001 - never kill the shift
        edit_status(chat_id, status_id, f"❌ Unexpected error: {redact(exc)[:300]}")
    finally:
        for path in (src, out):
            if path and TMP in path.parents:
                path.unlink(missing_ok=True)


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
                offset = update["update_id"] + 1
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
        print("Shift over.", flush=True)


if __name__ == "__main__":
    main()
