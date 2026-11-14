"""Video inspection, encoding options, and the ffmpeg pipeline."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

from .errors import BotError
from .settings import (
    AUDIO_OPTIONS,
    CODECS,
    DEFAULT_SETTINGS,
    OUTPUT_OPTIONS,
    QUALITIES,
    RESOLUTIONS,
    TARGET_SIZES,
    canonical_audio,
    canonical_target_size,
)

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

# VBV bounds for CRF mode: (maxrate, bufsize)
VBV_BOUNDS = {
    "2160": ("20M", "40M"),
    "1440": ("12M", "24M"),
    "1080": ("6M", "12M"),
    "720": ("3500k", "7000k"),
    "480": ("1800k", "3600k"),
    "360": ("1000k", "2000k"),
    "240": ("500k", "1000k"),
    "original": ("8M", "16M"),
}

CODEC_LABELS = dict(CODECS)
AUDIO_LABELS = dict(AUDIO_OPTIONS)
TARGET_SIZE_LABELS = dict(TARGET_SIZES)
OUTPUTS = OUTPUT_OPTIONS
OUTPUT_LABELS = dict(OUTPUT_OPTIONS)
NOTE_MAX_SECONDS = 60

VIDEO_SUFFIXES = {
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


class MediaInfo(TypedDict, total=False):
    width: int
    height: int
    duration: float
    has_audio: bool
    codec_name: str
    video_stream_index: int
    pix_fmt: str
    color_space: str
    color_transfer: str
    color_primaries: str
    rotation: int
    fps: float
    bit_rate: int
    audio_codec: str
    audio_bitrate: int


# Standalone direct media.compress(job_id=...) cancellation registry. Pipeline
# cancellation uses its per-conversation registry; this remains for callers that
# invoke the media layer directly.
# Active process cancellation registry
ACTIVE_PROCESSES: dict[str, subprocess.Popen] = {}
CANCELLED_JOBS: set[str] = set()
_PROCESS_LOCK = threading.Lock()


def register_process(job_id: str, proc: subprocess.Popen) -> None:
    with _PROCESS_LOCK:
        ACTIVE_PROCESSES[job_id] = proc


def unregister_process(job_id: str) -> None:
    with _PROCESS_LOCK:
        ACTIVE_PROCESSES.pop(job_id, None)
        CANCELLED_JOBS.discard(job_id)


def cancel_process(job_id: str) -> bool:
    with _PROCESS_LOCK:
        CANCELLED_JOBS.add(job_id)
        proc = ACTIVE_PROCESSES.get(job_id)
        if proc:
            try:
                proc.kill()
            except OSError:
                pass
            return True
        return False


def cancel_all_processes() -> int:
    with _PROCESS_LOCK:
        count = 0
        for job_id, proc in list(ACTIVE_PROCESSES.items()):
            CANCELLED_JOBS.add(job_id)
            try:
                proc.kill()
                count += 1
            except OSError:
                pass
        return count


def is_job_cancelled(job_id: str | None) -> bool:
    if not job_id:
        return False
    with _PROCESS_LOCK:
        return job_id in CANCELLED_JOBS


def parse_fps(val: str | None) -> float:
    if not val:
        return 0.0
    try:
        if "/" in val:
            num, den = val.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f != 0 else 0.0
        return float(val)
    except (ValueError, TypeError, ZeroDivisionError):
        return 0.0


def extract_rotation(video: dict, fmt: dict | None = None) -> int:
    tags = {str(k).lower(): v for k, v in (video.get("tags") or {}).items()}
    if "rotate" in tags:
        try:
            return int(float(tags["rotate"])) % 360
        except (ValueError, TypeError):
            pass

    for side in video.get("side_data_list", []):
        stype = str(side.get("side_data_type", "")).lower()
        if (
            "display matrix" in stype or "displaymatrix" in stype
        ) and "rotation" in side:
            try:
                return int(float(side["rotation"])) % 360
            except (ValueError, TypeError):
                pass

    if fmt:
        ftags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
        if "rotate" in ftags:
            try:
                return int(float(ftags["rotate"])) % 360
            except (ValueError, TypeError):
                pass

    return 0


def is_cover_art_stream(stream: dict) -> bool:
    if stream.get("codec_type") != "video":
        return False
    disposition = stream.get("disposition") or {}
    if disposition.get("attached_pic") == 1 or stream.get("attached_pic") == 1:
        return True
    return stream.get("codec_name") == "mjpeg"


def is_hdr(info: MediaInfo | dict | None) -> bool:
    if not info:
        return False
    pix_fmt = str(info.get("pix_fmt", "")).lower()
    color_space = str(info.get("color_space", "")).lower()
    color_transfer = str(info.get("color_transfer", "")).lower()
    color_primaries = str(info.get("color_primaries", "")).lower()
    if any(k in pix_fmt for k in ("10le", "10be", "12le", "12be", "p10", "p12")):
        return True
    if any(k in color_space for k in ("bt2020", "2020")):
        return True
    if any(k in color_transfer for k in ("smpte2084", "arib-std-b67", "2084")):
        return True
    return any(k in color_primaries for k in ("bt2020", "2020"))


def probe(path: Path) -> MediaInfo:
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
    video_index, video = next(
        (
            (index, stream)
            for index, stream in enumerate(streams)
            if stream.get("codec_type") == "video"
            and not is_cover_art_stream(stream)
        ),
        (None, None),
    )
    if video is None:
        raise BotError("No video stream found")

    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    fmt = data.get("format", {})
    duration = (
        fmt.get("duration")
        or video.get("duration")
        or (audio.get("duration") if audio else None)
        or 0
    )
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 0.0

    raw_width = int(video.get("width") or 0)
    raw_height = int(video.get("height") or 0)
    rotation = extract_rotation(video, fmt)

    if rotation in (90, 270):
        width, height = raw_height, raw_width
    else:
        width, height = raw_width, raw_height

    fps = parse_fps(video.get("r_frame_rate")) or parse_fps(video.get("avg_frame_rate"))

    bit_rate = 0
    raw_bitrate = video.get("bit_rate") or fmt.get("bit_rate")
    if raw_bitrate:
        try:
            bit_rate = int(raw_bitrate)
        except (ValueError, TypeError):
            bit_rate = 0

    audio_codec = ""
    audio_bitrate = 0
    if audio:
        audio_codec = str(audio.get("codec_name", "")).lower()
        raw_a_br = audio.get("bit_rate")
        if raw_a_br:
            try:
                audio_bitrate = int(raw_a_br)
            except (ValueError, TypeError):
                audio_bitrate = 0

    return {
        "width": width,
        "height": height,
        "duration": duration,
        "has_audio": audio is not None,
        "video_stream_index": video_index,
        "codec_name": str(video.get("codec_name", "")).lower(),
        "pix_fmt": str(video.get("pix_fmt", "")).lower(),
        "color_space": str(video.get("color_space", "")).lower(),
        "color_transfer": str(video.get("color_transfer", "")).lower(),
        "color_primaries": str(video.get("color_primaries", "")).lower(),
        "rotation": rotation,
        "fps": fps,
        "bit_rate": bit_rate,
        "audio_codec": audio_codec,
        "audio_bitrate": audio_bitrate,
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
        if mime.startswith("video/") or Path(name).suffix in VIDEO_SUFFIXES:
            return document, "document"
    return None


def settings_line(settings: dict) -> str:
    resolution = (
        "Original"
        if settings.get("resolution") == "original"
        else f"\u2264{settings.get('resolution', '720')}p"
    )
    quality_key = settings.get("quality", "balanced")
    legacy_target = canonical_target_size(str(quality_key))
    mode = settings.get("mode")
    if mode == "size" or (mode != "crf" and settings.get("target_size")):
        target = canonical_target_size(str(settings.get("target_size", "20mb")))
        quality_str = f"Target {TARGET_SIZE_LABELS.get(target, target.upper())}"
    elif "crf" in settings:
        quality_str = f"CRF {settings['crf']}"
    elif quality_key in QUALITIES:
        quality_str = QUALITIES[quality_key][0]
    elif legacy_target in TARGET_SIZE_LABELS:
        quality_str = TARGET_SIZE_LABELS[legacy_target]
    else:
        quality_str = str(quality_key).capitalize()
    output = OUTPUT_LABELS.get(settings.get("output", "auto"), "Auto")
    return f"{resolution} \u2022 {quality_str} \u2022 {output}"


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


def build_video_filter(
    kind: str,
    settings: dict | None = None,
    info: MediaInfo | dict | None = None,
) -> str:
    settings = settings or DEFAULT_SETTINGS
    resolution = settings.get("resolution", "720")
    filters: list[str] = []

    # 1. HDR tone-mapping
    if is_hdr(info) and str(settings.get("tonemap", "auto")).lower() != "off":
        filters.append("tonemap=mobius,format=yuv420p")

    # 2. Crop / Scale
    if kind == "video_note":
        filters.append("crop='min(iw,ih)':'min(iw,ih)'")
        cap = 640 if resolution == "original" else min(int(resolution), 640)
        filters.append(
            f"scale='min({cap},iw)':'min({cap},ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2"
        )
    elif resolution == "original":
        filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    else:
        cap = RESOLUTION_CAPS.get(resolution, 1280)
        filters.append(
            f"scale='min({cap},iw)':'min({cap},ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2"
        )

    # 3. Framerate capping
    fps_val = str(settings.get("fps", settings.get("fps_cap", "orig"))).lower()
    if fps_val in ("30", "24"):
        filters.append(f"fps=fps=min({fps_val}\\,source_fps)")
    elif fps_val not in ("orig", "original", "", "none"):
        try:
            fps_int = int(fps_val)
            filters.append(f"fps=fps=min({fps_int}\\,source_fps)")
        except ValueError:
            pass

    return ",".join(filters)


video_filter = build_video_filter


def build_audio_args(
    settings: dict | None = None,
    info: MediaInfo | dict | None = None,
) -> list[str]:
    settings = settings or DEFAULT_SETTINGS
    has_audio = info.get("has_audio", True) if info else True
    if not has_audio:
        return ["-an"]

    audio_opt = canonical_audio(str(settings.get("audio", "auto")))
    if audio_opt == "mute":
        return ["-an"]
    if audio_opt == "copy":
        return ["-c:a", "copy"]
    if audio_opt == "aac_96k":
        return ["-c:a", "aac", "-b:a", "96k", "-ac", "2"]
    if audio_opt == "opus_64k":
        return ["-c:a", "libopus", "-b:a", "64k"]
    if audio_opt == "mono":
        return ["-c:a", "aac", "-b:a", "48k", "-ac", "1"]
    if audio_opt == "auto":
        if (
            info
            and str(info.get("audio_codec", "")).lower() == "aac"
            and 0 < info.get("audio_bitrate", 0) <= 128_000
        ):
            return ["-c:a", "copy"]
        return ["-c:a", "aac", "-b:a", "96k", "-ac", "2"]

    return ["-c:a", "aac", "-b:a", "96k", "-ac", "2"]


def parse_target_size(val: str | float, source_bytes: int = 0) -> int | None:
    if isinstance(val, (int, float)):
        return int(val)
    if not isinstance(val, str):
        return None
    val_str = canonical_target_size(val)
    if val_str.endswith("%"):
        try:
            pct = float(val_str[:-1]) / 100.0
            return int(source_bytes * pct) if source_bytes > 0 else None
        except ValueError:
            return None
    if val_str.endswith("mb"):
        try:
            mb = float(val_str[:-2])
            return int(mb * 1024 * 1024)
        except ValueError:
            return None
    if val_str.endswith(("k", "kb")):
        val_clean = val_str.rstrip("b").rstrip("k")
        try:
            return int(float(val_clean) * 1024)
        except ValueError:
            return None
    try:
        return int(val_str)
    except ValueError:
        return None


def calculate_target_bitrate(
    target_bytes: float,
    duration: float,
    audio_bitrate: int = 96_000,
) -> int:
    if duration <= 0:
        return 500_000
    available_bits = target_bytes * 8 * 0.98
    total_bitrate = available_bits / duration
    video_bitrate = total_bitrate - audio_bitrate
    return max(32_000, int(video_bitrate))


def get_target_bytes(settings: dict, source_bytes: int = 0) -> int | None:
    mode = settings.get("mode")
    if mode == "crf":
        return None
    target = settings.get("target_size")
    if target:
        return parse_target_size(target, source_bytes)
    if mode == "size":
        return None
    quality = canonical_target_size(str(settings.get("quality", "")))
    if quality in ("10mb", "20mb", "50mb", "50%", "25%") or quality.endswith(
        ("%", "mb")
    ):
        return parse_target_size(quality, source_bytes)
    return None


def parse_out_time(value: str) -> float:
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return 0.0


def _tune_args(codec_name: str, settings: dict) -> list[str]:
    tune = str(settings.get("tune", "none")).lower()
    if codec_name == "libx264" and tune in ("film", "animation"):
        return ["-tune", tune]
    if codec_name == "libx265" and tune == "animation":
        return ["-tune", tune]
    return []


def build_ffmpeg_args(
    src: Path,
    dst: Path,
    kind: str,
    settings: dict | None = None,
    info: MediaInfo | dict | None = None,
    pass_num: int | None = None,
    passlog_prefix: Path | str | None = None,
    target_bitrate: int | None = None,
) -> list[str]:
    settings = settings or DEFAULT_SETTINGS
    video_map = (
        f"0:{info['video_stream_index']}"
        if info and "video_stream_index" in info
        else "0:v:0"
    )
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
        video_map,
    ]

    vf = build_video_filter(kind, settings, info)
    if vf:
        cmd.extend(["-vf", vf])

    codec = str(settings.get("codec", "h264")).lower()
    if codec in ("hevc", "h265", "libx265", "x265"):
        codec_name = "libx265"
    elif codec in ("av1", "libsvtav1", "svtav1"):
        codec_name = "libsvtav1"
    elif codec in ("vp9", "libvpx-vp9"):
        codec_name = "libvpx-vp9"
    else:
        codec_name = "libx264"

    # Rate control: Target Size (2-pass) vs CRF (1-pass)
    if pass_num is not None:
        b_v = str(target_bitrate) if target_bitrate else "500000"
        if codec_name == "libx264":
            cmd.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-b:v",
                    b_v,
                    "-pass",
                    str(pass_num),
                    "-passlogfile",
                    str(passlog_prefix),
                    "-profile:v",
                    "high",
                    "-pix_fmt",
                    "yuv420p",
                    "-threads",
                    "0",
                ]
            )
        elif codec_name == "libx265":
            cmd.extend(
                [
                    "-c:v",
                    "libx265",
                    "-tag:v",
                    "hvc1",
                    "-preset",
                    "faster",
                    "-b:v",
                    b_v,
                    "-pass",
                    str(pass_num),
                    "-passlogfile",
                    str(passlog_prefix),
                    "-pix_fmt",
                    "yuv420p",
                    "-threads",
                    "0",
                ]
            )
        elif codec_name == "libsvtav1":
            cmd.extend(
                [
                    "-c:v",
                    "libsvtav1",
                    "-preset",
                    "8",
                    "-svtav1-params",
                    "preset=8",
                    "-b:v",
                    b_v,
                    "-pass",
                    str(pass_num),
                    "-passlogfile",
                    str(passlog_prefix),
                    "-pix_fmt",
                    "yuv420p",
                    "-threads",
                    "0",
                ]
            )
        elif codec_name == "libvpx-vp9":
            cmd.extend(
                [
                    "-c:v",
                    "libvpx-vp9",
                    "-b:v",
                    b_v,
                    "-pass",
                    str(pass_num),
                    "-passlogfile",
                    str(passlog_prefix),
                    "-row-mt",
                    "1",
                    "-pix_fmt",
                    "yuv420p",
                    "-threads",
                    "0",
                ]
            )

        cmd.extend(_tune_args(codec_name, settings))
        if pass_num == 1:
            cmd.extend(["-an", "-f", "null", "/dev/null"])
            return cmd

    else:
        # CRF mode
        quality_key = settings.get("quality", "balanced")
        quality = QUALITIES.get(quality_key, QUALITIES["balanced"])
        try:
            crf = (
                int(settings["crf"])
                if settings.get("crf") is not None
                else quality[2]
            )
        except (TypeError, ValueError):
            crf = quality[2]
        if kind == "video_note":
            crf = min(51, crf + 2)

        res = str(settings.get("resolution", "720"))
        bounds = VBV_BOUNDS.get(res, ("3500k", "7000k"))
        maxrate = settings.get("maxrate", bounds[0])
        bufsize = settings.get("bufsize", bounds[1])

        if codec_name == "libx264":
            cmd.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    quality[1],
                    "-crf",
                    str(crf),
                    "-maxrate",
                    str(maxrate),
                    "-bufsize",
                    str(bufsize),
                    "-profile:v",
                    "high",
                    "-pix_fmt",
                    "yuv420p",
                    "-threads",
                    "0",
                ]
            )
        elif codec_name == "libx265":
            preset = "faster" if quality_key in ("high", "lossless") else "fast"
            cmd.extend(
                [
                    "-c:v",
                    "libx265",
                    "-tag:v",
                    "hvc1",
                    "-preset",
                    preset,
                    "-crf",
                    str(crf),
                    "-maxrate",
                    str(maxrate),
                    "-bufsize",
                    str(bufsize),
                    "-pix_fmt",
                    "yuv420p",
                    "-threads",
                    "0",
                ]
            )
        elif codec_name == "libsvtav1":
            cmd.extend(
                [
                    "-c:v",
                    "libsvtav1",
                    "-preset",
                    "8",
                    "-svtav1-params",
                    "preset=8",
                    "-crf",
                    str(crf),
                    "-maxrate",
                    str(maxrate),
                    "-bufsize",
                    str(bufsize),
                    "-pix_fmt",
                    "yuv420p",
                    "-threads",
                    "0",
                ]
            )
        elif codec_name == "libvpx-vp9":
            cmd.extend(
                [
                    "-c:v",
                    "libvpx-vp9",
                    "-b:v",
                    "0",
                    "-crf",
                    str(crf),
                    "-row-mt",
                    "1",
                    "-pix_fmt",
                    "yuv420p",
                    "-threads",
                    "0",
                ]
            )

    if pass_num is None:
        cmd.extend(_tune_args(codec_name, settings))

    # Audio mapping and flags (for 1-pass CRF or pass 2)
    has_audio = info.get("has_audio", True) if info else True
    audio_opt = canonical_audio(str(settings.get("audio", "auto")))
    if has_audio and audio_opt != "mute":
        cmd.extend(["-map", "0:a:0?"])
    audio_args = build_audio_args(settings, info)
    cmd.extend(audio_args)

    # Container flags
    is_webm = dst.suffix.lower() == ".webm" or codec_name == "libvpx-vp9"
    if not is_webm:
        cmd.extend(["-movflags", "+faststart"])

    cmd.extend(
        [
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-max_muxing_queue_size",
            "1024",
            str(dst),
        ]
    )
    return cmd


def _run_ffmpeg_step(
    cmd: list[str],
    err_file: Path,
    duration: float,
    on_percent: Callable[[int], None] | None = None,
    percent_offset: int = 0,
    percent_scale: float = 100.0,
    job_id: str | None = None,
    max_duration: int = 3600,
    is_cancelled: Callable[[], bool] | None = None,
    on_process: Callable[[subprocess.Popen], None] | None = None,
) -> None:
    timed_out = False
    process = None
    watchdog = None

    def kill_on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        if process:
            try:
                process.kill()
            except OSError:
                pass

    try:
        if (is_cancelled and is_cancelled()) or is_job_cancelled(job_id):
            raise BotError("Compression was cancelled")
        with err_file.open("wb") as errors:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=errors, text=True
            )
            if on_process:
                on_process(process)
            if job_id:
                register_process(job_id, process)
            watchdog = threading.Timer(max_duration, kill_on_timeout)
            watchdog.start()

            for line in process.stdout:
                if (is_cancelled and is_cancelled()) or is_job_cancelled(job_id):
                    process.kill()
                    raise BotError("Compression was cancelled")
                if not on_percent or duration <= 0:
                    continue
                if line.startswith("out_time="):
                    seconds = parse_out_time(line.split("=", 1)[1].strip())
                    prog = percent_offset + int(seconds / duration * percent_scale)
                    on_percent(
                        max(
                            percent_offset + 1,
                            min(percent_offset + int(percent_scale) - 1, prog),
                        )
                    )
                elif line.startswith("progress=end"):
                    on_percent(percent_offset + int(percent_scale))

            process.stdout.close()
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                raise BotError("Compression took too long to finish") from None

            if timed_out:
                raise BotError("Compression timed out")

            if (
                (is_cancelled and is_cancelled())
                or is_job_cancelled(job_id)
                or process.returncode in (-9, 137)
            ):
                raise BotError("Compression was cancelled")

            if process.returncode != 0:
                detail = err_file.read_text(errors="replace").strip().splitlines()
                raise BotError(f"ffmpeg failed: {detail[-1] if detail else 'unknown'}")
    except OSError as exc:
        raise BotError(f"ffmpeg error: {exc}") from None
    finally:
        if watchdog:
            watchdog.cancel()
        if job_id:
            unregister_process(job_id)
        err_file.unlink(missing_ok=True)


def compress(
    src: Path,
    dst: Path,
    kind: str,
    settings: dict | None = None,
    duration: float = 0.0,
    on_percent: Callable[[int], None] | None = None,
    max_duration: int = 3600,
    job_id: str | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    on_process: Callable[[subprocess.Popen], None] | None = None,
    probe_info: MediaInfo | dict | None = None,
    metrics_job_id: str | None = None,
) -> MediaInfo:
    settings = settings or DEFAULT_SETTINGS
    info = probe_info if probe_info is not None else probe(src)
    dur = duration if duration > 0 else info.get("duration", 0.0)
    if info["duration"] > max_duration:
        raise BotError(
            f"Video is {info['duration'] / 60:.0f} min; "
            f"limit is {max_duration // 60} min."
        )

    job_id = job_id or settings.get("job_id")
    target_bytes = get_target_bytes(settings, src.stat().st_size if src.exists() else 0)

    if target_bytes and target_bytes > 0:
        # 2-Pass Target Size Mode
        audio_opt = canonical_audio(str(settings.get("audio", "auto")))
        if not info.get("has_audio") or audio_opt == "mute":
            audio_bps = 0
        elif audio_opt == "opus_64k":
            audio_bps = 64_000
        elif audio_opt == "mono":
            audio_bps = 48_000
        elif audio_opt == "copy":
            audio_bps = info.get("audio_bitrate") or 128_000
        else:
            audio_bps = 96_000

        target_bitrate = calculate_target_bitrate(target_bytes, dur, audio_bps)

        shm_dir = Path("/dev/shm")
        passlog_dir = shm_dir if shm_dir.is_dir() else dst.parent
        passlog_prefix = passlog_dir / f"tgcompress_pass_{os.getpid()}_{time.time_ns()}"

        try:
            # Pass 1
            pass1_cmd = build_ffmpeg_args(
                src=src,
                dst=dst,
                kind=kind,
                settings=settings,
                info=info,
                pass_num=1,
                passlog_prefix=passlog_prefix,
                target_bitrate=target_bitrate,
            )
            err1_file = dst.parent / f"{dst.name}.pass1.err"
            _profiled_ffmpeg_step(
                pass1_cmd,
                metrics_job_id,
                err_file=err1_file,
                duration=dur,
                on_percent=on_percent,
                percent_offset=0,
                percent_scale=50.0,
                job_id=job_id,
                max_duration=max_duration,
                is_cancelled=is_cancelled,
                on_process=on_process,
            )

            # Pass 2
            pass2_cmd = build_ffmpeg_args(
                src=src,
                dst=dst,
                kind=kind,
                settings=settings,
                info=info,
                pass_num=2,
                passlog_prefix=passlog_prefix,
                target_bitrate=target_bitrate,
            )
            err2_file = dst.parent / f"{dst.name}.pass2.err"
            _profiled_ffmpeg_step(
                pass2_cmd,
                metrics_job_id,
                err_file=err2_file,
                duration=dur,
                on_percent=on_percent,
                percent_offset=50,
                percent_scale=50.0,
                job_id=job_id,
                max_duration=max_duration,
                is_cancelled=is_cancelled,
                on_process=on_process,
            )
        finally:
            # Cleanup pass log files
            for log_file in passlog_dir.glob(f"{passlog_prefix.name}*"):
                try:
                    log_file.unlink(missing_ok=True)
                except OSError:
                    pass
    else:
        # 1-Pass CRF Mode
        cmd = build_ffmpeg_args(
            src=src,
            dst=dst,
            kind=kind,
            settings=settings,
            info=info,
            pass_num=None,
        )
        err_file = dst.parent / f"{dst.name}.err"
        _profiled_ffmpeg_step(
            cmd,
            metrics_job_id,
            err_file=err_file,
            duration=dur,
            on_percent=on_percent,
            percent_offset=0,
            percent_scale=100.0,
            job_id=job_id,
            max_duration=max_duration,
            is_cancelled=is_cancelled,
            on_process=on_process,
        )

    return probe(dst)


def _profiled_ffmpeg_step(cmd: list[str], metrics_job_id: str | None, **kwargs) -> None:
    import resource

    from .metrics import stage

    def option(name: str, default: str) -> str:
        return cmd[cmd.index(name) + 1] if name in cmd else default

    pass_number = int(option("-pass", "0")) or "crf"
    with stage(
        "ffmpeg",
        job_id=metrics_job_id or kwargs.get("job_id"),
        codec=option("-c:v", "unknown"),
        preset=option("-preset", "default"),
        cpu_used=option("-cpu-used", "default"),
        threads=option("-threads", "auto"),
        **{"pass": pass_number},
    ) as stats:
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        try:
            _run_ffmpeg_step(cmd=cmd, **kwargs)
        finally:
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            stats["child_process_cpu_seconds"] = round(
                after.ru_utime
                + after.ru_stime
                - before.ru_utime
                - before.ru_stime,
                3,
            )
            if cmd[-1] != "/dev/null":
                try:
                    stats["bytes"] = Path(cmd[-1]).stat().st_size
                except OSError:
                    pass


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
