"""Tracked tasks ("arcs"): the optional second main story beside merged PRs.

An arc is one task on whatever board or tracker you use. The ledger takes two lists from it: the arcs CLOSED on the day
(each can be the episode's story, like a merged PR) and the arcs still OPEN that moved on the day (teasers for the
episode's out). Nothing here reads a tracker. The operator points settings.toml `sources.tracker` at a Python file of
their own that exposes `newest(day, start, end)`, `closed(day, start, end)` and `active(day, start, end)`;
docs/sources.md has the contract and a worked example. Without one, the day has no arcs and the scoreboard says the
count is unknown, never zero.

`newest` is the proof that the tracker's record reaches the day: the time of the newest event it holds at all. A log
that stops before the day (or holds nothing) says nothing about the day, so its arcs are left unknown for that day
instead of being read as "nothing closed" (`covers`).

Everything an adapter returns is untrusted: each row is checked here, a row that does not fit is counted in `skipped`
and dropped, and the titles are sanitized like every other title. An adapter that raises fails the collect stage
(TrackerReadError), because an unreadable tracker must never look like a day on which nothing closed.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from daily.day import day_window, to_utc
from daily.sources.transcripts import safe_title

TITLE_CHARS = 200  # a closed arc's title
TEASER_TITLE_CHARS = 120  # an open arc's title: it ships in ledger.teasers
MAX_ROWS = 1000
_ID_CHARS = 200


class TrackerReadError(Exception):
    """The tracker adapter could not be loaded, raised, or returned something that is not a list of rows. The message
    is a short code, never adapter output."""


@dataclass(frozen=True)
class ArcClose:
    arc_id: str
    title: str
    repo: str | None  # as the tracker spells it (owner/name, a path, a bare name); Config.classify normalizes it
    harness: str
    ts: str


class ClosedArcs(list):
    """The arcs closed on the day, plus `skipped`: rows that could not be used (bad shape, no usable time or id, a time
    outside the day)."""

    skipped: int = 0
    meta_failed: int = 0


@dataclass(frozen=True)
class ActiveArc:
    arc_id: str
    title: str
    repo: str | None
    harness: str
    last_ts: str  # the latest in-day activity, as the tracker spells it


class ActiveArcs(list):
    """The still-open arcs with activity on the day, newest first, plus `skipped` (as for ClosedArcs)."""

    skipped: int = 0
    meta_failed: int = 0


def _safe_id(v) -> bool:
    return (isinstance(v, str) and 0 < len(v) <= _ID_CHARS and v not in (".", "..")
            and not any(c in v for c in "/\\\0") and v.strip() == v)


def _rows(got, what: str) -> list:
    if not isinstance(got, (list, tuple)):
        raise TrackerReadError(f"bad_{what}_output")
    if len(got) > MAX_ROWS:
        raise TrackerReadError(f"too_many_{what}_rows")
    return list(got)


def _repo(v) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


class Tracker:
    """A loaded tracker adapter. `harnesses` is the set of harness ids a row may name; any other is "unknown"."""

    def __init__(self, module, harnesses=()):
        fns = [getattr(module, name, None) for name in ("newest", "closed", "active")]
        if not all(map(callable, fns)):
            raise TrackerReadError("adapter_shape")
        (self._newest, self._closed, self._active), self.harnesses = fns, frozenset(harnesses)

    def covers(self, day: dt.date) -> bool:
        """Whether the tracker's newest event is at or after the day's start: proof its record reaches the day. None
        (an empty record) or an older time is False. A time that is not an ISO string with a zone, or an adapter that
        raises, raises TrackerReadError."""
        start, end = day_window(day)
        try:
            got = self._newest(day, start, end)
        except Exception as e:
            raise TrackerReadError(f"newest_failed:{type(e).__name__}") from e
        if got is None:
            return False
        t = to_utc(got) if isinstance(got, str) else None
        if t is None:
            raise TrackerReadError("bad_newest_output")
        return t >= start

    def _harness(self, v) -> str:
        return v if isinstance(v, str) and v in self.harnesses else "unknown"

    def _call(self, fn, day: dt.date, what: str) -> list:
        start, end = day_window(day)
        try:
            got = fn(day, start, end)
        except Exception as e:  # the adapter's own failure: never a quiet day, and never its message
            raise TrackerReadError(f"{what}_failed:{type(e).__name__}") from e
        return _rows(got, what)

    def closed(self, day: dt.date) -> ClosedArcs:
        start, end = day_window(day)
        out, last = ClosedArcs(), {}
        for row in self._call(self._closed, day, "closed"):
            t = to_utc(row.get("ts")) if isinstance(row, dict) and isinstance(row.get("ts"), str) else None
            if t is None or not start <= t < end or not _safe_id(row.get("id")):
                out.skipped += 1
                continue
            if row["id"] not in last or t >= last[row["id"]][0]:  # closed twice in the day: counted once, latest
                last[row["id"]] = (t, row)
        for arc_id in sorted(last):
            row = last[arc_id][1]
            out.append(ArcClose(arc_id=arc_id, title=safe_title(row.get("title"), TITLE_CHARS), repo=_repo(row.get("repo")),
                                harness=self._harness(row.get("harness")), ts=row["ts"]))
        return out

    def active(self, day: dt.date) -> ActiveArcs:
        start, end = day_window(day)
        out, latest = ActiveArcs(), {}
        for row in self._call(self._active, day, "active"):
            t = to_utc(row.get("last_ts")) if isinstance(row, dict) and isinstance(row.get("last_ts"), str) else None
            if t is None or not start <= t < end or not _safe_id(row.get("id")):
                out.skipped += 1
                continue
            if row["id"] not in latest or t >= latest[row["id"]][0]:
                latest[row["id"]] = (t, row)
        found = sorted(latest.items(), key=lambda kv: kv[0])
        found.sort(key=lambda kv: kv[1][0], reverse=True)  # stable: equal instants keep their id order
        out.extend(ActiveArc(arc_id=arc_id, title=safe_title(row.get("title"), TEASER_TITLE_CHARS),
                             repo=_repo(row.get("repo")), harness=self._harness(row.get("harness")),
                             last_ts=row["last_ts"]) for arc_id, (_, row) in found)
        return out


def load_tracker(path: Path, harnesses=()) -> Tracker:
    """The adapter file at `path`, loaded by file location under a private module name (textcheck.load_module)."""
    from daily import textcheck  # lazy: textcheck pulls in paths and nothing of the sources
    try:
        module = textcheck.load_module(Path(path), "tracker")
    except Exception as e:  # missing, linked from outside its folder, broken on load
        raise TrackerReadError("adapter_unavailable") from e
    return Tracker(module, harnesses)
