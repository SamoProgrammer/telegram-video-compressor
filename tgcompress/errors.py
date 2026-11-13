"""Errors shared by every layer."""


class BotError(RuntimeError):
    """A user-facing failure: network, Telegram, ffmpeg, or a limit."""


class Defer(Exception):
    """The current shift is too short for this video; leave it for the next."""
