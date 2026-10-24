"""The OnFailure handler: its item is keyed by the day the failed run selected (else the pipeline's due day, the same
rule as run.py's --day due), it goes through the notify delivery (the command or the default sink), and the item id it
gets back is recorded in that day's notify.json so a later run that reports the day expires it like any other
superseded item."""
import datetime as dt
import fcntl
import hashlib
import json
import os
import subprocess
import threading
import time
from zoneinfo import ZoneInfo

import pytest

from daily import day as daymod
from daily import notify, paths, unit_failed

PT = ZoneInfo("America/Los_Angeles")   # the conftest settings' zone
SETTINGS = '''timezone = "America/Los_Angeles"

[show]
series = "Demo Agents on the Clock"
host_name = "Sam"
disclosure = "Narrated with an AI voice; agent lines are verbatim from logs."

[sources]
codex = false
github = false

[notify]
command = ["notify-hook"]
'''


def at(y, mo, d, h, mi=0):
    return dt.datetime(y, mo, d, h, mi, tzinfo=PT)


def item_id(key):
    return "i-" + hashlib.sha1(key.encode()).hexdigest()[:12]


class Hook:
    """A fake notify command: answers the add with an item id JSON (or `out`) and exit `rc`."""

    def __init__(self, rc=0, state="open", out=None):
        self.msgs, self.cmds, self.rc, self.state, self.out = [], [], rc, state, out

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        msg = json.loads(kw["input"])
        self.msgs.append(msg)
        key = msg["item"]["key"]
        out = self.out if self.out is not None else json.dumps({"item_id": item_id(key), "state": self.state})
        return subprocess.CompletedProcess(cmd, self.rc, stdout=out, stderr="")

    @property
    def item(self):
        return self.msgs[0]["item"]


