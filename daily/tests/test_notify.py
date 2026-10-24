"""Notify stage: the run's outcome from status.json, one item per (day, outcome), delivered to the operator's command
or written to the state folder and printed, superseded by a later outcome, never resent once it was accepted."""
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from daily import gate, notify, paths
from daily.settings import Adapter, Settings
from daily.stage import Ctx, StageResult

DAY = dt.date(2026, 10, 16)
VID = "abcdefghijk"
URL = f"https://studio.youtube.com/video/{VID}/edit"
WATCH = f"https://youtube.com/shorts/{VID}"
DASHES = (chr(0x2013), chr(0x2014), chr(0x2015))
ALL_OK = {h: "ok" for h in ("claude_code", "codex", "night_bot")}
DONE = dict(collect="done", write="done", gate="done", voice="done", render="done", publish="done")
NREQ = len(gate.REQUIRED)
HOOK = ("notify-hook", "--json")
SETTINGS = Settings(timezone="America/Los_Angeles", series="Demo", host_name="Sam", disclosure="Synthetic voice.",
                    github=False, notify_command=HOOK,
                    adapters=(Adapter("night_bot", "Night Bot", Path("/nonexistent/night_bot.py")),))
SINK = dataclasses.replace(SETTINGS, notify_command=())


def sha(b):
    return hashlib.sha256(b).hexdigest()


def status(reasons=None, **stages):
    reasons = reasons or {}
    return {"day": "2026-10-16",
            "stages": {k: {"state": v, "reason": reasons.get(k, ""), "at": "x"} for k, v in stages.items()}}


def build(work, title="Two agents, one browser pool", coverage=None):
    """A work dir whose text verdict passes the gate, plus a private_verified receipt and render evidence."""
    script_b = json.dumps({"day": "2026-10-16", "quiet": False, "title": title}).encode()
    ledger_b = json.dumps({"day": "2026-10-16", "coverage": coverage or ALL_OK}).encode()
    (work / "script.json").write_bytes(script_b)
    (work / "ledger.json").write_bytes(ledger_b)
    (work / "verdict.json").write_text(json.dumps(
        {"schema": gate.VERDICT_SCHEMA, "subject_sha256": sha(script_b), "ledger_sha256": sha(ledger_b), "pass": True,
         "dry": False, "checks": [{"name": n, "pass": True} for n in gate.REQUIRED]}))
    (work / "render.json").write_text(json.dumps({"checks": [
        {"name": "duration", "pass": True, "evidence": "60.02 s (timeline 60.0 s; allowed 45 to 75 s)"},
        {"name": "loudness", "pass": True, "evidence": "-14.2 LUFS integrated (allowed -16 to -13 LUFS)"},
        {"name": "format", "pass": True, "evidence": "h264"}]}))
    (work / "upload.json").write_text(json.dumps(
        {"day": "2026-10-16", "stage": "private_verified", "video_id": VID, "studio_url": URL}))
    return work


@pytest.fixture
def work(tmp_path):
    w = tmp_path / "2026-10-16"
    w.mkdir()
    return build(w)


def write_status(work, st):
    (work / "status.json").write_text(json.dumps(st))


def ctx(work, dry=False, settings=SETTINGS):
    return Ctx(day=DAY, work=work, cfg=None, dry=dry, settings=settings)


def item_id(key):
    return "i-" + hashlib.sha1(key.encode()).hexdigest()[:12]


class Runner:
    """A fake notify command: an add answers the item id JSON, an expire answers its exit code."""

    def __init__(self, rc=0, expire_rc=0, state="open"):
        self.calls, self.rc, self.expire_rc, self.state = [], rc, expire_rc, state

    def __call__(self, cmd, **kw):
        msg = json.loads(kw["input"])
        self.calls.append((cmd, kw, msg))
        if msg["op"] == "expire":
            return subprocess.CompletedProcess(cmd, self.expire_rc, stdout="{}", stderr="x" if self.expire_rc else "")
        out = json.dumps({"item_id": item_id(msg["item"]["key"]), "state": self.state})
        return subprocess.CompletedProcess(cmd, self.rc, stdout=out, stderr="refused: x" if self.rc else "")

    @property
    def adds(self):
        return [m["item"] for _, _, m in self.calls if m["op"] == "add"]

    @property
    def expires(self):
        return [m for _, _, m in self.calls if m["op"] == "expire"]


def sent(work, reasons=None, **st):
    write_status(work, status(reasons, **st))
    r = Runner()
    res = notify.run_stage(ctx(work), runner=r)
    assert res.ok, res
    (item,) = r.adds
    return item


def rerun(stage):
    return f"{sys.executable} {notify.RUN_PY} --day 2026-10-16 --from-stage {stage}"


def alltext(item):
    return json.dumps(item, ensure_ascii=False)


# --- outcome -------------------------------------------------------------------------------------------------------

