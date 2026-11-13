"""Environment-driven configuration for the bot."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Telegram's hard cap for bot transfers over MTProto, with headroom.
MT_UPLOAD_LIMIT_BYTES = 1900 * 1024 * 1024


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value) if value else default


@dataclass(frozen=True)
class Config:
    token: str = ""
    api_base: str = "https://api.telegram.org"
    run_seconds: int = 19500
    reserve_seconds: int = 300
    max_input_mb: int = 20
    max_output_mb: int = 50
    max_duration: int = 3600
    disk_budget_mb: int = 2048
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    state_file: Path = field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "bot-state.json"
    )
    mt_session_file: Path = field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "bot-mtproto.session"
    )
    work_dir: Path = field(
        default_factory=lambda: Path(tempfile.mkdtemp(prefix="tg-compress-"))
    )
    allowed_user_ids: frozenset[int] = frozenset()
    allowed_usernames: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "api_base", self.api_base.rstrip("/"))

    @classmethod
    def from_env(cls) -> Config:
        user_ids = {
            int(part)
            for part in os.environ.get("ALLOWED_USER_IDS", "")
            .replace(" ", "")
            .split(",")
            if part
        }
        usernames = {
            part.strip().lstrip("@").lower()
            for part in os.environ.get("ALLOWED_USERNAMES", "")
            .replace(";", ",")
            .split(",")
            if part.strip()
        }
        return cls(
            token=os.environ.get("BOT_TOKEN", ""),
            api_base=os.environ.get("BOT_API_URL", "https://api.telegram.org"),
            run_seconds=_env_int("BOT_RUN_SECONDS", 19500),
            reserve_seconds=_env_int("BOT_RESERVE_SECONDS", 300),
            max_input_mb=_env_int("MAX_INPUT_MB", 20),
            max_output_mb=_env_int("MAX_OUTPUT_MB", 50),
            max_duration=_env_int("MAX_DURATION", 3600),
            disk_budget_mb=_env_int("DISK_BUDGET_MB", 2048),
            telegram_api_id=_env_int("TELEGRAM_API_ID", 0),
            telegram_api_hash=os.environ.get("TELEGRAM_API_HASH", "").strip(),
            state_file=_env_path(
                "BOT_STATE_FILE",
                Path(tempfile.gettempdir()) / "bot-state.json",
            ),
            mt_session_file=_env_path(
                "BOT_MT_SESSION",
                Path(tempfile.gettempdir()) / "bot-mtproto.session",
            ),
            work_dir=Path(tempfile.mkdtemp(prefix="tg-compress-")),
            allowed_user_ids=frozenset(user_ids),
            allowed_usernames=frozenset(usernames),
        )

    @property
    def mt_ready(self) -> bool:
        return bool(self.telegram_api_id and self.telegram_api_hash)

    @property
    def input_limit_bytes(self) -> int:
        if self.mt_ready:
            return MT_UPLOAD_LIMIT_BYTES
        return self.max_input_mb * 1024 * 1024

    @property
    def upload_limit_bytes(self) -> int:
        if self.mt_ready:
            return MT_UPLOAD_LIMIT_BYTES
        return self.max_output_mb * 1024 * 1024

    @property
    def disk_budget_bytes(self) -> int:
        return self.disk_budget_mb * 1024 * 1024

    def is_allowed(self, user_id: int | None, username: str | None) -> bool:
        if not self.allowed_user_ids and not self.allowed_usernames:
            return True
        if user_id in self.allowed_user_ids:
            return True
        return bool(username and username.lstrip("@").lower() in self.allowed_usernames)

    def redact(self, text: object) -> str:
        value = str(text)
        return value.replace(self.token, "***") if self.token else value
