"""Filesystem helpers confined to the per-shift work directory."""

from __future__ import annotations

from pathlib import Path


def safe_unlink(path: Path | str | None, root: Path | str) -> None:
    """Delete a file only when it lives strictly under the bot's work directory."""
    if not path:
        return
    try:
        p = Path(path).resolve()
        r = Path(root).resolve()
        if p != r and p.is_relative_to(r):
            p.unlink(missing_ok=True)
    except (ValueError, OSError):
        pass