def test_outcome_classes():
    assert notify.outcome(status(**DONE))["outcome"] == "ready"
    q = notify.outcome(status(collect="done", write="done", gate="quiet"))
    assert (q["outcome"], q["stage"]) == ("quiet", "gate")
    f = notify.outcome(status({"write": "write_failed"}, collect="done", write="failed"))
    assert (f["outcome"], f["stage"], f["reason"]) == ("failed", "write", "write_failed")
    assert notify.outcome(status({"collect": "config:missing"}, collect="failed"))["outcome"] == "config_error"
    for code in ("needs_you:receipt_other_render", "needs_you"):
        assert notify.outcome(status({"publish": code}, **{**DONE, "publish": "failed"}))["outcome"] == "needs_you"
    assert notify.outcome(status({"publish": "browser_busy"}, **{**DONE, "publish": "failed"}))["outcome"] == "failed"
    assert notify.outcome(status({"publish": "not_installed"}, **{**DONE, "publish": "skipped"}))["outcome"] == "failed"
    assert notify.outcome(status(collect="done", write="done", gate="done"))["outcome"] == "incomplete"
    assert notify.outcome(status(**{**DONE, "publish": "dry"}))["outcome"] == "incomplete"
    assert notify.outcome({})["outcome"] == "incomplete"
    assert notify.outcome({"stages": "x"})["outcome"] == "incomplete"


def test_a_collect_failure_that_is_not_a_config_code_is_a_plain_failure():
    assert notify.outcome(status({"collect": "exception:OSError"}, collect="failed"))["outcome"] == "failed"


# --- the texts -----------------------------------------------------------------------------------------------------

def test_ready_is_an_approval_with_the_studio_url_and_render_evidence(work):
    i = sent(work, **DONE)
    assert i["kind"] == "approval" and i["outcome"] == "ready" and i["day"] == "2026-10-16"
    assert i["key"] == "daily:2026-10-16:ready"
    assert i["title"] == "Daily episode 2026-10-16: ready in Studio (private), review and publish or delete"
    assert i["pointer"] == URL
    assert i["meta"] == {"episode": "Two agents, one browser pool", "duration": "60.0 s", "loudness": "-14.2 LUFS",
                         "checks": f"text gate {NREQ}/{NREQ}, render gate 3/3"}
    assert i["severity"] is None and i["rerun"] is None


def test_quiet_is_fyi_and_demoted(work):
    i = sent(work, collect="done", write="quiet")
    assert i["kind"] == "fyi" and i["severity"] == "fyi"
    assert i["key"] == "daily:2026-10-16:quiet"
    assert i["title"] == "Daily episode 2026-10-16: quiet day, no episode"
    assert i["pointer"] == str(work) and i["meta"] == {}


def test_stopped_is_fyi_with_the_stage_the_reason_code_and_the_rerun_command(work):
    i = sent(work, {"gate": "text_check_failed"}, collect="done", write="done", gate="failed")
    assert i["kind"] == "fyi" and i["key"] == "daily:2026-10-16:failed-gate"
    assert i["title"] == "Daily episode 2026-10-16: stopped at gate (text_check_failed)"
    assert i["rerun"] == rerun("gate") and i["pointer"] == str(work)


def test_config_error_says_nothing_ran(work):
    i = sent(work, {"collect": "config:missing_lanes"}, collect="failed")
    assert i["kind"] == "fyi" and i["key"] == "daily:2026-10-16:config_error"
    assert i["title"] == "Daily episode 2026-10-16: config error (config:missing_lanes), nothing ran"
    assert i["rerun"] == rerun("collect")


def test_needs_you_is_an_approval_phrased_as_the_ask_with_the_work_dir_pointer(work):
    i = sent(work, {"publish": "needs_you:receipt_other_render"}, **{**DONE, "publish": "failed"})
    assert i["kind"] == "approval" and i["key"] == "daily:2026-10-16:needs_you"
    assert i["title"] == "Daily episode 2026-10-16: needs you before publishing (needs_you:receipt_other_render)"
    assert i["pointer"] == str(work) and i["rerun"] is None and i["severity"] is None


def test_no_text_has_a_long_dash(work):
    for st in (DONE, dict(collect="done", write="quiet"), dict(collect="done", write="failed")):
        (work / notify.RECEIPT).unlink(missing_ok=True)
        text = alltext(sent(work, **st))
        assert not any(c in text for c in DASHES)


def test_the_rerun_command_runs_the_orchestrator_by_its_absolute_path():
    assert notify.RUN_PY == paths.PACKAGE / "run.py" and notify.RUN_PY.is_file() and notify.RUN_PY.is_absolute()


# --- untrusted text stays out --------------------------------------------------------------------------------------

def test_reason_text_that_is_not_a_code_is_left_out(work):
    for bad in ("Traceback in /home/someone/secret.py", "ledger said: Sam sent 5 emails", "x" * 90, "a b"):
        (work / notify.RECEIPT).unlink(missing_ok=True)
        i = sent(work, {"gate": bad}, collect="done", write="done", gate="failed")
        assert i["title"] == "Daily episode 2026-10-16: stopped at gate"
        assert bad not in alltext(i)


def test_episode_title_is_cleaned_and_dropped_when_unsafe(tmp_path):
    cases = [("Sam" + chr(0x2019) + "s " + chr(0x2018) + "own" + chr(0x2019) + " run", "Sam's 'own' run"),
             ("Agents `run it` " + DASHES[1] + " " + chr(0x201c) + "fast" + chr(0x201d), "Agents run it - fast"),
             ("Ping sam@example.com now", None), ("Call 555-123-4567", None),
             ("See /home/someone/x", "See home someone x"), ("t" * 150, "t" * 80)]
    for n, (raw, want) in enumerate(cases):
        w = tmp_path / f"d{n}"
        w.mkdir()
        build(w, title=raw)
        i = sent(w, **DONE)
        assert i["meta"].get("episode") == want, raw
        assert not any(c in alltext(i) for c in DASHES + ("`",))


