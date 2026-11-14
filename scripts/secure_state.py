#!/usr/bin/env python3
"""Encrypt and restore the fixed bot state files used by Actions caching."""

import argparse
import binascii
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

PRIVATE_DIR = Path("/tmp/tg-private")
CACHE_DIR = Path("/tmp/tg-cache")
FILENAMES = ("bot-state.json", "bot-mtproto.session")


class SecureStateError(Exception):
    """A safe-to-report state encryption error."""


def _fernet(operation):
    key = os.environ.get("BOT_STATE_ENCRYPTION_KEY", "").strip()
    if not key:
        raise SecureStateError(
            f"BOT_STATE_ENCRYPTION_KEY is required to {operation} existing state."
        )
    try:
        return Fernet(key.encode("ascii"))
    except (TypeError, ValueError, binascii.Error) as exc:
        raise SecureStateError(
            "BOT_STATE_ENCRYPTION_KEY is not a valid Fernet key."
        ) from exc


def _atomic_write(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".secure-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as temporary_file:
            temporary_file.write(data)
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _prepare_dirs() -> None:
    for directory in (PRIVATE_DIR, CACHE_DIR):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)


def restore():
    """Decrypt every available cache file before replacing any plaintext."""
    _prepare_dirs()
    restored = []
    fernet = None
    for name in FILENAMES:
        encrypted_path = CACHE_DIR / f"{name}.enc"
        if not encrypted_path.exists():
            continue
        if fernet is None:
            fernet = _fernet("restore")
        try:
            plaintext = fernet.decrypt(encrypted_path.read_bytes())
        except InvalidToken as exc:
            raise SecureStateError(
                f"Cannot decrypt {encrypted_path.name}: wrong key or corrupt "
                "encrypted state; refusing to overwrite plaintext."
            ) from exc
        restored.append((PRIVATE_DIR / name, plaintext))

    for path, plaintext in restored:
        _atomic_write(path, plaintext)


def save():
    """Encrypt every existing plaintext file before replacing any cache file."""
    _prepare_dirs()
    encrypted_files = []
    fernet = None
    for name in FILENAMES:
        plaintext_path = PRIVATE_DIR / name
        if not plaintext_path.exists():
            continue
        if fernet is None:
            fernet = _fernet("save")
        encrypted_files.append(
            (CACHE_DIR / f"{name}.enc", fernet.encrypt(plaintext_path.read_bytes()))
        )

    for path, encrypted in encrypted_files:
        _atomic_write(path, encrypted)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("restore", "save"))
    args = parser.parse_args()
    try:
        if args.command == "restore":
            restore()
        else:
            save()
    except (SecureStateError, OSError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
