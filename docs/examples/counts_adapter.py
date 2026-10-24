"""A worked example of a counts-only agent adapter (docs/sources.md).

It reads an imaginary agent's session logs: one JSONL file per session under a folder, one event per line, each with
an ISO `ts` (UTC, with a zone) and an optional `parent` (set for a session the agent spawned as a helper). A session
counts on the day when at least one of its events falls inside [start, end).

Point settings.toml at a copy of this file and adapt the reading to your agent:

    [[sources.adapters]]
    harness = "my_agent"
    display = "My Agent"
    module = "~/.config/orion-on-the-clock/adapters/counts_adapter.py"

The folder comes from MY_AGENT_LOGS (default ~/.my-agent/sessions). Stdlib only, read-only.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path


def _when(ts):
    if not isinstance(ts, str):
        return None
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def scan(day: dt.date, start: dt.datetime, end: dt.datetime) -> dict:
    """{"sessions": n, "subagents": m} for the day. Raises when the logs cannot be read: a missing folder must never
    look like a quiet day."""
    root = Path(os.environ.get("MY_AGENT_LOGS") or "~/.my-agent/sessions").expanduser()
    if not root.is_dir():
        raise FileNotFoundError("my_agent_logs_missing")  # a short code: it becomes the coverage reason
    sessions = subagents = 0
    for path in sorted(root.glob("*.jsonl")):
        active, helper = False, False
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # a torn last line in a live log
                if not isinstance(event, dict):
                    continue
                t = _when(event.get("ts"))
                if t is not None and start <= t < end:
                    active = True
                    helper = helper or bool(event.get("parent"))
        if active:
            subagents += helper
            sessions += not helper
    return {"sessions": sessions, "subagents": subagents}