def test_title_comes_only_from_a_gate_verified_script(work):
    (work / "script.json").write_text(json.dumps({"day": "2026-10-16", "title": "Tampered"}))   # verdict no longer binds
    i = sent(work, **DONE)
    assert "episode" not in i["meta"] and "Tampered" not in alltext(i)


def test_a_foreign_studio_url_in_the_receipt_is_never_forwarded(work):
    (work / "upload.json").write_text(json.dumps(
        {"day": "2026-10-16", "stage": "private_verified", "video_id": VID, "studio_url": "https://evil.example/x"}))
    assert sent(work, **DONE)["pointer"] == URL


def test_a_missing_or_wrong_receipt_fails_the_ready_item_and_sends_nothing(work):
    for bad in ({"day": "2026-10-15", "stage": "private_verified", "video_id": VID},
                {"day": "2026-10-16", "stage": "uploaded", "video_id": VID},
                {"day": "2026-10-16", "stage": "private_verified", "video_id": "../etc"},
                {"day": "2026-10-16", "stage": "private_verified"}, "text"):
        (work / "upload.json").write_text(json.dumps(bad))
        write_status(work, status(**DONE))
        r = Runner()
        res = notify.run_stage(ctx(work), runner=r)
        assert not res.ok and res.reason == "upload_receipt_invalid" and r.calls == []
    (work / "upload.json").unlink()
    assert notify.run_stage(ctx(work), runner=Runner()).reason == "upload_receipt_invalid"


def test_render_numbers_must_look_like_numbers(work):
    (work / "render.json").write_text(json.dumps({"checks": [
        {"name": "duration", "pass": True, "evidence": "60 s <script>"},
        {"name": "loudness", "pass": True, "evidence": "loud LUFS"}]}))
    m = sent(work, **DONE)["meta"]
    assert "loudness" not in m and m["duration"] == "60.0 s"


# --- coverage ------------------------------------------------------------------------------------------------------

def test_an_unavailable_agent_is_named_by_display_name(tmp_path):
    w = tmp_path / "d"
    w.mkdir()
    build(w, coverage=dict(ALL_OK, codex="unavailable:db_locked", night_bot="unavailable:adapter_failed"))
    i = sent(w, **DONE)
    assert i["meta"]["unavailable"] == "Codex (db_locked); Night Bot (adapter_failed)"
    assert i["title"].startswith("Daily episode 2026-10-16: ready")   # the title stays fixed


def test_coverage_note_shapes(work):
    (work / "ledger.json").write_text(json.dumps({"coverage": {
        "codex": "unavailable:" + "x" * 80 + " <b>", "Night_Bot!": "unavailable:ok", "night_bot": "weird",
        "mystery": "unavailable", "claude_code": "ok"}}))
    display = SETTINGS.harness_display()
    assert notify.coverage_note(work, display) == "Codex; Night Bot (unknown state); unknown agent"
    assert notify.coverage_note(work, {}) == "unknown agent"


def test_all_agents_ok_or_no_ledger_means_no_coverage_note(work):
    assert notify.coverage_note(work, SETTINGS.harness_display()) == ""
    (work / "ledger.json").unlink()
    assert notify.coverage_note(work, SETTINGS.harness_display()) == ""
    write_status(work, status({"collect": "config:x"}, collect="failed"))   # a config error writes no ledger
    r = Runner()
    assert notify.run_stage(ctx(work), runner=r).ok and r.adds[0]["meta"] == {}


# --- dedupe and receipts -------------------------------------------------------------------------------------------

def test_the_same_outcome_is_sent_once(work):
    write_status(work, status(collect="done", write="quiet"))
    r = Runner()
    assert notify.run_stage(ctx(work), runner=r).reason == "notified: quiet"
    assert notify.run_stage(ctx(work), runner=r).reason == "already notified: quiet"
    assert len(r.calls) == 1
    assert json.loads((work / notify.RECEIPT).read_text())["daily:2026-10-16:quiet"]["sent"] == "ok"


def test_the_reasons_carry_the_reported_prefixes_run_py_exits_5_on():
    assert paths.NOTIFY_REPORTED == ("notified", "already notified", "closed outside")


def test_a_later_outcome_for_the_same_day_is_its_own_item(work):
    r = Runner()
    write_status(work, status(collect="done", write="failed"))
    notify.run_stage(ctx(work), runner=r)
    write_status(work, status(collect="done", write="done", gate="failed"))   # a different stage stopped it
    notify.run_stage(ctx(work), runner=r)
    write_status(work, status(**DONE))
    notify.run_stage(ctx(work), runner=r)
    assert [i["key"] for i in r.adds] == [
        "daily:2026-10-16:failed-write", "daily:2026-10-16:failed-gate", "daily:2026-10-16:ready"]


def test_a_refusal_fails_the_stage_without_a_receipt_and_is_retried(work):
    write_status(work, status(collect="done", write="quiet"))
    res = notify.run_stage(ctx(work), runner=Runner(rc=2))
    assert not res.ok and res.reason == "notify_exit_2" and not (work / notify.RECEIPT).exists()
    r = Runner()
    assert notify.run_stage(ctx(work), runner=r).ok and len(r.adds) == 1


def test_a_command_that_cannot_run_or_times_out_fails_with_a_code(work):
    write_status(work, status(collect="done", write="quiet"))

    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    def missing(cmd, **kw):
        raise FileNotFoundError("/secret/path")

    assert notify.run_stage(ctx(work), runner=timeout).reason == "notify_timeout"
    assert notify.run_stage(ctx(work), runner=missing).reason == "notify_not_runnable"