@pytest.fixture
def state(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "settings.toml").write_text(SETTINGS)
    monkeypatch.setenv(paths.ENV_CONFIG, str(cfg))
    monkeypatch.setenv(paths.ENV_STATE, str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    return tmp_path / "state"


@pytest.mark.parametrize("now,due", [(at(2026, 10, 19, 6), "2026-10-18"), (at(2026, 10, 19, 3), "2026-10-17"),
                                     (at(2026, 10, 19, 5, 29), "2026-10-17"), (at(2026, 10, 19, 5, 30), "2026-10-18")])
def test_the_key_uses_the_due_day_not_the_wall_clock_date(state, now, due):
    h = Hook()
    assert unit_failed.main(runner=h, now=now) == 0
    assert h.item["key"] == f"daily:unit-failed:{due}"
    assert due == daymod.due_day(now).isoformat()


def test_the_item_shape_and_the_command(state):
    h = Hook()
    unit_failed.main(runner=h, now=at(2026, 10, 19, 6))
    assert h.cmds == [["notify-hook"]] and h.msgs[0]["op"] == "add" and h.msgs[0]["schema"] == notify.SCHEMA
    i = h.item
    assert (i["kind"], i["outcome"], i["day"]) == ("approval", "unit-failed", "2026-10-18")
    assert i["pointer"] == "journalctl --user -u orion-on-the-clock"
    assert "journalctl --user -u orion-on-the-clock" in i["title"]


def test_the_item_id_is_recorded_in_that_days_notify_json(state):
    unit_failed.main(runner=Hook(), now=at(2026, 10, 19, 6))
    rec = json.loads((state / "2026-10-18" / notify.RECEIPT).read_text())
    assert rec["unit-failed"]["sent"] == "ok" and rec["unit-failed"]["item_id"] == item_id("daily:unit-failed:2026-10-18")


def test_existing_entries_are_kept(state):
    d = state / "2026-10-18"
    d.mkdir()
    (d / notify.RECEIPT).write_text(json.dumps({"daily:2026-10-18:ready": {"sent": "ok", "item_id": "i-aaaaaaaaaaaa"}}))
    unit_failed.main(runner=Hook(), now=at(2026, 10, 19, 6))
    assert set(json.loads((d / notify.RECEIPT).read_text())) == {"daily:2026-10-18:ready", "unit-failed"}


def test_an_unreadable_receipt_is_not_overwritten_and_the_item_still_counts(state):
    d = state / "2026-10-18"
    d.mkdir()
    (d / notify.RECEIPT).write_text("{not json")
    assert unit_failed.main(runner=Hook(), now=at(2026, 10, 19, 6)) == 0
    assert (d / notify.RECEIPT).read_text() == "{not json"


def test_a_failed_send_fails_and_records_nothing(state):
    assert unit_failed.main(runner=Hook(rc=7), now=at(2026, 10, 19, 6)) == 7
    assert not (state / "2026-10-18" / notify.RECEIPT).exists()


@pytest.mark.parametrize("exc,code", [(subprocess.TimeoutExpired(["x"], 1), 1), (FileNotFoundError("x"), 1)])
def test_a_command_that_cannot_run_or_times_out_exits_1(state, exc, code, capsys):
    def runner(cmd, **kw):
        raise exc
    assert unit_failed.main(runner=runner, now=at(2026, 10, 19, 6)) == code
    assert "unit_failed:" in capsys.readouterr().err


@pytest.mark.parametrize("out", ["", "not json", "{}", json.dumps({"item_id": "bad id", "state": "open"}),
                                 json.dumps({"item_id": "i-aaaaaaaaaaaa", "state": "expired"})])
def test_an_answer_without_a_usable_open_item_records_nothing_but_the_add_still_counts(state, out):
    assert unit_failed.main(runner=Hook(out=out), now=at(2026, 10, 19, 6)) == 0
    assert not (state / "2026-10-18" / notify.RECEIPT).exists()


# ---- the day the run SELECTED, and a lock shared with notify ----

def record(state, day, started):
    (state / "last-run.json").write_text(json.dumps({"day": day, "started_at": started, "pid": 1}))


def z(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_catch_up_run_selected_for_n_minus_2_that_fails_after_0530_files_under_n_minus_2(state):
    started = at(2026, 10, 19, 4, 50)                     # Persistent catch-up before 05:30: selects 2026-10-17
    record(state, "2026-10-17", z(started))
    now = at(2026, 10, 19, 5, 45)                         # it dies after 05:30, when due_day() has become 2026-10-18
    assert daymod.due_day(now).isoformat() == "2026-10-18"
    h = Hook()
    assert unit_failed.main(runner=h, now=now) == 0
    assert h.item["key"] == "daily:unit-failed:2026-10-17"
    assert "guess" not in h.item["title"]
    assert (state / "2026-10-17" / notify.RECEIPT).exists() and not (state / "2026-10-18").exists()


@pytest.mark.parametrize("make", ["missing", "garbage", "bad_day", "no_day", "stale", "bad_started"])
def test_without_a_usable_record_it_falls_back_to_the_due_day_and_says_so(state, make):
    now = at(2026, 10, 19, 6)
    if make == "garbage":
        (state / "last-run.json").write_text("{nope")
    elif make == "bad_day":
        record(state, "2026-13-45", z(now))
    elif make == "no_day":
        (state / "last-run.json").write_text(json.dumps({"started_at": z(now)}))
    elif make == "stale":
        record(state, "2026-10-17", z(now - dt.timedelta(hours=13)))     # an earlier run: this one died before run.py
    elif make == "bad_started":
        record(state, "2026-10-17", "yesterday")
    h = Hook()
    assert unit_failed.main(runner=h, now=now) == 0
    assert h.item["key"] == "daily:unit-failed:2026-10-18"
    assert "day guessed" in h.item["title"]


def hold(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_a_held_lock_never_loses_the_item_but_records_nothing(state, monkeypatch, capsys):
    monkeypatch.setattr(unit_failed, "LOCK_WAIT_S", 0.3)
    fd = hold(state / "2026-10-18" / notify.LOCK)
    try:
        h = Hook()
        assert unit_failed.main(runner=h, now=at(2026, 10, 19, 6)) == 0
    finally:
        os.close(fd)
    assert len(h.msgs) == 1                                             # the item was still added
    assert not (state / "2026-10-18" / notify.RECEIPT).exists()
    assert "unit_failed_unrecorded" in capsys.readouterr().err


def test_concurrent_writers_keep_both_entries(state, monkeypatch):
    monkeypatch.setattr(unit_failed, "LOCK_WAIT_S", 10)
    d = state / "2026-10-18"
    fd = hold(d / notify.LOCK)

    def notify_writes_then_releases():
        time.sleep(0.4)
        (d / notify.RECEIPT).write_text(json.dumps({"daily:2026-10-18:ready": {"sent": "ok", "item_id": "i-aaaaaaaaaaaa"}}))
        os.close(fd)
    t = threading.Thread(target=notify_writes_then_releases)
    t.start()
    assert unit_failed.main(runner=Hook(), now=at(2026, 10, 19, 6)) == 0
    t.join()
    assert set(json.loads((d / notify.RECEIPT).read_text())) == {"daily:2026-10-18:ready", "unit-failed"}


# ---- no command, or no settings at all: the default sink ----

def test_without_a_command_the_item_lands_in_the_state_folder_and_is_printed(state, capsys):
    cfg = paths.config_dir()
    (cfg / "settings.toml").write_text(SETTINGS.replace('command = ["notify-hook"]', "command = []"))
    assert unit_failed.main(runner=lambda *a, **k: pytest.fail("ran a command"), now=at(2026, 10, 19, 6)) == 0
    iid = notify.sink_id("daily:unit-failed:2026-10-18")
    item = json.loads((state / notify.SINK_DIR / f"{iid}.json").read_text())
    assert item["state"] == "open" and item["outcome"] == "unit-failed"
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["item_id"] == iid
    assert json.loads((state / "2026-10-18" / notify.RECEIPT).read_text())["unit-failed"]["item_id"] == iid


def test_settings_that_cannot_load_still_report_through_the_default_sink(state, capsys):
    (paths.config_dir() / "settings.toml").write_text("timezone = \n")     # broken: maybe what killed the run
    record(state, "2026-10-17", z(at(2026, 10, 19, 5)))
    assert unit_failed.main(runner=lambda *a, **k: pytest.fail("ran a command"), now=at(2026, 10, 19, 6)) == 0
    iid = notify.sink_id("daily:unit-failed:2026-10-17")
    assert (state / notify.SINK_DIR / f"{iid}.json").is_file()
