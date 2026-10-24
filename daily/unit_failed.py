#!/usr/bin/env python3
"""OnFailure handler for the timer's service (systemd/orion-on-the-clock-failed.sh): the run died before the notify
stage could report (an entry script refusal, a crash, a kill). It sends one approval item through the same delivery
as the notify stage (settings.toml `notify.command`, or the default sink: written under <state>/notify/ and printed).

The item is keyed by the day the failed run SELECTED: run.py records it in <state>/last-run.json before any stage, and
this reads it (a catch-up that picked day N-2 before 05:30 and died after it still files under N-2). Without a usable
record (missing, unreadable, or older than one run) it falls back to the clock's due day (day.due_day) and says so in
the item title. The item id the add answers with is recorded in that day's notify.json under `unit-failed`, written
atomically with the other entries kept and under the same notify.lock the notify stage holds (a lock held past a
short wait records nothing; the item is still added), so a later run that reports the day expires the item as a
superseded one. A failed send exits non-zero (the journal keeps it). An answer without a usable open item id records
nothing but the add still counts.

Settings that cannot be loaded (the very thing that may have killed the run) do not stop the report: the item goes to
the default sink, and the day is cut in UTC unless the last-run record names it."""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daily import files, notify, paths  # noqa: E402
from daily.day import due_day, use_timezone, zone  # noqa: E402
from daily.settings import SettingsError, load_settings  # noqa: E402

UNIT = "orion-on-the-clock"
TITLE = f"Daily episode: the timer run failed before it could report (check: journalctl --user -u {UNIT})"
GUESS_NOTE = " [day guessed from the clock: no usable last-run record]"
POINTER = f"journalctl --user -u {UNIT}"
RECORD_KEY = "unit-failed"
LOCK_WAIT_S = 10          # how long to wait for notify.lock before recording nothing (the item is added anyway)
RECENT_S = 12 * 3600      # a last-run record older than this belongs to an earlier run (the unit's own limit is 9 h)


def failed_day(now: dt.datetime) -> tuple[str, bool]:
    """(day, from_record): the day the failed run SELECTED, from the last-run record run.py wrote at its start, when
    the record is readable, has a real day and started within RECENT_S; else the due day by the clock (a run that died
    before run.py started, or no record) with from_record False."""
    rec = _read_strict(paths.last_run_path())
    if isinstance(rec, dict):
        try:
            day = dt.date.fromisoformat(rec.get("day"))
            started = dt.datetime.fromisoformat(str(rec.get("started_at")).replace("Z", "+00:00"))
            if started.tzinfo is not None and 0 <= (now - started).total_seconds() <= RECENT_S:
                return day.isoformat(), True
        except (TypeError, ValueError):
            pass
    return due_day(now).isoformat(), False


def _settings():
    """The validated settings with their time zone in force, or None when they cannot be loaded."""
    try:
        s = load_settings(paths.config_dir())
        use_timezone(s.timezone)
        return s
    except (SettingsError, ValueError):
        return None


def main(argv=None, runner=subprocess.run, now: dt.datetime | None = None) -> int:
    settings = _settings()
    now = now or dt.datetime.now(zone())
    day, from_record = failed_day(now)
    key = f"{notify.KEY_PREFIX}:unit-failed:{day}"
    item = {"key": key, "kind": "approval", "outcome": "unit-failed", "day": day,
            "title": TITLE if from_record else TITLE + GUESS_NOTE, "pointer": POINTER, "severity": None,
            "rerun": None, "meta": {}}
    try:
        rc, out = notify.send({"op": "add", "item": item}, settings, runner)
    except subprocess.TimeoutExpired:
        sys.stderr.write("unit_failed: the notify command timed out\n")
        return 1
    except Exception as e:
        sys.stderr.write(f"unit_failed: the notify command could not run ({type(e).__name__})\n")
        return 1
    if getattr(settings, "notify_command", ()) and out:
        sys.stdout.write(out)
    if rc != 0:
        sys.stderr.write(f"unit_failed: the notify command exited {rc}\n")
        return rc
    iid, state = notify.answer(out)
    if iid and state in ("open", "acked"):
        work = paths.state_root() / day
        target = work / notify.RECEIPT
        try:   # the same lock the notify stage holds across its own read-modify-write of this file
            work.mkdir(parents=True, exist_ok=True)
            with files.file_lock(work / notify.LOCK, LOCK_WAIT_S):
                rec = _read_strict(target) if target.exists() else {}
                if isinstance(rec, dict):
                    rec[RECORD_KEY] = {"sent": "ok", "item_id": iid, "at": files.now_iso()}
                    files.write_json(target, rec)
        except files.LockBusy:
            sys.stderr.write("unit_failed_unrecorded: notify.lock is held\n")
        except OSError as e:
            sys.stderr.write(f"unit_failed_unrecorded: {e.strerror}\n")
    return 0


def _read_strict(path):
    """The parsed file, or None when it is unreadable (never overwritten)."""
    try:
        return files.read_json(path)
    except (OSError, ValueError, RecursionError):
        return None


if __name__ == "__main__":
    sys.exit(main())
