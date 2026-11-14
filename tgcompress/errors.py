"""Errors shared by every layer."""


class BotError(RuntimeError):
    """A user-facing failure: network, Telegram, ffmpeg, or a limit."""


class UploadTooLarge(BotError):
    """The selected Telegram transport rejected the upload because of its size."""


class Defer(Exception):
    """The current shift is too short for this video; leave it for the next."""
