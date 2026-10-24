"""The operator's own sources: counts-only adapters and the tracker adapter, their loaders and the row checks, and the
worked examples in docs/examples/ run against synthetic logs (so the docs cannot drift from the contract)."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path

import pytest

from daily import collect, paths
from daily.day import day_window
from daily.ledger import build_ledger
from daily.settings import Adapter
from daily.sources import adapters, tracker
from daily.sources.tracker import ActiveArc, ArcClose, Tracker, TrackerReadError

DAY = dt.date(2026, 10, 16)
EXAMPLES = paths.REPO / "docs" / "examples"
IN = "2026-10-16T20:00:00Z"  # inside the day (Pacific in the tests)
BEFORE = "2026-10-16T06:59:59Z"  # still the 15th
AFTER = "2026-10-17T07:00:00Z"  # already the 17th


def write_jsonl(path: Path, rows: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows))
    return path


# ---- counts-only adapters ----

def test_the_example_counts_adapter_counts_sessions_and_helpers(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    write_jsonl(logs / "a.jsonl", [{"ts": BEFORE}, {"ts": IN}])                     # active on the day
    write_jsonl(logs / "b.jsonl", [{"ts": IN, "parent": "a"}])                     # a helper the agent spawned
    write_jsonl(logs / "c.jsonl", [{"ts": BEFORE}, {"ts": AFTER}, "{torn line"])   # not on the day
    monkeypatch.setenv("MY_AGENT_LOGS", str(logs))
    scan = adapters.load_scan(EXAMPLES / "counts_adapter.py")
    day = adapters.run_adapter(scan, DAY)
    assert (day.sessions, day.subagents, day.quotes) == (1, 1, ())


def test_the_example_counts_adapter_raises_on_a_missing_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_AGENT_LOGS", str(tmp_path / "nope"))
    scan = adapters.load_scan(EXAMPLES / "counts_adapter.py")
    with pytest.raises(FileNotFoundError, match="^my_agent_logs_missing$"):
        adapters.run_adapter(scan, DAY)


def test_an_adapter_is_handed_the_local_day_and_its_utc_bounds():
    seen = []
    adapters.run_adapter(lambda day, start, end: seen.append((day, start, end)) or {"sessions": 0, "subagents": 0},
                         DAY)
    assert seen == [(DAY, *day_window(DAY))]


@pytest.mark.parametrize("text,code", [("x = 1\n", "adapter_shape"), ("raise ImportError('x')\n", "adapter_unavailable"),
                                       ("scan = 5\n", "adapter_shape")])
def test_a_broken_adapter_file_is_refused(tmp_path, text, code):
    (tmp_path / "a.py").write_text(text)
    with pytest.raises(adapters.AdapterError, match=f"^{code}$"):
        adapters.load_scan(tmp_path / "a.py")


def test_a_missing_or_linked_adapter_file_is_refused(tmp_path):
    with pytest.raises(adapters.AdapterError, match="^adapter_unavailable$"):
        adapters.load_scan(tmp_path / "missing.py")
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "a.py").write_text("def scan(d, s, e):\n    return {'sessions': 0, 'subagents': 0}\n")
    (tmp_path / "linked").mkdir()
    (tmp_path / "linked" / "a.py").symlink_to(tmp_path / "real" / "a.py")
    with pytest.raises(adapters.AdapterError, match="^adapter_unavailable$"):
        adapters.load_scan(tmp_path / "linked" / "a.py")
    assert callable(adapters.load_scan(tmp_path / "real" / "a.py"))  # positive control


# ---- the tracker ----

def tracker_log(tmp_path):
    return write_jsonl(tmp_path / "events.jsonl", [
        {"ts": BEFORE, "task": "t-1", "event": "opened", "title": "Add the export button", "repo": "owner/demo-tool"},
        {"ts": IN, "task": "t-1", "event": "closed", "title": "Add the export button", "repo": "owner/demo-tool",
         "actor": "claude/export"},
        {"ts": IN, "task": "t-2", "event": "moved", "title": "Draft the import flow", "repo": "owner/demo-tool",
         "actor": "codex/import"},
        {"ts": IN, "task": "t-3", "event": "moved", "title": "Rotate the keys", "repo": "owner/backup-box"},
        {"ts": AFTER, "task": "t-4", "event": "closed", "title": "Tomorrow's task", "repo": "owner/demo-tool"},
        "{torn",
    ])


def test_the_example_tracker_reads_closed_and_moving_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TRACKER_EVENTS", str(tracker_log(tmp_path)))
    t = tracker.load_tracker(EXAMPLES / "tracker_jsonl.py", {"claude_code", "codex"})
    assert t.covers(DAY)  # its newest event is on the 17th: the log reaches the day
    closed, moving = t.closed(DAY), t.active(DAY)
    assert list(closed) == [ArcClose("t-1", "Add the export button", "owner/demo-tool", "claude_code", IN)]
    assert [(a.arc_id, a.harness) for a in moving] == [("t-2", "codex"), ("t-3", "unknown")]
    assert all(isinstance(a, ActiveArc) for a in moving) and closed.skipped == moving.skipped == 0


def test_the_example_tracker_does_not_cover_a_day_after_its_log_stops(tmp_path, monkeypatch):
    log = write_jsonl(tmp_path / "events.jsonl", [{"ts": BEFORE, "task": "t-1", "event": "opened", "title": "Old"}])
    monkeypatch.setenv("MY_TRACKER_EVENTS", str(log))
    t = tracker.load_tracker(EXAMPLES / "tracker_jsonl.py")
    assert not t.covers(DAY) and t.covers(DAY - dt.timedelta(days=1))
    log.write_text("")
    assert not t.covers(DAY - dt.timedelta(days=1))  # an empty log proves nothing


def test_the_example_tracker_feeds_the_ledger_through_the_lanes(tmp_path, monkeypatch, cfg, settings):
    """End to end through gather: the public lane's task is an item and a teaser; the headline lane's task shows
    only in the count, never by title."""
    monkeypatch.setenv("MY_TRACKER_EVENTS", str(tracker_log(tmp_path)))
    s = dataclasses.replace(settings, github=False, claude_code=False, tracker=EXAMPLES / "tracker_jsonl.py",
                            adapters=(Adapter("my_agent", "My Agent", EXAMPLES / "counts_adapter.py"),))
    monkeypatch.setenv("MY_AGENT_LOGS", str(tmp_path / "no-logs"))
    raw = collect.gather(DAY, cfg, s, codex_db=tmp_path / "no-codex.sqlite")
    led, _ = build_ledger(DAY, raw, cfg, "2026-10-17T12:00:00Z", str(tmp_path / "github.jsonl"))
    assert led["scoreboard"]["arcs_closed"] == 1 and led["scoreboard"]["prs_merged"] is None
    assert [i["title"] for i in led["items"]] == ["Add the export button"]
    assert [t["title"] for t in led["teasers"]] == ["Draft the import flow"]
    assert "Rotate the keys" not in json.dumps(led)
    assert led["coverage"] == {"codex": "unavailable:db_missing", "my_agent": "unavailable:my_agent_logs_missing"}


class Fake:
    def __init__(self, closed=(), active=(), boom=None, newest=AFTER):
        self._c, self._a, self.boom, self._n = closed, active, boom, newest

    def newest(self, day, start, end):
        if isinstance(self._n, BaseException):
            raise self._n
        return self._n

    def closed(self, day, start, end):
        if self.boom:
            raise self.boom
        return self._c

    def active(self, day, start, end):
        return self._a


@pytest.mark.parametrize("row", [
    "not a dict", {"id": "a"}, {"id": "a", "ts": BEFORE}, {"id": "a", "ts": AFTER}, {"id": "", "ts": IN},
    {"id": "../x", "ts": IN}, {"id": " a", "ts": IN}, {"id": "a" * 201, "ts": IN}, {"id": 5, "ts": IN},
    {"id": "a", "ts": "2026-10-16T20:00:00"},
], ids=["string", "no-ts", "before", "after", "empty-id", "path-id", "padded-id", "long-id", "int-id", "naive-ts"])
def test_a_tracker_row_that_does_not_fit_is_skipped_and_counted(row):
    t = Tracker(Fake(closed=[row, {"id": "ok", "ts": IN, "title": "Fine"}]))
    got = t.closed(DAY)
    assert [a.arc_id for a in got] == ["ok"] and got.skipped == 1


def test_tracker_titles_are_sanitized_and_harnesses_checked():
    t = Tracker(Fake(closed=[{"id": "a", "ts": IN, "title": "Fix​ the\nflag‮", "harness": "claude_code"},
                             {"id": "b", "ts": IN, "title": 7, "harness": "rogue"}]), {"claude_code"})
    a, b = t.closed(DAY)
    assert (a.title, a.harness) == ("Fix the flag", "claude_code")
    assert (b.title, b.harness, b.repo) == ("", "unknown", None)


def test_a_task_closed_twice_counts_once_at_its_latest_close():
    later = "2026-10-16T22:00:00Z"
    t = Tracker(Fake(closed=[{"id": "a", "ts": IN, "title": "One"}, {"id": "a", "ts": later, "title": "Two"}]))
    assert [(a.title, a.ts) for a in t.closed(DAY)] == [("Two", later)]


def test_active_tasks_are_newest_first():
    t = Tracker(Fake(active=[{"id": "a", "last_ts": "2026-10-16T19:00:00Z"}, {"id": "b", "last_ts": IN}]))
    assert [a.arc_id for a in t.active(DAY)] == ["b", "a"]


@pytest.mark.parametrize("fake,code", [(Fake(boom=OSError("/home/someone/private")), "closed_failed:OSError"),
                                       (Fake(closed="nope"), "bad_closed_output"),
                                       (Fake(closed=[{}] * 1001), "too_many_closed_rows")])
def test_a_failing_tracker_raises_a_short_code_never_its_message(fake, code):
    with pytest.raises(TrackerReadError, match=f"^{code}$"):
        Tracker(fake).closed(DAY)


@pytest.mark.parametrize("newest,covers", [(AFTER, True), (IN, True), ("2026-10-16T07:00:00Z", True),
                                           (BEFORE, False), (None, False)])
def test_covers_needs_a_newest_event_at_or_after_the_day_start(newest, covers):
    assert Tracker(Fake(newest=newest)).covers(DAY) is covers


@pytest.mark.parametrize("newest,code", [(1_760_000_000, "bad_newest_output"), ("soon", "bad_newest_output"),
                                         ("2026-10-16T20:00:00", "bad_newest_output"),
                                         (OSError("/home/someone/x"), "newest_failed:OSError")])
def test_a_bad_newest_answer_raises_a_short_code(newest, code):
    with pytest.raises(TrackerReadError, match=f"^{code}$"):
        Tracker(Fake(newest=newest)).covers(DAY)


def test_a_numeric_time_in_a_row_is_skipped_not_stored():
    t = Tracker(Fake(closed=[{"id": "a", "ts": 1_760_644_800, "title": "Epoch"}]))
    got = t.closed(DAY)
    assert list(got) == [] and got.skipped == 1


def test_a_tracker_file_without_all_three_functions_is_refused(tmp_path):
    (tmp_path / "t.py").write_text("def closed(d, s, e):\n    return []\ndef active(d, s, e):\n    return []\n")
    with pytest.raises(TrackerReadError, match="^adapter_shape$"):
        tracker.load_tracker(tmp_path / "t.py")
    with pytest.raises(TrackerReadError, match="^adapter_unavailable$"):
        tracker.load_tracker(tmp_path / "missing.py")