def test_a_corrupt_receipt_is_treated_as_empty(work):
    write_status(work, status(collect="done", write="quiet"))
    (work / notify.RECEIPT).write_text("{not json")
    r = Runner()
    assert notify.run_stage(ctx(work), runner=r).ok and len(r.calls) == 1


def test_nothing_to_report_yet_sends_nothing(work):
    write_status(work, status(collect="done", write="done", gate="done"))
    r = Runner()
    assert notify.run_stage(ctx(work), runner=r) == StageResult(ok=True, reason="nothing_to_report")
    assert r.calls == []


def test_dry_run_sends_nothing(work, tmp_path, monkeypatch):
    monkeypatch.setenv(paths.ENV_STATE, str(tmp_path / "state"))
    write_status(work, status(collect="failed"))
    r = Runner()
    assert notify.run_stage(ctx(work, dry=True), runner=r) == StageResult(ok=True, reason="dry run: nothing sent")
    assert notify.run_stage(ctx(work, dry=True, settings=SINK), runner=r).ok
    assert r.calls == [] and not (tmp_path / "state").exists() and not (work / notify.RECEIPT).exists()


def test_no_settings_fails_closed(work):
    write_status(work, status(collect="done", write="quiet"))
    r = Runner()
    assert notify.run_stage(ctx(work, settings=None), runner=r) == StageResult(ok=False, reason="settings_missing")
    assert r.calls == []


# --- the command contract ------------------------------------------------------------------------------------------

def test_the_command_gets_one_json_message_on_stdin_with_no_shell_and_a_timeout(work):
    write_status(work, status(**DONE))
    r = Runner()
    notify.run_stage(ctx(work), runner=r)
    (cmd, kw, msg), = r.calls
    assert cmd == list(HOOK) and "shell" not in kw
    assert kw["timeout"] == notify.TIMEOUT_S == 60 and kw["capture_output"] is True and kw["text"] is True
    assert msg["schema"] == "orion-on-the-clock/notify/v1" and msg["op"] == "add"
    assert set(msg["item"]) == {"key", "kind", "outcome", "day", "title", "pointer", "severity", "rerun", "meta"}


def test_the_source_sends_nothing_anywhere_by_itself():
    src = Path(notify.__file__).read_text()
    for needle in ("urlopen", "requests", "smtplib", "socket", "shell=True", "os.system"):
        assert needle not in src, needle
    assert not any(c in src for c in DASHES)


# --- superseding, revisions, closed outside -------------------------------------------------------------------------

def test_the_raw_render_float_duration_is_reported_to_one_decimal(work):
    for raw, want in (("61.733333 s (timeline 61.7 s; allowed 45 to 75 s)", "61.7 s"), ("59.96 s (x)", "60.0 s"),
                      ("75 s (x)", "75.0 s"), ("0.5 s (x)", "0.5 s")):
        (work / notify.RECEIPT).unlink(missing_ok=True)
        (work / "render.json").write_text(json.dumps({"checks": [{"name": "duration", "pass": True, "evidence": raw}]}))
        assert sent(work, **DONE)["meta"]["duration"] == want, raw
    for bad in ("s (x)", "1.2.3 s", "about 61 s", "61s", "-5 s", "1e3 s", " 61 s"):
        (work / notify.RECEIPT).unlink(missing_ok=True)
        (work / "render.json").write_text(json.dumps({"checks": [{"name": "duration", "pass": True, "evidence": bad}]}))
        assert "duration" not in sent(work, **DONE)["meta"], bad


def test_a_raw_float_loudness_is_reported_to_one_decimal(work):
    (work / "render.json").write_text(json.dumps({"checks": [
        {"name": "loudness", "pass": True, "evidence": "-14.2345678 LUFS integrated (allowed -16 to -13)"}]}))
    assert sent(work, **DONE)["meta"]["loudness"] == "-14.2 LUFS"


def test_ready_stores_the_item_id_and_a_later_outcome_expires_it(work):
    r = Runner()
    write_status(work, status(**DONE))
    notify.run_stage(ctx(work), runner=r)
    first = json.loads((work / notify.RECEIPT).read_text())["daily:2026-10-16:ready"]
    assert first["item_id"] == item_id("daily:2026-10-16:ready") and r.expires == []
    write_status(work, status({"publish": "needs_you"}, **{**DONE, "publish": "failed"}))
    assert notify.run_stage(ctx(work), runner=r).ok
    assert [(m["item_id"], m["reason"], m["key"]) for m in r.expires] == [
        (first["item_id"], "superseded", "daily:2026-10-16:ready")]
    rec = json.loads((work / notify.RECEIPT).read_text())
    assert rec["daily:2026-10-16:ready"]["expired"] is True
    assert rec["daily:2026-10-16:needs_you"].get("expired") is not True
    notify.run_stage(ctx(work), runner=r)                       # nothing new: no second expire, no second add
    assert len(r.expires) == 1 and len(r.adds) == 2


def test_the_new_item_is_added_before_the_old_one_is_expired(work):
    r = Runner()
    write_status(work, status(collect="done", write="failed"))
    notify.run_stage(ctx(work), runner=r)
    write_status(work, status(collect="done", write="done", gate="failed"))
    notify.run_stage(ctx(work), runner=r)
    assert [m["op"] for _, _, m in r.calls] == ["add", "add", "expire"]


