"""Counts-only agent sources the operator supplies (settings.toml `[[sources.adapters]]`).

An adapter is a Python file exposing `scan(day, start, end)`: `day` is the local date, `start` and `end` the aware UTC
datetimes that bound it. It returns {"sessions": n, "subagents": m} with two non-negative integers, and raises on any
failure to read its source (never zeros for a source it could not read). docs/sources.md has the full contract and a
worked example.

Counts only, by design: an adapter's agent shows on the scoreboard and in the writer's coverage list, but gives no
quotes and no session notes, because the gate can re-read and verify quotes only from the built-in transcript readers.
A failing adapter drops its agent out of the day as a coverage gap (`unavailable:<code>`), like a built-in reader.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from daily.day import day_window
from daily.sources.transcripts import HarnessDay


class AdapterError(Exception):
    """An adapter could not be loaded or returned something other than its two counts. The message is a short code."""


@dataclass(frozen=True)
class AdapterDay(HarnessDay):
    """An adapter's counts; it never carries quotes."""


def _count(v) -> int:
    if type(v) is not int or v < 0:
        raise AdapterError("bad_adapter_output")
    return v


def load_scan(path: Path):
    """The adapter's scan function, loaded by file location under a private module name (textcheck.load_module)."""
    from daily import textcheck
    try:
        module = textcheck.load_module(Path(path), "adapter")
    except Exception as e:  # missing, linked from outside its folder, broken on load
        raise AdapterError("adapter_unavailable") from e
    scan = getattr(module, "scan", None)
    if not callable(scan):
        raise AdapterError("adapter_shape")
    return scan


def run_adapter(scan, day: dt.date) -> AdapterDay:
    """One adapter's day. Its own exceptions propagate (the collect stage records them as a coverage gap); output that
    is not the two counts raises AdapterError."""
    start, end = day_window(day)
    got = scan(day, start, end)
    if not isinstance(got, dict) or set(got) != {"sessions", "subagents"}:
        raise AdapterError("bad_adapter_output")
    return AdapterDay(_count(got["sessions"]), _count(got["subagents"]), ())
