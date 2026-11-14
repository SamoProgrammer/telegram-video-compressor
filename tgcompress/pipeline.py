"""The compress-one pipeline: download → probe → encode → send."""

from __future__ import annotations

import inspect
import threading
import time
from pathlib import Path
from typing import Any

from .config import Config
from .errors import BotError, Defer
from .files import safe_unlink
from .media import compress, get_target_bytes, probe, settings_line, target_kind
from .metrics import new_job_id, stage
from .transfer import Transfer

CODEC_LABELS = {
    "h264": "H.264",
    "x264": "H.264",
    "h265": "H.265",
    "hevc": "H.265",
    "x265": "H.265",
    "av1": "AV1",
    "svtav1": "AV1",
    "vp9": "VP9",
}


class ExecutionRegistry:
    """Tracks active transcode processes for immediate termination on cancel."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[Any, Any] = {}
        self._cancelled: set[Any] = set()

    def register(self, key: Any, proc: Any) -> None:
        with self._lock:
            self._active[key] = proc

    def unregister(self, key: Any) -> None:
        with self._lock:
            self._active.pop(key, None)

    def is_cancelled(self, key: Any) -> bool:
        with self._lock:
            return key in self._cancelled

    def clear_cancel(self, key: Any) -> None:
        with self._lock:
            self._cancelled.discard(key)

    def cancel(self, key: Any) -> bool:
        with self._lock:
            self._cancelled.add(key)
            proc = self._active.pop(key, None)
        if proc is None:
            return False
        if hasattr(proc, "kill"):
            try:
                proc.kill()
            except OSError:
                pass
            return True
        elif hasattr(proc, "terminate"):
            try:
                proc.terminate()
            except OSError:
                pass
            return True
        elif callable(proc):
            try:
                proc()
            except Exception:  # noqa: BLE001, S110
                pass
            return True
        return False

    def cancel_all(self) -> None:
        with self._lock:
            self._cancelled.update(self._active.keys())
            procs = list(self._active.values())
            self._active.clear()
        for proc in procs:
            if hasattr(proc, "kill"):
                try:
                    proc.kill()
                except OSError:
                    pass
            elif callable(proc):
                try:
                    proc()
                except Exception:  # noqa: BLE001, S110
                    pass


TRANSCODE_SEMAPHORE = threading.Semaphore(1)


class Pipeline:
    def __init__(
        self,
        config: Config,
        transfer: Transfer,
        probe_fn=probe,
        compress_fn=compress,
        registry: ExecutionRegistry | None = None,
        transcode_semaphore: threading.Semaphore | None = None,
    ) -> None:
        self.config = config
        self.transfer = transfer
        self.probe = probe_fn
        self.compress = compress_fn
        self.registry = registry or ExecutionRegistry()
        self.transcode_semaphore = transcode_semaphore or TRANSCODE_SEMAPHORE

    def _status(self, report):
        def say(phase: str, percent: int | None = None) -> None:
            if not report:
                return
            try:
                report(phase, percent)
            except BotError as exc:
                print(f"status update: {self.config.redact(exc)}", flush=True)

        return say

    def download_one(
        self,
        chat_id: int,
        reply_to: int,
        media: dict,
        report=None,
        job_id: str | None = None,
    ) -> Path:
        say = self._status(report)

        say("⏳ downloading")
        return self.transfer.download(
            media["file_id"],
            lambda pct: say("⏳ downloading", pct),
            chat_id=chat_id,
            message_id=reply_to,
            job_id=job_id,
            media_size=int(media.get("file_size") or 0),
        )

    def transcode_one(
        self,
        src: Path,
        kind: str,
        settings: dict,
        duration: float = 0.0,
        deadline: float | None = None,
        report=None,
        user_id: Any | None = None,
        probe_info: Any | None = None,
        job_id: str | None = None,
    ) -> dict:
        job_id = job_id or new_job_id()
        say = self._status(report)

        if deadline and duration and time.time() + duration + 120 > deadline:
            say("⏳ shift too short — retried next shift")
            raise Defer

        reg_key = user_id if user_id is not None else str(src)
        out_kind, fallback = target_kind(kind, settings, duration)
        out = self.config.work_dir / f"out-{time.time_ns()}.mp4"

        comp_kwargs: dict[str, Any] = {
            "duration": duration,
            "on_percent": lambda pct: say("⚙️ compressing", pct),
            "max_duration": self.config.max_duration,
            "is_cancelled": lambda: self.registry.is_cancelled(reg_key),
        }
        for opt in (
            "codec",
            "mode",
            "crf",
            "target_size",
            "fps",
            "audio",
            "tune",
            "tonemap",
        ):
            if opt in settings:
                comp_kwargs[opt] = settings[opt]
        if probe_info is not None:
            comp_kwargs["probe_info"] = probe_info
        comp_kwargs["metrics_job_id"] = job_id

        sig = inspect.signature(self.compress)
        accepted = sig.parameters
        has_varkw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in accepted.values()
        )
        if "on_process" in accepted or has_varkw:
            comp_kwargs["on_process"] = lambda proc: self.registry.register(
                reg_key, proc
            )

        if not has_varkw:
            comp_kwargs = {k: v for k, v in comp_kwargs.items() if k in accepted}

        with self.transcode_semaphore:
            if self.registry.is_cancelled(reg_key):
                self.registry.clear_cancel(reg_key)
                raise BotError("Transcode cancelled")
            say("⚙️ compressing")
            two_pass = bool(
                get_target_bytes(settings, src.stat().st_size if src.exists() else 0)
            )
            with stage(
                "encode",
                job_id=job_id,
                codec=str(settings.get("codec", "h264")),
                passes=2 if two_pass else 1,
                bytes=src.stat().st_size,
            ) as stats:
                try:
                    info = self.compress(src, out, out_kind, settings, **comp_kwargs)
                    stats["bytes"] = out.stat().st_size
                except Exception:
                    if self.registry.is_cancelled(reg_key):
                        raise BotError("Transcode cancelled") from None
                    raise
                finally:
                    self.registry.unregister(reg_key)

        in_mb = src.stat().st_size / 1048576
        out_mb = out.stat().st_size / 1048576
        if out.stat().st_size > self.config.upload_limit_bytes:
            limit = (
                "~2000 MB"
                if self.config.mt_ready
                else f"{self.config.upload_limit_bytes / 1048576:.1f} MiB"
            )
            raise BotError(
                f"Compressed file is {out_mb:.1f} MB; the upload limit is "
                f"{limit}. Choose a lower resolution or quality."
            )

        return {
            "out": out,
            "info": info,
            "out_kind": out_kind,
            "fallback": fallback,
            "in_mb": in_mb,
            "out_mb": out_mb,
        }

    def upload_one(
        self,
        chat_id: int,
        reply_to: int,
        media: dict,
        src: Path,
        out: Path | None,
        kind: str,
        out_kind: str,
        info: dict,
        settings: dict,
        in_mb: float,
        out_mb: float,
        filename: str = "compressed.mp4",
        fallback: bool = False,
        report=None,
        job_id: str | None = None,
    ) -> dict:
        say = self._status(report)

        try:
            if out is None or out_mb >= in_mb * 0.98:
                try:
                    self.transfer.send_original(chat_id, reply_to, media, out_kind, job_id)
                except BotError:
                    if out_kind == kind:
                        raise
                    self.transfer.send_original(chat_id, reply_to, media, kind, job_id)
                say(f"✅ sent original ({in_mb:.1f} MB)")
                return {
                    "in": in_mb,
                    "out": in_mb,
                    "saved": False,
                    "kind": out_kind,
                    "fallback": fallback,
                }

            saved = 100 * (1 - out_mb / in_mb)
            codec_name = str(settings.get("codec", "h264")).lower()
            codec_label = CODEC_LABELS.get(codec_name, "H.264")
            caption = (
                f"✅ {in_mb:.1f} MB → {out_mb:.1f} MB ({saved:.0f}% smaller) • "
                f"{codec_label} • {settings_line(settings)}"
            )
            if fallback:
                caption += (
                    "\nRound video is limited to 60 s, so this was sent as a video."
                )
            say("📤 uploading")
            self.transfer.send_result(
                chat_id,
                reply_to,
                out,
                out_kind,
                info,
                caption,
                filename,
                on_percent=lambda pct: say("📤 uploading", pct),
                job_id=job_id,
            )
            say(f"✅ {in_mb:.1f} → {out_mb:.1f} MB ({saved:.0f}%)")
            return {
                "in": in_mb,
                "out": out_mb,
                "saved": True,
                "kind": out_kind,
                "fallback": fallback,
            }
        finally:
            for leftover in (src, out):
                safe_unlink(leftover, self.config.work_dir)

    def compress_one(
        self,
        chat_id: int,
        reply_to: int,
        media: dict,
        kind: str,
        settings: dict,
        deadline: float | None = None,
        path: Path | None = None,
        filename: str = "compressed.mp4",
        report=None,
        user_id: Any | None = None,
        job_id: str | None = None,
    ) -> dict:
        job_id = job_id or new_job_id()
        say = self._status(report)

        say("⏳ downloading")
        src = path
        out = None
        try:
            if src is None or not src.exists():
                src = self.download_one(
                    chat_id=chat_id,
                    reply_to=reply_to,
                    media=media,
                    report=report,
                    job_id=job_id,
                )
            actual = self.probe(src)
            t_res = self.transcode_one(
                src=src,
                kind=kind,
                settings=settings,
                duration=actual["duration"],
                deadline=deadline,
                report=report,
                user_id=user_id or chat_id,
                probe_info=actual,
                job_id=job_id,
            )
            out = t_res["out"]
            return self.upload_one(
                chat_id=chat_id,
                reply_to=reply_to,
                media=media,
                src=src,
                out=out,
                kind=kind,
                out_kind=t_res["out_kind"],
                info=t_res["info"],
                settings=settings,
                in_mb=t_res["in_mb"],
                out_mb=t_res["out_mb"],
                filename=filename,
                fallback=t_res["fallback"],
                report=report,
                job_id=job_id,
            )
        except Defer:
            raise
        except BotError as exc:
            say(f"❌ {self.config.redact(exc)[:120]}")
            return {"error": self.config.redact(exc)}
        except Exception as exc:  # noqa: BLE001 - never kill the shift
            say("❌ unexpected error")
            print(
                f"compress_one: {type(exc).__name__}: {self.config.redact(exc)}",
                flush=True,
            )
            return {"error": self.config.redact(exc)}
        finally:
            for leftover in (src, out):
                safe_unlink(leftover, self.config.work_dir)
            self.registry.clear_cancel(user_id or chat_id)