def test_a_failed_expire_does_not_block_the_new_item_and_is_retried(work):
    write_status(work, status(collect="done", write="failed"))
    notify.run_stage(ctx(work), runner=Runner())
    write_status(work, status(collect="done", write="done", gate="failed"))
    bad = Runner(expire_rc=2)
    res = notify.run_stage(ctx(work), runner=bad)
    assert res.ok and len(bad.adds) == 1 and len(bad.expires) == 1
    rec = json.loads((work / notify.RECEIPT).read_text())
    assert rec["daily:2026-10-16:failed-write"].get("expired") is not True
    assert rec["daily:2026-10-16:failed-write"]["expire_failed"] is True
    good = Runner()
    assert notify.run_stage(ctx(work), runner=good).ok
    assert good.adds == [] and len(good.expires) == 1           # retried, the new item is not re-added
    assert json.loads((work / notify.RECEIPT).read_text())["daily:2026-10-16:failed-write"]["expired"] is True


def test_an_expire_that_raises_is_recorded_not_raised(work):
    write_status(work, status(collect="done", write="failed"))
    notify.run_stage(ctx(work), runner=Runner())
    write_status(work, status(collect="done", write="done", gate="failed"))

    class Boom(Runner):
        def __call__(self, cmd, **kw):
            if json.loads(kw["input"])["op"] == "expire":
                raise subprocess.TimeoutExpired(cmd, 1)
            return super().__call__(cmd, **kw)

    assert notify.run_stage(ctx(work), runner=Boom()).ok
    assert json.loads((work / notify.RECEIPT).read_text())["daily:2026-10-16:failed-write"].get("expired") is not True


def test_returning_to_an_expired_outcome_adds_a_revision_item(work):
    r = Runner()
    for st in (dict(collect="done", write="failed"), dict(collect="done", write="done", gate="failed"),
               dict(collect="done", write="failed"), dict(collect="done", write="done", gate="failed"),
               dict(collect="done", write="failed")):
        write_status(work, status(**st))
        notify.run_stage(ctx(work), runner=r)
    base = "daily:2026-10-16:failed-"
    assert [i["key"] for i in r.adds] == [base + "write", base + "gate", base + "write:r2", base + "gate:r2",
                                         base + "write:r3"]
    rec = json.loads((work / notify.RECEIPT).read_text())
    assert [bool(rec[base + k].get("expired")) for k in ("write", "write:r2", "write:r3")] == [True, True, False]
    notify.run_stage(ctx(work), runner=r)
    assert len(r.adds) == 5                                   # the live revision is already sent


def test_an_add_that_does_not_answer_open_or_acked_is_a_failure(work):
    write_status(work, status(collect="done", write="quiet"))
    for state in (None, "bogus", 5):
        r = Runner(state=state)
        res = notify.run_stage(ctx(work), runner=r)
        assert res.ok is False and res.reason.startswith("notify_"), state
        rec = json.loads((work / notify.RECEIPT).read_text()) if (work / notify.RECEIPT).exists() else {}
        assert rec.get("daily:2026-10-16:quiet", {}).get("sent") != "ok", state
        (work / notify.RECEIPT).unlink(missing_ok=True)
    assert notify.run_stage(ctx(work), runner=Runner(state="acked")).ok


def test_an_item_already_closed_outside_is_recorded_and_never_re_added(work):
    """The crash window: the add worked, the receipt write did not, the item was closed. Not a failure, no revision."""
    for state in ("expired", "done"):
        (work / notify.RECEIPT).unlink(missing_ok=True)
        write_status(work, status(collect="done", write="quiet"))
        r = Runner(state=state)
        res = notify.run_stage(ctx(work), runner=r)
        assert res.ok is True and res.reason == "closed outside: quiet", state
        rec = json.loads((work / notify.RECEIPT).read_text())
        assert rec["daily:2026-10-16:quiet"]["sent"] == "closed_outside"
        again = Runner()
        assert notify.run_stage(ctx(work), runner=again).ok and again.calls == [], state   # no revision, no loop


def test_a_closed_outside_item_is_not_expired_and_does_not_block_a_later_outcome(work):
    write_status(work, status(collect="done", write="quiet"))
    notify.run_stage(ctx(work), runner=Runner(state="done"))
    write_status(work, status(collect="done", write="done", gate="failed"))
    r = Runner()
    assert notify.run_stage(ctx(work), runner=r).ok
    assert r.expires == [] and len(r.adds) == 1


def test_a_key_we_recorded_and_then_superseded_still_gets_a_revision(work):
    r = Runner()
    write_status(work, status(collect="done", write="quiet"))
    notify.run_stage(ctx(work), runner=r)
    write_status(work, status(**DONE))
    notify.run_stage(ctx(work), runner=r)                       # quiet expired by us
    write_status(work, status(collect="done", write="quiet"))
    notify.run_stage(ctx(work), runner=r)
    assert r.adds[-1]["key"] == "daily:2026-10-16:quiet:r2"


def test_a_corrupt_item_id_in_the_receipt_is_never_expired(work):
    for bad in ("../x", "", "-leading", "a" * 65, "sp ace", 5, None):
        (work / notify.RECEIPT).write_text(json.dumps({"daily:2026-10-16:ready": {"sent": "ok", "item_id": bad}}))
        write_status(work, status(collect="done", write="quiet"))
        r = Runner()
        assert notify.run_stage(ctx(work), runner=r).ok and r.expires == [], bad


