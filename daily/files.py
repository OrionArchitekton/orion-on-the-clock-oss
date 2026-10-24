"""Atomic writes and small helpers. A reader never sees a half-written JSON file."""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_text(path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def write_json(path, obj) -> None:
    write_text(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LockBusy(Exception):
    """The lock stayed held for the whole bounded wait."""


@contextlib.contextmanager
def file_lock(path, wait_s: float, poll_s: float = 0.1):
    """An exclusive flock on `path` (created if missing, never deleted), waited for at most `wait_s` seconds, else
    LockBusy. The kernel drops it when the process ends. Every read-modify-write of one shared receipt takes the same
    lock, so concurrent writers cannot drop each other's entries."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockBusy(str(path)) from None
                time.sleep(poll_s)
        yield
    finally:
        os.close(fd)
