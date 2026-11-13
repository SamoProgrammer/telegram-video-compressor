"""Thin HTTP client for the Telegram Bot API."""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests
import requests.adapters

from .errors import BotError

DEFAULT_TIMEOUT = 600


def reply_parameters(message_id: int) -> str:
    return json.dumps({"message_id": message_id, "allow_sending_without_reply": True})


def _rewind_files(files: dict | None) -> None:
    if not files:
        return
    for value in files.values():
        fileobj = None
        if isinstance(value, tuple) and len(value) >= 2:
            fileobj = value[1]
        elif hasattr(value, "seek"):
            fileobj = value
        if fileobj and hasattr(fileobj, "seek"):
            try:
                fileobj.seek(0)
            except (OSError, ValueError):
                pass


class BotApi:
    """One Bot API call per request; safe to use while an upload is streaming."""

    def __init__(
        self,
        token: str,
        base_url: str,
        http=None,
        timeout: int = DEFAULT_TIMEOUT,
        sleep_fn=time.sleep,
        max_retries: int = 3,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        if http is not None and http is not requests:
            self.http = http
        else:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=10,
                pool_maxsize=10,
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self.http = session
        self.timeout = timeout
        self.sleep_fn = sleep_fn
        self.max_retries = max_retries

    @staticmethod
    def _extract_retry_after(response) -> int | None:
        if hasattr(response, "json"):
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    retry = payload.get("parameters", {}).get("retry_after")
                    if retry is not None:
                        return int(retry)
            except (ValueError, TypeError, KeyError):
                pass
        if hasattr(response, "headers") and response.headers:
            try:
                val = response.headers.get("Retry-After", 0)
                return int(val) if val else None
            except (ValueError, TypeError):
                pass
        return None

    def call(self, method: str, files: dict | None = None, **params):
        url = f"{self.base_url}/bot{self.token}/{method}"
        clean = {
            key: ("true" if value is True else "false" if value is False else value)
            for key, value in params.items()
            if value is not None
        }
        for attempt in range(self.max_retries + 1):
            _rewind_files(files)
            try:
                response = self.http.post(
                    url, data=clean, files=files, timeout=self.timeout
                )
            except requests.RequestException:
                raise BotError(f"{method}: network error") from None

            if response.status_code == 429 and attempt < self.max_retries:
                retry_after = self._extract_retry_after(response)
                if not retry_after:
                    retry_after = 2**attempt
                self.sleep_fn(retry_after)
                continue

            payload = self._payload(response, method)
            if not payload.get("ok"):
                if payload.get("error_code") == 429 and attempt < self.max_retries:
                    retry_after = payload.get("parameters", {}).get("retry_after") or (
                        2**attempt
                    )
                    self.sleep_fn(retry_after)
                    continue
                description = (
                    payload.get("description") or f"HTTP {response.status_code}"
                )
                raise BotError(f"{method}: {description}")
            return payload["result"]

    def get_updates(self, offset: int | None, timeout: int = 50) -> list:
        params = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if offset is not None:
            params["offset"] = offset

        for attempt in range(self.max_retries + 1):
            try:
                response = self.http.get(
                    f"{self.base_url}/bot{self.token}/getUpdates",
                    params=params,
                    timeout=timeout + 20,
                )
            except requests.RequestException:
                raise BotError("getUpdates: network error") from None

            if response.status_code == 429 and attempt < self.max_retries:
                retry_after = self._extract_retry_after(response)
                if not retry_after:
                    retry_after = 2**attempt
                self.sleep_fn(retry_after)
                continue

            payload = self._payload(response, "getUpdates")
            if not payload.get("ok"):
                if payload.get("error_code") == 429 and attempt < self.max_retries:
                    retry_after = payload.get("parameters", {}).get("retry_after") or (
                        2**attempt
                    )
                    self.sleep_fn(retry_after)
                    continue
                description = (
                    payload.get("description") or f"HTTP {response.status_code}"
                )
                raise BotError(f"getUpdates: {description}")
            return payload["result"]

    def delete_webhook(self) -> None:
        self.call("deleteWebhook", drop_pending_updates=False)

    def get_file(self, file_id: str) -> dict:
        return self.call("getFile", file_id=file_id)

    def download(
        self,
        file_path: str,
        dest: Path,
        on_percent=None,
        total: int = 0,
    ) -> Path:
        url = f"{self.base_url}/file/bot{self.token}/{file_path}"
        received = 0
        try:
            with self.http.get(url, stream=True, timeout=self.timeout) as response:
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
        except requests.RequestException:
            raise BotError("Download failed (network error)") from None
        return dest

    @staticmethod
    def _payload(response, method: str) -> dict:
        try:
            return response.json()
        except ValueError:
            raise BotError(f"{method}: HTTP {response.status_code}") from None

    def close(self) -> None:
        if hasattr(self.http, "close"):
            self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class UploadMeter:
    """Streams an upload body and reports real sent-byte percent."""

    def __init__(self, handle, total: int, on_percent) -> None:
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
