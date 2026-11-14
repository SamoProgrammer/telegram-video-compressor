"""Download and send media, choosing Bot API or MTProto by size."""

from __future__ import annotations

import os
import time
from pathlib import Path

from .config import CLOUD_UPLOAD_LIMIT_BYTES, Config
from .errors import BotError, UploadTooLarge
from .mtproto import MTProtoClient
from .telegram_api import BotApi, UploadMeter, reply_parameters

BOT_API_UPLOAD_CAP = CLOUD_UPLOAD_LIMIT_BYTES


class Transfer:
    def __init__(self, config: Config, api: BotApi, mtproto: MTProtoClient) -> None:
        self.config = config
        self.api = api
        self.mtproto = mtproto

    def download(
        self,
        file_id: str,
        on_percent=None,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> Path:
        dest = self.config.work_dir / f"in-{time.time_ns()}.bin"
        try:
            info = self.api.get_file(file_id)
        except BotError as exc:
            # The cloud HTTP Bot API caps getFile at 20 MB; MTProto with the
            # same bot token does not.
            if (
                "too big" not in self.config.redact(exc).lower()
                or not self.config.mt_ready
                or not chat_id
                or not message_id
            ):
                raise
            return self.mtproto.download(chat_id, message_id, dest, on_percent)
        file_path = info.get("file_path", "")
        # A local Bot API server returns an absolute path and no cap.
        if file_path and os.path.isabs(file_path) and os.path.exists(file_path):
            return Path(file_path)
        if not file_path:
            raise BotError("Telegram returned no file path")
        total = int(info.get("file_size") or 0)
        return self.api.download(file_path, dest, on_percent, total)

    def send_original(
        self, chat_id: int, message_id: int, media: dict, kind: str
    ) -> None:
        reply = reply_parameters(message_id)
        if kind == "video_note":
            self.api.call(
                "sendVideoNote",
                chat_id=chat_id,
                video_note=media["file_id"],
                reply_parameters=reply,
            )
        elif kind == "document":
            self.api.call(
                "sendDocument",
                chat_id=chat_id,
                document=media["file_id"],
                reply_parameters=reply,
            )
        else:
            self.api.call(
                "sendVideo",
                chat_id=chat_id,
                video=media["file_id"],
                reply_parameters=reply,
            )

    def send_result(
        self,
        chat_id: int,
        message_id: int,
        path: Path,
        kind: str,
        info: dict,
        caption: str,
        filename: str = "compressed.mp4",
        on_percent=None,
    ) -> None:
        size = path.stat().st_size
        limit = self.config.bot_api_upload_limit_bytes
        api_mode = "cloud" if self.config.using_cloud_api else "local"
        use_mtproto = size > limit and self.config.mt_ready
        route = "mtproto" if use_mtproto else "bot_api"
        if size > limit and self.config.using_cloud_api and not self.config.mt_ready:
            route = "error"
        print(
            f"upload decision: size_bytes={size} route={route} "
            f"api_mode={api_mode} mt_ready={self.config.mt_ready} "
            f"limit_bytes={limit}",
            flush=True,
        )

        def send_mtproto(reason: str) -> None:
            print(
                f"upload fallback: size_bytes={size} route=mtproto reason={reason}",
                flush=True,
            )
            self.mtproto.upload(
                chat_id,
                message_id,
                path,
                kind,
                caption,
                filename,
                info,
                on_percent,
            )

        if use_mtproto:
            send_mtproto("size_preflight")
            return
        if size > limit and self.config.using_cloud_api and not self.config.mt_ready:
            raise UploadTooLarge(
                f"Compressed output is {size / 1048576:.1f} MiB; the safe cloud "
                f"limit is {limit} bytes. Enable MTProto or a local Bot API server, "
                "or choose a smaller target size/resolution."
            )

        reply = reply_parameters(message_id)
        duration = max(1, int(info["duration"]))
        try:
            with path.open("rb") as handle:
                body = UploadMeter(handle, size, on_percent) if on_percent else handle
                if kind == "video_note":
                    self.api.call(
                        "sendVideoNote",
                        files={"video_note": (filename, body, "video/mp4")},
                        chat_id=chat_id,
                        duration=duration,
                        length=int(info["width"]),
                        reply_parameters=reply,
                    )
                elif kind == "document":
                    self.api.call(
                        "sendDocument",
                        files={"document": (filename, body, "video/mp4")},
                        chat_id=chat_id,
                        caption=caption,
                        reply_parameters=reply,
                    )
                else:
                    self.api.call(
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
        except UploadTooLarge:
            if not self.config.mt_ready:
                raise UploadTooLarge(
                    f"Telegram rejected the {size}-byte upload as too large. "
                    "Enable MTProto or a local Bot API server, or choose a "
                    "smaller target size/resolution."
                ) from None
            send_mtproto("telegram_rejected")