def test_a_separated_digit_title_is_dropped():
    assert notify.clean_title("Ref 5-5-5-1-2-3-4-5-6-7") is None


def test_the_evidence_length_is_capped(work):
    (work / "render.json").write_text(json.dumps({"checks": [
        {"name": "duration", "pass": True, "evidence": "61." + "5" * 400 + " s"}]}))
    assert "duration" not in sent(work, **DONE)["meta"]


def test_an_add_that_answers_no_json_is_a_failure_with_no_receipt(work):
    write_status(work, status(collect="done", write="quiet"))

    def odd(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr="")

    res = notify.run_stage(ctx(work), runner=odd)
    assert res.ok is False and res.reason == "notify_bad_answer" and not (work / notify.RECEIPT).exists()


def test_a_video_id_must_be_exactly_eleven_characters(work):
    for vid in ("abcdefghijkl", "abcdefghij"):
        (work / "upload.json").write_text(json.dumps({"day": "2026-10-16", "stage": "private_verified", "video_id": vid}))
        write_status(work, status(**DONE))
        assert notify.run_stage(ctx(work), runner=Runner()).reason == "upload_receipt_invalid"


def test_a_phone_number_made_by_cleaning_drops_the_title():
    assert notify.clean_title("Call 555/123/4567") is None        # clean text would read as a phone number
    assert notify.clean_title("Call 555 123 4567") is None
    assert notify.clean_title("Ship 12 agents") == "Ship 12 agents"


def test_only_a_true_pass_counts_in_the_check_totals(work):
    (work / "render.json").write_text(json.dumps({"checks": [
        {"name": "a", "pass": True}, {"name": "b", "pass": 1}, {"name": "c", "pass": "yes"}, "junk"]}))
    assert sent(work, **DONE)["meta"]["checks"].endswith("render gate 1/4")


def test_an_unwritable_receipt_fails_the_stage(work, monkeypatch):
    write_status(work, status(collect="done", write="quiet"))

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(notify.files, "write_json", boom)
    res = notify.run_stage(ctx(work), runner=Runner())
    assert res.ok is False and res.reason == "notify_receipt_unwritable"


# --- the default sink: the state folder and stdout -------------------------------------------------------------------

