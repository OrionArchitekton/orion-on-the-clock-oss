"""A worked example of a tracker adapter (docs/sources.md): tasks closed on the day, and open tasks that moved.

It reads an imaginary tracker's event log, one JSON object per line:

    {"ts": "2026-10-16T21:00:00Z", "task": "t-42", "event": "closed", "title": "Add the export button",
     "repo": "owner/demo-tool", "actor": "claude/export-button"}

`event` is "opened", "moved" or "closed"; the latest event of a task decides whether it is open. `actor` names whoever
acted, and a branch-style prefix ("claude/...", "codex/...") credits an agent, as merged PRs are credited. Every title
you return is checked by the ledger like any other text, and a task whose repo is not a public lane never shows by
name.

Point settings.toml at a copy of this file and adapt the reading to your tracker:

    [sources]
    tracker = "~/.config/orion-on-the-clock/adapters/tracker_jsonl.py"

The log comes from MY_TRACKER_EVENTS (default ~/.my-tracker/events.jsonl). Stdlib only, read-only.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

PREFIXES = (("claude/", "claude_code"), ("codex/", "codex"))


def _when(ts):
    if not isinstance(ts, str):
        return None
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def _harness(actor) -> str:
    actor = actor if isinstance(actor, str) else ""
    return next((h for prefix, h in PREFIXES if actor.startswith(prefix)), "unknown")


def _events() -> list:
    path = Path(os.environ.get("MY_TRACKER_EVENTS") or "~/.my-tracker/events.jsonl").expanduser()
    out = []
    with path.open(encoding="utf-8", errors="replace") as fh:  # a missing log raises: never a quiet day
        for line in fh:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and _when(event.get("ts")) is not None:
                out.append(event)
    out.sort(key=lambda e: _when(e["ts"]))
    return out


def newest(day: dt.date, start: dt.datetime, end: dt.datetime):
    """The time of the newest event in the whole log (None for an empty one): proof the log reaches the day."""
    events = _events()
    return events[-1]["ts"] if events else None


def closed(day: dt.date, start: dt.datetime, end: dt.datetime) -> list:
    """Every task whose close falls inside the day."""
    return [{"id": e.get("task"), "title": e.get("title"), "repo": e.get("repo"), "harness": _harness(e.get("actor")),
             "ts": e["ts"]}
            for e in _events() if e.get("event") == "closed" and start <= _when(e["ts"]) < end]


def active(day: dt.date, start: dt.datetime, end: dt.datetime) -> list:
    """Every task still open whose latest event of the day falls inside it."""
    latest, moved = {}, {}
    for e in _events():
        latest[e.get("task")] = e
        if start <= _when(e["ts"]) < end:
            moved[e.get("task")] = e
    return [{"id": task, "title": e.get("title"), "repo": e.get("repo"), "harness": _harness(e.get("actor")),
             "last_ts": e["ts"]}
            for task, e in moved.items() if latest[task].get("event") != "closed"]
