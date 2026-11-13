"""Shared fakes and wiring for the test suite."""

from __future__ import annotations

from pathlib import Path

from tgcompress.batch import BatchRunner
from tgcompress.config import Config
from tgcompress.errors import BotError
from tgcompress.handlers import Handlers
from tgcompress.pipeline import Pipeline
from tgcompress.queue import BatchCache
from tgcompress.state import StateStore
from tgcompress.transfer import Transfer
from tgcompress.ui import CardRenderer


class FakeApi:
    token = "test-token"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, method: str, files=None, **params):
        self.calls.append((method, params))
        return {"message_id": len(self.calls)}

    def get_file(self, file_id: str) -> dict:
        raise BotError("getFile: file is too big")


class StubMTProto:
    def __init__(self) -> None:
        self.downloads: list[tuple] = []
        self.uploads: list[tuple] = []

    def download(self, chat_id, message_id, dest, on_percent=None):
        self.downloads.append((chat_id, message_id, dest))
        return dest

    def upload(self, *args, **kwargs):
        self.uploads.append((args, kwargs))


class Stack:
    def __init__(self, config: Config, pipeline=None) -> None:
        self.config = config
        self.api = FakeApi()
        self.store = StateStore(config.state_file)
        self.mtproto = StubMTProto()
        self.transfer = Transfer(config, self.api, self.mtproto)
        self.cards = CardRenderer(self.api, self.store, config)
        self.pipeline = pipeline or Pipeline(config, self.transfer)
        self.cache = BatchCache(config, self.transfer)
        self.batch = BatchRunner(config, self.store, self.pipeline, self.cards)
        self.handlers = Handlers(
            config,
            self.store,
            self.cards,
            self.pipeline,
            self.batch,
            self.cache,
        )


def make_config(tmp: Path, **overrides) -> Config:
    values = {
        "token": "test-token",
        "work_dir": tmp,
        "state_file": tmp / "state.json",
    }
    values.update(overrides)
    return Config(**values)


def make_stack(tmp: Path, **overrides) -> Stack:
    pipeline = overrides.pop("pipeline", None)
    return Stack(make_config(tmp, **overrides), pipeline=pipeline)


def item(name: str, **extra) -> dict:
    base = {
        "media": {"file_id": name},
        "kind": "video",
        "chat_id": 10,
        "message_id": 40 + len(name),
        "size": 1024,
        "name": name,
        "filename": f"{name}.mp4",
        "path": None,
        "duration": 1,
        "status": "queued",
        "delivered": False,
    }
    base.update(extra)
    return base