@pytest.fixture
def state(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv(paths.ENV_STATE, str(d))
    return d


def sink_items(state):
    return {p.stem: json.loads(p.read_text()) for p in sorted((state / notify.SINK_DIR).glob("*.json"))}


def test_without_a_command_the_item_is_written_under_the_state_folder_and_printed(work, state, capsys):
    write_status(work, status(**DONE))
    res = notify.run_stage(ctx(work, settings=SINK), runner=lambda *a, **k: pytest.fail("ran a command"))
    assert res == StageResult(ok=True, reason="notified: ready")
    items = sink_items(state)
    iid = notify.sink_id("daily:2026-10-16:ready")
    assert list(items) == [iid] and items[iid]["state"] == "open" and items[iid]["pointer"] == URL
    printed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert printed[-1]["item_id"] == iid and printed[-1]["key"] == "daily:2026-10-16:ready"
    assert json.loads((work / notify.RECEIPT).read_text())["daily:2026-10-16:ready"]["item_id"] == iid


def test_the_sink_keeps_exactly_one_open_item_the_latest_outcome(work, state):
    for st in (dict(collect="done", write="quiet"), DONE, dict(collect="done", write="quiet")):
        write_status(work, status(**st))
        assert notify.run_stage(ctx(work, settings=SINK)).ok
    items = sink_items(state)
    live = [i for i in items.values() if i["state"] == "open"]
    assert [i["key"] for i in live] == ["daily:2026-10-16:quiet:r2"]
    assert sorted(i["state"] for i in items.values()) == ["expired", "expired", "open"]
    assert all(i.get("reason") == "superseded" for i in items.values() if i["state"] == "expired")


def test_an_item_closed_by_hand_in_the_sink_is_closed_outside_and_not_reopened(work, state):
    write_status(work, status(collect="done", write="quiet"))
    iid = notify.sink_id("daily:2026-10-16:quiet")
    (state / notify.SINK_DIR).mkdir(parents=True)
    (state / notify.SINK_DIR / f"{iid}.json").write_text(json.dumps({"item_id": iid, "state": "done"}))
    assert notify.run_stage(ctx(work, settings=SINK)).reason == "closed outside: quiet"
    assert sink_items(state)[iid] == {"item_id": iid, "state": "done"}


def test_the_sink_ids_are_stable_and_safe():
    a = notify.sink_id("daily:2026-10-16:ready")
    assert a == notify.sink_id("daily:2026-10-16:ready") != notify.sink_id("daily:2026-10-17:ready")
    assert notify.ITEM_ID.fullmatch(a) and a.startswith("n-") and len(a) == 14


# ---- the contact sheet's path rides on the ready item ----

def test_ready_item_names_the_contact_sheet_by_absolute_path(work):
    (work / "contact-sheet.jpg").write_bytes(b"sheet")
    path = sent(work, **DONE)["meta"]["contact_sheet"]
    assert path == str(work.resolve() / "contact-sheet.jpg") and Path(path).is_absolute()


def test_no_contact_sheet_meta_when_the_file_is_missing(work):
    assert "contact_sheet" not in sent(work, **DONE)["meta"]


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_a_symlinked_or_non_regular_contact_sheet_is_never_named(work, tmp_path, kind):
    if kind == "symlink":
        outside = tmp_path / "elsewhere.jpg"
        outside.write_bytes(b"x")
        (work / "contact-sheet.jpg").symlink_to(outside)
    else:
        (work / "contact-sheet.jpg").mkdir()
    assert "contact_sheet" not in sent(work, **DONE)["meta"]


def test_only_the_ready_item_carries_the_contact_sheet(work):
    (work / "contact-sheet.jpg").write_bytes(b"sheet")
    i = sent(work, {"publish": "needs_you:signed_out"}, **{**DONE, "publish": "failed"})
    assert "contact_sheet" not in i["meta"]


# ---- a failed switch back to the home channel ----

def _receipt_with(work, **extra):
    rec = json.loads((work / "upload.json").read_text())
    rec.update(extra)
    (work / "upload.json").write_text(json.dumps(rec))


def test_ready_item_carries_the_note_when_the_browser_was_not_switched_back(work):
    _receipt_with(work, switched_back=False)
    assert sent(work, **DONE)["meta"]["browser"] == notify.SWITCH_NOTE


@pytest.mark.parametrize("extra", [{"switched_back": True}, {}, {"switched_back": "false"}, {"switched_back": None}])
def test_ready_item_has_no_note_when_switched_back_or_unrecorded(work, extra):
    _receipt_with(work, **extra)
    assert "browser" not in sent(work, **DONE)["meta"]


def test_the_note_has_no_long_dash():
    assert not any(c in notify.SWITCH_NOTE for c in DASHES)


# ---- the OnFailure item is superseded by a later report ----

def test_a_later_report_expires_the_unit_failed_item_recorded_for_the_day(work):
    (work / notify.RECEIPT).write_text(json.dumps(
        {"unit-failed": {"sent": "ok", "item_id": item_id("daily:unit-failed:2026-10-16"), "at": "x"}}))
    write_status(work, status(None, **DONE))
    r = Runner()
    assert notify.run_stage(ctx(work), runner=r).ok
    assert [m["item_id"] for m in r.expires] == [item_id("daily:unit-failed:2026-10-16")]
    rec = json.loads((work / notify.RECEIPT).read_text())
    assert rec["unit-failed"]["expired"] is True and rec["daily:2026-10-16:ready"]["sent"] == "ok"


# ---- notify.json read-modify-write is under the shared notify.lock ----

def test_notify_holds_the_shared_lock_across_its_send_and_the_write(work):
    held = []

    class Spy(Runner):
        def __call__(self, cmd, **kw):
            fd = os.open(work / notify.LOCK, os.O_RDWR)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held.append(False)
                except OSError:
                    held.append(True)
            finally:
                os.close(fd)
            return super().__call__(cmd, **kw)
    write_status(work, status(None, **DONE))
    assert notify.run_stage(ctx(work), runner=Spy()).ok
    assert held and all(held)


def test_notify_waits_a_bounded_time_for_the_lock_then_fails_without_an_item(work, monkeypatch):
    monkeypatch.setattr(notify, "LOCK_WAIT_S", 0.2)
    write_status(work, status(None, **DONE))
    fd = os.open(work / notify.LOCK, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        r = Runner()
        res = notify.run_stage(ctx(work), runner=r)
    finally:
        os.close(fd)
    assert res == StageResult(ok=False, reason="notify_busy") and r.calls == []
    assert not (work / notify.RECEIPT).exists()
    assert notify.run_stage(ctx(work), runner=Runner()).ok            # control: free again, it reports


# ---- a verified public upload: one FYI, nothing asked ----

def make_public(work, **extra):
    rec = {"day": "2026-10-16", "stage": "public_verified", "video_id": VID, "studio_url": URL, "visibility": "PUBLIC"}
    rec.update(extra)
    (work / "upload.json").write_text(json.dumps(rec))


def test_a_verified_public_upload_sends_one_fyi_published_item(work):
    make_public(work)
    write_status(work, status({"publish": f"uploaded_public:{VID}"}, **DONE))
    r = Runner()
    res = notify.run_stage(ctx(work), runner=r)
    assert res == StageResult(ok=True, reason="notified: published")
    (i,) = r.adds
    assert i["kind"] == "fyi" and i["key"] == "daily:2026-10-16:published" and i["outcome"] == "published"
    assert i["title"] == "Published: Two agents, one browser pool"
    assert i["pointer"] == WATCH
    assert i["meta"] == {"duration": "60.0 s", "loudness": "-14.2 LUFS"}
    assert i["severity"] is None and i["rerun"] is None
    assert notify.run_stage(ctx(work), runner=r).reason == "already notified: published" and len(r.adds) == 1


def test_the_published_item_is_decided_by_the_receipt_not_the_status_text(work):
    make_public(work)
    i = sent(work, **DONE)                          # a done publish whose reason says nothing (a reused stage)
    assert i["key"] == "daily:2026-10-16:published" and i["kind"] == "fyi"


def test_the_published_item_carries_the_switch_note(work):
    make_public(work, switched_back=False)
    assert sent(work, **DONE)["meta"]["browser"] == notify.SWITCH_NOTE


@pytest.mark.parametrize("raw", ["Ping sam@example.com now", "Call 555-123-4567", "<>{}"])
def test_an_unsafe_title_is_dropped_from_the_published_item(tmp_path, raw):
    w = tmp_path / "d"
    w.mkdir()
    build(w, title=raw)
    make_public(w)
    i = sent(w, **DONE)
    assert i["title"] == "Published: daily episode 2026-10-16"
    assert raw not in alltext(i) and "example.com" not in alltext(i) and "555" not in alltext(i)


def test_the_published_title_comes_only_from_a_gate_verified_script(work):
    make_public(work)
    (work / "script.json").write_text(json.dumps({"day": "2026-10-16", "title": "Tampered"}))
    i = sent(work, **DONE)
    assert i["title"] == "Published: daily episode 2026-10-16" and "Tampered" not in alltext(i)


def test_a_cleaned_title_is_used_in_the_published_title(tmp_path):
    w = tmp_path / "d"
    w.mkdir()
    build(w, title="Agents `run it` " + DASHES[1] + " fast")
    make_public(w)
    i = sent(w, **DONE)
    assert i["title"] == "Published: Agents run it - fast" and not any(c in alltext(i) for c in DASHES)


def test_a_foreign_link_in_a_public_receipt_is_never_forwarded(work):
    make_public(work, studio_url="https://evil.example/x", watch_url="https://evil.example/y")
    i = sent(work, **DONE)
    assert i["pointer"] == WATCH and "evil" not in alltext(i)


@pytest.mark.parametrize("bad", [{"day": "2026-10-15"}, {"video_id": "../etc"}, {"video_id": "abcdefghijkl"},
                                 {"video_id": None}])
def test_a_wrong_public_receipt_fails_the_item_and_sends_nothing(work, bad):
    make_public(work, **bad)
    write_status(work, status(**DONE))
    r = Runner()
    res = notify.run_stage(ctx(work), runner=r)
    assert res == StageResult(ok=False, reason="upload_receipt_invalid") and r.calls == []


def test_private_mode_keeps_the_ready_approval(work):
    i = sent(work, {"publish": f"uploaded_private:{VID}"}, **DONE)
    assert i["kind"] == "approval" and i["key"] == "daily:2026-10-16:ready" and i["pointer"] == URL


def test_public_unconfirmed_stays_an_approval(work):
    i = sent(work, {"publish": "needs_you:public_unconfirmed"}, **{**DONE, "publish": "failed"})
    assert i["kind"] == "approval" and i["key"] == "daily:2026-10-16:needs_you"
    assert i["title"] == "Daily episode 2026-10-16: needs you before publishing (needs_you:public_unconfirmed)"


def test_published_supersedes_the_earlier_needs_you_item_and_returns_as_a_revision(work):
    r = Runner()
    write_status(work, status({"publish": "needs_you:public_unconfirmed"}, **{**DONE, "publish": "failed"}))
    notify.run_stage(ctx(work), runner=r)
    make_public(work)
    write_status(work, status(**DONE))
    assert notify.run_stage(ctx(work), runner=r).reason == "notified: published"
    rec = json.loads((work / notify.RECEIPT).read_text())
    assert [m["item_id"] for m in r.expires] == [rec["daily:2026-10-16:needs_you"]["item_id"]]
    write_status(work, status({"publish": "needs_you"}, **{**DONE, "publish": "failed"}))
    notify.run_stage(ctx(work), runner=r)
    write_status(work, status(**DONE))
    notify.run_stage(ctx(work), runner=r)
    assert [i["key"] for i in r.adds] == ["daily:2026-10-16:needs_you", "daily:2026-10-16:published",
                                         "daily:2026-10-16:needs_you:r2", "daily:2026-10-16:published:r2"]


def test_a_published_item_already_closed_is_recorded_and_never_re_added(work):
    make_public(work)
    write_status(work, status(**DONE))
    res = notify.run_stage(ctx(work), runner=Runner(state="done"))
    assert res == StageResult(ok=True, reason="closed outside: published")
    again = Runner()
    assert notify.run_stage(ctx(work), runner=again).ok and again.calls == []


# ---- the published FYI needs a receipt that names PUBLIC, not the stage alone ----

@pytest.mark.parametrize("vis", [None, "PRIVATE", "public", "UNLISTED"])
def test_a_public_verified_receipt_without_public_sends_no_published_item(work, vis):
    rec = {"day": "2026-10-16", "stage": "public_verified", "video_id": VID}
    if vis is not None:
        rec["visibility"] = vis
    (work / "upload.json").write_text(json.dumps(rec))
    write_status(work, status(**DONE))
    r = Runner()
    assert notify.run_stage(ctx(work), runner=r) == StageResult(ok=False, reason="upload_receipt_invalid")
    assert r.calls == []


@pytest.mark.parametrize("vis", ["PUBLIC", "UNLISTED", None])
def test_a_private_verified_receipt_naming_another_visibility_is_invalid(work, vis):
    (work / "upload.json").write_text(json.dumps({"day": "2026-10-16", "stage": "private_verified", "video_id": VID,
                                                  "visibility": vis}))
    write_status(work, status(**DONE))
    r = Runner()
    assert notify.run_stage(ctx(work), runner=r).reason == "upload_receipt_invalid" and r.calls == []


def test_a_private_verified_receipt_naming_private_is_ready(work):
    (work / "upload.json").write_text(json.dumps({"day": "2026-10-16", "stage": "private_verified", "video_id": VID,
                                                  "visibility": "PRIVATE"}))
    assert sent(work, **DONE)["key"] == "daily:2026-10-16:ready"
