"""Publish stage: the text verdict (read once, on the exact bytes) and the render verdict (accepts plus stage, day,
dry, script and ledger hashes) must both hold before any browser runs; the uploaded file is a staging copy whose hash
equals render.json's subject_sha256; dry never runs the child; the child's exit code becomes a short reason; public
mode only from publish.json saying exactly so."""
import dataclasses
import datetime as dt
import fcntl
import hashlib
import inspect
import json
import os
import shutil
import subprocess

import pytest

from daily import gate, notify, paths, publish
from daily.settings import Settings
from daily.stage import Ctx, StageResult

DAY = dt.date(2026, 10, 16)
VIDEO = b"video bytes"
VID = "abcdefghijk"
CHAN = "UC" + "c" * 22   # a synthetic channel id
DASHES = (chr(0x2013), chr(0x2014), chr(0x2015))
DISCLOSURE = "Narrated with an AI voice; agent lines are verbatim from logs."
SETTINGS = Settings(timezone="America/Los_Angeles", series="Demo Agents on the Clock", host_name="Sam",
                    disclosure=DISCLOSURE, channel_id=CHAN, github=False)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def put(work, name, obj):
    (work / name).write_text(json.dumps(obj))


def verdict(subject, ledger, required, **extra):
    v = {"schema": gate.VERDICT_SCHEMA, "subject_sha256": sha(subject), "ledger_sha256": sha(ledger), "pass": True,
         "checks": [{"name": n, "pass": True} for n in required]}
    v.update(extra)
    return v


def build(work, script=None, render=None, text=None, dry=False):
    """A work dir whose files pass both gates; overrides replace pieces. dry: a dry rehearsal folder (render-dry.json,
    episode-dry.mp4, render dry true)."""
    sfx = "-dry" if dry else ""
    script_obj = {"day": "2026-10-16", "quiet": False, "title": "Two agents, one browser pool",
                  "description": "What got built yesterday."}
    script_obj.update(script or {})
    script_b = json.dumps(script_obj).encode()
    ledger_b = json.dumps({"day": "2026-10-16"}).encode()
    (work / "script.json").write_bytes(script_b)
    (work / "ledger.json").write_bytes(ledger_b)
    (work / f"episode{sfx}.mp4").write_bytes(VIDEO)
    put(work, "verdict.json", {**verdict(script_b, ledger_b, gate.REQUIRED, dry=False), **(text or {})})
    r = verdict(VIDEO, ledger_b, gate.RENDER_REQUIRED, day="2026-10-16", stage="render", dry=dry,
                script_sha256=sha(script_b), voices_sha256=None, ref_sha256=None)
    r.update(render or {})
    put(work, f"render{sfx}.json", r)
    return work


@pytest.fixture(autouse=True)
def cfg_home(tmp_path, monkeypatch):
    """Never the real private config: every test gets its own config dir, with no publish.json (so private)."""
    d = tmp_path / "cfg-home"
    d.mkdir()
    monkeypatch.setenv(paths.ENV_CONFIG, str(d))
    return d


@pytest.fixture
def work(tmp_path):
    d = tmp_path / "2026-10-16"
    d.mkdir()
    return build(d)


@pytest.fixture
def dwork(tmp_path):
    """A dry rehearsal folder: what a dry render leaves (render-dry.json, episode-dry.mp4)."""
    d = tmp_path / "2026-10-16-dry"
    d.mkdir()
    return build(d, dry=True)


def ctx(work, dry=False, settings=SETTINGS):
    return Ctx(day=DAY, work=work, cfg=None, dry=dry, settings=settings)


def never_run(*a, **k):
    raise AssertionError("the browser child ran")


def ok_child(work, rc=0, out="step\nlast line\n", receipt=True, stage="private_verified", visibility=None):
    seen = []

    def runner(cmd, **kw):
        seen.append((cmd, kw))
        if rc == 0 and receipt:
            rec = {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": VID, "stage": stage}
            if visibility is not None:
                rec["visibility"] = visibility
            put(work, "upload.json", rec)
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")
    runner.seen = seen
    return runner


# ---- settings ----

def test_no_settings_fails_closed_before_anything(work):
    assert publish.run_stage(ctx(work, settings=None), runner=never_run) == StageResult(ok=False,
                                                                                         reason="settings_missing")


def test_a_real_run_without_a_channel_stops_before_the_child_and_writes_nothing(work):
    res = publish.run_stage(ctx(work, settings=dataclasses.replace(SETTINGS, channel_id=None)), runner=never_run)
    assert res == StageResult(ok=False, reason="missing:channel.id")
    assert not (work / publish.STAGING_NAME).exists() and not (work / "upload-item.json").exists()


def test_a_dry_run_needs_no_channel(dwork):
    res = publish.run_stage(ctx(dwork, dry=True, settings=dataclasses.replace(SETTINGS, channel_id=None)),
                            runner=never_run)
    assert res == StageResult(ok=True, reason="dry_run")
    assert json.loads((dwork / "upload-item.json").read_text())["channel"] is None


def test_a_verified_day_is_done_even_after_the_channel_setting_is_removed(work):
    put(work, "upload.json", {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": VID,
                              "stage": "private_verified"})
    res = publish.run_stage(ctx(work, settings=dataclasses.replace(SETTINGS, channel_id=None)), runner=never_run)
    assert res == StageResult(ok=True, reason=f"already_private:{VID}")


# ---- text verdict ----

def test_valid_inputs_upload_private(work):
    r = ok_child(work)
    res = publish.run_stage(ctx(work), runner=r)
    assert res == StageResult(ok=True, reason=f"uploaded_private:{VID}")
    assert len(r.seen) == 1


@pytest.mark.parametrize("mutate,reason", [
    (lambda w: (w / "script.json").write_bytes(b'{"title": "edited after the gate"}'), "text_not_accepted:sha_mismatch"),
    (lambda w: (w / "ledger.json").write_bytes(b'{"day": "2026-10-16", "x": 1}'), "text_not_accepted:ledger_mismatch"),
    (lambda w: (w / "verdict.json").unlink(), "text_not_accepted:unreadable:FileNotFoundError"),
])
def test_text_verdict_failures_stop_before_any_browser(work, mutate, reason):
    mutate(work)
    assert publish.run_stage(ctx(work), runner=never_run) == StageResult(ok=False, reason=reason)


def test_text_verdict_missing_a_required_check_stops(work):
    script_b = (work / "script.json").read_bytes()
    ledger_b = (work / "ledger.json").read_bytes()
    put(work, "verdict.json", verdict(script_b, ledger_b, gate.REQUIRED[:-1], dry=False))
    res = publish.run_stage(ctx(work), runner=never_run)
    assert not res.ok and res.reason.startswith("text_not_accepted:missing:")


def test_text_check_uses_gate_required_not_a_literal(dwork, monkeypatch):
    seen = []
    real = gate.read_verified_full
    monkeypatch.setattr(publish.gate, "read_verified_full",
                        lambda v, s, led, req: (seen.append(tuple(req)), real(v, s, led, req))[1])
    publish.run_stage(ctx(dwork, dry=True), runner=never_run)
    assert seen[0] == gate.REQUIRED


def test_title_and_description_come_from_the_exact_gated_bytes(dwork, monkeypatch):
    real = gate.read_verified_full

    def swap_after_read(v, s, led, req):
        out = real(v, s, led, req)
        (dwork / "script.json").write_text(json.dumps({"day": "2026-10-16", "quiet": False, "title": "SWAPPED",
                                                      "description": "swapped"}))
        return out
    monkeypatch.setattr(publish.gate, "read_verified_full", swap_after_read)
    publish.run_stage(ctx(dwork, dry=True), runner=never_run)
    item = json.loads((dwork / "upload-item.json").read_text())
    assert item["title"] == "Two agents, one browser pool"


@pytest.mark.parametrize("over,reason", [
    ({"title": "  "}, "no_title"), ({"title": 5}, "no_title"), ({"quiet": True}, "script_quiet"),
    ({"day": "2026-10-15"}, "script_day_mismatch")])
def test_script_content_refusals(tmp_path, over, reason):
    d = tmp_path / "2026-10-16"
    d.mkdir()
    build(d, script=over)
    assert publish.run_stage(ctx(d), runner=never_run) == StageResult(ok=False, reason=reason)


def test_script_not_an_object_is_refused(tmp_path):
    d = tmp_path / "2026-10-16"
    d.mkdir()
    build(d)
    b = b"[1]"
    (d / "script.json").write_bytes(b)
    ledger_b = (d / "ledger.json").read_bytes()
    put(d, "verdict.json", verdict(b, ledger_b, gate.REQUIRED, dry=False))
    assert publish.run_stage(ctx(d), runner=never_run) == StageResult(ok=False, reason="script_unreadable")


# ---- render verdict ----

@pytest.mark.parametrize("render,reason", [
    ({"stage": "voice"}, "render_not_accepted:stage"),
    ({"stage": None}, "render_not_accepted:stage"),
    ({"day": "2026-10-15"}, "render_not_accepted:day"),
    ({"dry": True}, "render_not_accepted:dry"),
    ({"dry": "false"}, "render_not_accepted:dry"),
    ({"script_sha256": "0" * 64}, "render_not_accepted:script_sha256"),
    ({"script_sha256": None}, "render_not_accepted:script_sha256"),
    ({"pass": False}, "render_not_accepted:verdict_not_pass"),
    ({"schema": "other"}, "render_not_accepted:bad_schema"),
    ({"subject_sha256": "0" * 64}, "render_not_accepted:sha_mismatch"),
    ({"ledger_sha256": "0" * 64}, "render_not_accepted:ledger_mismatch"),
])
def test_render_verdict_refusals_stop_before_any_browser(tmp_path, render, reason):
    d = tmp_path / "2026-10-16"
    d.mkdir()
    build(d, render=render)
    assert publish.run_stage(ctx(d), runner=never_run) == StageResult(ok=False, reason=reason)


def test_render_missing_ledger_check_names_the_failing_check(work):
    ledger_b = (work / "ledger.json").read_bytes()
    bad = verdict(VIDEO, ledger_b, gate.RENDER_REQUIRED, day="2026-10-16", stage="render", dry=False,
                  script_sha256=sha((work / "script.json").read_bytes()))
    bad["checks"][2]["pass"] = False
    put(work, "render.json", bad)
    assert publish.run_stage(ctx(work), runner=never_run) == StageResult(ok=False, reason="render_not_accepted:failed:loudness")


def test_render_checked_with_gate_render_required_and_ledger(dwork, monkeypatch):
    calls = []
    real = gate.read_verified_render
    monkeypatch.setattr(publish.gate, "read_verified_render",
                        lambda v, s, led, req: (calls.append((v.name, s.name, led.name, tuple(req))),
                                                real(v, s, led, req))[1])
    publish.run_stage(ctx(dwork, dry=True), runner=never_run)
    assert calls == [("render-dry.json", "episode-dry.mp4", "ledger.json", gate.RENDER_REQUIRED)]
    calls.clear()
    publish.run_stage(ctx(dwork), runner=never_run)   # a real run never reads the dry names
    assert calls == [("render.json", "episode.mp4", "ledger.json", gate.RENDER_REQUIRED)]


def test_render_json_is_read_once_so_a_later_swap_changes_nothing(work, monkeypatch):
    real = publish.gate.read_verified_render

    def swap_after_judge(v, s, led, req):
        out = real(v, s, led, req)
        put(work, "render.json", {"schema": "swapped", "stage": "voice", "dry": True})
        return out
    monkeypatch.setattr(publish.gate, "read_verified_render", swap_after_judge)
    res = publish.run_stage(ctx(work), runner=ok_child(work))
    assert res == StageResult(ok=True, reason=f"uploaded_private:{VID}")


def test_episode_swapped_after_the_judge_cannot_reach_the_upload(work, monkeypatch):
    real = publish.gate.read_verified_render

    def swap_after_judge(v, s, led, req):
        out = real(v, s, led, req)
        (work / "episode.mp4").write_bytes(b"swapped after the check")
        return out
    monkeypatch.setattr(publish.gate, "read_verified_render", swap_after_judge)
    publish.run_stage(ctx(work), runner=ok_child(work))
    assert (work / publish.STAGING_NAME).read_bytes() == VIDEO


def test_gate_accepts_is_not_a_second_judge_of_render_json(dwork, monkeypatch):
    monkeypatch.setattr(publish.gate, "accepts", never_run)
    assert publish.run_stage(ctx(dwork, dry=True), runner=never_run).reason == "dry_run"


def test_judged_ledger_must_be_the_text_gates_ledger(work, monkeypatch):
    real = publish.gate.read_verified_render
    monkeypatch.setattr(publish.gate, "read_verified_render",
                        lambda v, s, led, req: (lambda o: (o[0], o[1], o[2] + b" ", o[3]))(real(v, s, led, req)))
    assert publish.run_stage(ctx(work), runner=never_run).reason == "render_not_accepted:ledger_sha256"


# ---- staging copy and item ----

def test_item_points_at_a_staging_copy_with_the_verdict_hash(work):
    r = ok_child(work)
    publish.run_stage(ctx(work), runner=r)
    item = json.loads((work / "upload-item.json").read_text())
    staged = work / publish.STAGING_NAME
    assert item["file"] == str(staged) and item["file"] != str(work / "episode.mp4")
    assert staged.read_bytes() == VIDEO
    assert item["sha256"] == sha(VIDEO) == json.loads((work / "render.json").read_text())["subject_sha256"]
    assert item["id"] == "daily-2026-10-16" and item["day"] == "2026-10-16"
    assert item["channel"] == CHAN
    assert item["title"] == "Two agents, one browser pool"
    assert item["description"] == "What got built yesterday.\n\n" + DISCLOSURE
    assert item["synthetic"] is True


def test_staging_copy_that_does_not_hash_to_the_verdict_stops(work, monkeypatch):
    monkeypatch.setattr(publish, "_write_bytes", lambda p, b: p.write_bytes(b + b"x"))
    assert publish.run_stage(ctx(work), runner=never_run) == StageResult(ok=False, reason="staging_mismatch")


def test_stale_staging_file_or_symlink_is_replaced(work, tmp_path):
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"not mine")
    (work / publish.STAGING_NAME).symlink_to(outside)
    publish.run_stage(ctx(work), runner=ok_child(work))
    assert not (work / publish.STAGING_NAME).is_symlink()
    assert (work / publish.STAGING_NAME).read_bytes() == VIDEO
    assert outside.read_bytes() == b"not mine"


# ---- the disclosure line (settings show.disclosure) ----

@pytest.mark.parametrize("bad", ["Narrated by a voice " + DASHES[0] + " really", "Made with <b>care</b>",
                                 "A line\nand another", "zero" + chr(0x200B) + "width", "   ", "x" * 201,
                                 "an em " + DASHES[1] + " dash", "a bar " + DASHES[2] + " here"])
def test_a_disclosure_that_could_not_end_a_description_stops_before_any_browser(work, bad):
    res = publish.run_stage(ctx(work, settings=dataclasses.replace(SETTINGS, disclosure=bad)), runner=never_run)
    assert res == StageResult(ok=False, reason="bad_disclosure")
    assert not (work / "upload-item.json").exists()


def test_the_disclosure_check_passes_a_plain_line():
    assert publish.disclosure_ok(DISCLOSURE) and publish.disclosure_ok("x" * 200)
    assert publish.disclosure_ok("Narrated with a synthetic voice (my own, cloned).")


def test_description_gets_the_disclosure_always_but_never_twice(tmp_path):
    for desc, want in (("", DISCLOSURE), ("plain", "plain\n\n" + DISCLOSURE),
                       ("already\n\n" + DISCLOSURE, "already\n\n" + DISCLOSURE)):
        d = tmp_path / f"2026-10-16-{len(want)}"
        d.mkdir()
        build(d, script={"description": desc}, dry=True)
        publish.run_stage(ctx(d, dry=True), runner=never_run)
        assert json.loads((d / "upload-item.json").read_text())["description"] == want


def test_description_disclosure_exactly_once_even_if_the_script_repeats_it(tmp_path):
    d = tmp_path / "2026-10-16"
    d.mkdir()
    build(d, script={"description": f"intro\n\n{DISCLOSURE}\n\n{DISCLOSURE}"}, dry=True)
    publish.run_stage(ctx(d, dry=True), runner=never_run)
    desc = json.loads((d / "upload-item.json").read_text())["description"]
    assert desc.count(DISCLOSURE) == 1 and desc.startswith("intro") and desc.endswith(DISCLOSURE)


# ---- dry ----

def test_dry_writes_the_item_and_never_runs_the_browser(dwork):
    res = publish.run_stage(ctx(dwork, dry=True), runner=never_run)
    assert res == StageResult(ok=True, reason="dry_run")
    item = json.loads((dwork / "upload-item.json").read_text())
    assert item["dry"] is True and item["sha256"] == sha(VIDEO)


def test_dry_still_refuses_a_bad_verdict(tmp_path):
    d = tmp_path / "2026-10-16"
    d.mkdir()
    build(d, render={"stage": "voice"})
    assert publish.run_stage(ctx(d, dry=True), runner=never_run).ok is False


def test_dry_does_not_launch_even_with_a_passing_real_render_json(work):
    build(work)   # a folder with only a real-named render: dry reads the dry names, finds none, and stops before the child
    publish.run_stage(ctx(work, dry=True), runner=never_run)  # never_run would raise if it got as far as the child


def test_real_run_after_dry_overwrites_the_dry_item(work):
    shutil.copy(work / "render.json", work / "render-dry.json")
    shutil.copy(work / "episode.mp4", work / "episode-dry.mp4")
    d = json.loads((work / "render-dry.json").read_text())
    put(work, "render-dry.json", {**d, "dry": True})
    publish.run_stage(ctx(work, dry=True), runner=never_run)
    assert json.loads((work / "upload-item.json").read_text())["dry"] is True
    publish.run_stage(ctx(work), runner=ok_child(work))
    assert "dry" not in json.loads((work / "upload-item.json").read_text())


def test_dry_publish_reads_render_dry_json_and_episode_dry_mp4(dwork):
    res = publish.run_stage(ctx(dwork, dry=True), runner=never_run)
    assert res == StageResult(ok=True, reason="dry_run")
    item = json.loads((dwork / "upload-item.json").read_text())
    assert item["dry"] is True and item["sha256"] == sha(VIDEO) and item["day"] == "2026-10-16"
    assert not (dwork / publish.STAGING_NAME).exists() and not (dwork / "publish.log").exists()


def test_dry_publish_does_not_fall_back_to_the_real_names(work):
    res = publish.run_stage(ctx(work, dry=True), runner=never_run)   # real render.json and episode.mp4 only
    assert res.ok is False and res.reason.startswith("render_not_accepted:")
    assert not (work / "upload-item.json").exists()


def test_a_dry_render_verdict_with_dry_false_is_refused_in_dry(tmp_path):
    d = tmp_path / "2026-10-16-dry"
    d.mkdir()
    build(d, dry=True, render={"dry": False})
    assert publish.run_stage(ctx(d, dry=True), runner=never_run) == StageResult(
        ok=False, reason="render_not_accepted:dry")


def test_dry_accepts_a_dry_or_a_seeded_text_verdict_and_refuses_a_non_boolean(tmp_path):
    for text, ok in (({"dry": True}, True), ({"dry": False}, True), ({"dry": None}, False), ({"dry": "true"}, False)):
        d = tmp_path / f"2026-10-16-{ok}-{text['dry']}"
        d.mkdir()
        build(d, dry=True, text=text)
        res = publish.run_stage(ctx(d, dry=True), runner=never_run)
        assert res.ok is ok, (text, res)
        if not ok:
            assert res.reason == "text_not_accepted:dry"


def test_real_run_still_refuses_a_dry_text_verdict_and_a_dry_render(work):
    put(work, "verdict.json", {**json.loads((work / "verdict.json").read_text()), "dry": True})
    assert publish.run_stage(ctx(work), runner=never_run).reason == "text_not_accepted:dry"


def test_dry_item_never_touches_a_real_item_or_receipt(dwork):
    publish.run_stage(ctx(dwork, dry=True), runner=never_run)
    assert not (dwork / "upload.json").exists() and not (dwork / publish.STAGING_NAME).exists()
    assert (dwork / "upload-item.json").exists()


# ---- receipt: no second copy ----

def test_verified_receipt_for_this_render_skips_the_browser(work):
    put(work, "upload.json", {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": VID,
                              "stage": "private_verified"})
    assert publish.run_stage(ctx(work), runner=never_run) == StageResult(ok=True, reason=f"already_private:{VID}")


def test_receipt_for_a_different_render_needs_you(work):
    put(work, "upload.json", {"day": "2026-10-16", "subject_sha256": "f" * 64, "video_id": VID,
                              "stage": "private_verified"})
    assert publish.run_stage(ctx(work), runner=never_run) == StageResult(ok=False, reason="needs_you:receipt_other_render")


def test_receipt_for_a_different_render_at_any_stage_needs_you(work):
    put(work, "upload.json", {"day": "2026-10-16", "subject_sha256": "f" * 64, "stage": "uploaded", "video_id": VID})
    assert publish.run_stage(ctx(work), runner=never_run).reason == "needs_you:receipt_other_render"


def test_partial_receipt_for_this_render_goes_to_the_child_to_resume(work):
    put(work, "upload.json", {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "stage": "saved"})
    r = ok_child(work)
    assert publish.run_stage(ctx(work), runner=r).ok and len(r.seen) == 1


def test_corrupt_receipt_needs_you_without_a_browser(work):
    (work / "upload.json").write_text("{not json")
    assert publish.run_stage(ctx(work), runner=never_run) == StageResult(ok=False, reason="needs_you:receipt_corrupt")


def test_receipt_for_another_day_needs_you(work):
    put(work, "upload.json", {"day": "2026-10-15", "subject_sha256": sha(VIDEO), "stage": "private_verified",
                              "video_id": VID})
    assert publish.run_stage(ctx(work), runner=never_run) == StageResult(ok=False, reason="needs_you:receipt_other_day")


def test_verified_receipt_with_a_bad_video_id_is_not_trusted(work):
    put(work, "upload.json", {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": "x", "stage": "private_verified"})
    assert publish.run_stage(ctx(work), runner=never_run).reason == "needs_you:receipt_invalid"


@pytest.mark.parametrize("body", ["[]", '"x"', "5", "null"])
def test_non_dict_receipt_is_corrupt(work, body):
    (work / "upload.json").write_text(body)
    assert publish.run_stage(ctx(work), runner=never_run).reason == "needs_you:receipt_corrupt"


def test_empty_object_receipt_is_no_receipt(work):
    (work / "upload.json").write_text("{}")
    r = ok_child(work)
    assert publish.run_stage(ctx(work), runner=r).ok and len(r.seen) == 1


# ---- the child ----

def test_child_command_environment_and_log(work, monkeypatch):
    monkeypatch.setenv(publish.KEEP_ENV, "1")
    r = ok_child(work)
    publish.run_stage(ctx(work), runner=r)
    cmd, kw = r.seen[0]
    assert isinstance(cmd, list) and cmd[-4:] == ["upload", str(work), "--visibility", "private"]
    assert cmd[-5].endswith("release/studio.py")
    assert kw["stdin"] == subprocess.DEVNULL and kw["timeout"] == publish.UPLOAD_TIMEOUT_S
    assert publish.UPLOAD_TIMEOUT_S > 40 * 60 + 30 * 60
    assert publish.KEEP_ENV == "ORION_CLOCK_KEEP_ON_ABORT" and publish.KEEP_ENV not in kw["env"]
    assert "last line" in (work / "publish.log").read_text()


@pytest.mark.parametrize("rc,reason", [(1, "studio_abort"), (2, "studio_usage"), (3, "browser_busy"),
                                       (4, "needs_you"), (9, "studio_exit_9"), (-9, "studio_signal"),
                                       (143, "studio_signal")])
def test_child_exit_codes_become_short_reasons(work, rc, reason):
    out = "/home/someone/secret path and Title Text\n"
    res = publish.run_stage(ctx(work), runner=ok_child(work, rc=rc, out=out))
    assert res == StageResult(ok=False, reason=reason)
    assert "secret" in (work / "publish.log").read_text()


def test_exit_zero_without_a_matching_private_receipt_fails(work):
    res = publish.run_stage(ctx(work), runner=ok_child(work, receipt=False))
    assert res == StageResult(ok=False, reason="studio_receipt_missing")


def test_exit_zero_with_a_receipt_for_another_render_fails(work):
    def runner(cmd, **kw):
        put(work, "upload.json", {"day": "2026-10-16", "subject_sha256": "e" * 64, "video_id": VID, "stage": "private_verified"})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    assert publish.run_stage(ctx(work), runner=runner).reason == "studio_receipt_missing"


def test_child_timeout_fails_and_releases_the_browser(work):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        if cmd[-1] == "release":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
    res = publish.run_stage(ctx(work), runner=runner)
    assert res == StageResult(ok=False, reason="studio_timeout")
    assert calls[-1][-1] == "release" and calls[-1][-2].endswith("studio.py")


def test_release_failure_after_timeout_does_not_mask_the_timeout(work):
    def runner(cmd, **kw):
        if cmd[-1] == "release":
            raise OSError("boom")
        raise subprocess.TimeoutExpired(cmd, 1)
    assert publish.run_stage(ctx(work), runner=runner).reason == "studio_timeout"


def test_child_cannot_start(work):
    def runner(cmd, **kw):
        raise FileNotFoundError("nope")
    assert publish.run_stage(ctx(work), runner=runner) == StageResult(ok=False, reason="studio_not_runnable")


def test_reasons_are_short_codes_without_dashes_or_paths(work):
    for rc in (0, 1, 3, 4, 9):
        res = publish.run_stage(ctx(work), runner=ok_child(work, rc=rc, out="/etc/x " + chr(0x2014) + " text"))
        assert "/" not in res.reason and not any(c in res.reason for c in DASHES)


def test_source_has_no_long_dashes():
    assert not any(c in inspect.getsource(publish) for c in DASHES)


# ---- interrupts, upload lock, strictness ----

class Terminated(BaseException):
    """Stands in for run.py's RunTerminated (a BaseException)."""


def test_base_exception_during_child_releases_the_browser_and_propagates(work):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        if cmd[-1] == "release":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise Terminated()
    with pytest.raises(Terminated):
        publish.run_stage(ctx(work), runner=runner)
    assert calls[-1][-1] == "release" and calls[-1][-2].endswith("studio.py")


def test_keyboard_interrupt_releases_too_and_a_failing_release_does_not_mask_it(work):
    def runner(cmd, **kw):
        if cmd[-1] == "release":
            raise OSError("boom")
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        publish.run_stage(ctx(work), runner=runner)


def hold(work):
    fd = os.open(work / "upload.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


@pytest.mark.parametrize("dry", [False, True])
def test_held_upload_lock_stops_with_nothing_written(work, dwork, dry):
    work = dwork if dry else work
    fd = hold(work)
    try:
        res = publish.run_stage(ctx(work, dry=dry), runner=never_run)
    finally:
        os.close(fd)
    assert res == StageResult(ok=False, reason="needs_you:upload_locked")
    assert not (work / publish.STAGING_NAME).exists() and not (work / "upload-item.json").exists()


def test_lock_is_held_while_writing_and_free_when_the_child_starts(work, monkeypatch):
    state = {}

    def runner(cmd, **kw):
        fd = os.open(work / "upload.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # the child must be able to take it
            state["free_at_child"] = True
        finally:
            os.close(fd)
        put(work, "upload.json", {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": VID,
                                  "stage": "private_verified"})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    real_write = publish._write_bytes
    seen = []

    def spy(path, data):
        fd = os.open(work / "upload.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            seen.append("free")
        except OSError:
            seen.append("held")
        finally:
            os.close(fd)
        real_write(path, data)
    monkeypatch.setattr(publish, "_write_bytes", spy)
    res = publish.run_stage(ctx(work), runner=runner)
    assert res.ok and state == {"free_at_child": True} and seen == ["held"]


def test_text_verdict_must_not_be_dry(work):
    script_b = (work / "script.json").read_bytes()
    ledger_b = (work / "ledger.json").read_bytes()
    for dry in (True, None, "false", 0):
        put(work, "verdict.json", verdict(script_b, ledger_b, gate.REQUIRED, dry=dry))
        assert publish.run_stage(ctx(work), runner=never_run) == StageResult(ok=False, reason="text_not_accepted:dry")
    put(work, "verdict.json", verdict(script_b, ledger_b, gate.REQUIRED))   # no dry field at all
    assert publish.run_stage(ctx(work), runner=never_run).reason == "text_not_accepted:dry"


@pytest.mark.parametrize("dry", [None, 0, "", []])
def test_render_dry_must_be_exactly_false(tmp_path, dry):
    d = tmp_path / "2026-10-16"
    d.mkdir()
    build(d, render={"dry": dry})
    assert publish.run_stage(ctx(d), runner=never_run) == StageResult(ok=False, reason="render_not_accepted:dry")


def test_render_dry_field_missing_is_refused(tmp_path):
    d = tmp_path / "2026-10-16"
    d.mkdir()
    build(d)
    r = json.loads((d / "render.json").read_text())
    del r["dry"]
    put(d, "render.json", r)
    assert publish.run_stage(ctx(d), runner=never_run).reason == "render_not_accepted:dry"


def _script_without(tmp_path, key=None, dry=False, **over):
    d = tmp_path / "2026-10-16"
    d.mkdir()
    build(d, script=over, dry=dry)
    sfx = "-dry" if dry else ""
    obj = json.loads((d / "script.json").read_text())
    if key:
        del obj[key]
    b = json.dumps(obj).encode()
    (d / "script.json").write_bytes(b)
    led = (d / "ledger.json").read_bytes()
    put(d, "verdict.json", verdict(b, led, gate.REQUIRED, dry=False))
    r = json.loads((d / f"render{sfx}.json").read_text())
    r["script_sha256"] = sha(b)
    put(d, f"render{sfx}.json", r)
    return d


def test_script_quiet_missing_is_refused(tmp_path):
    d = _script_without(tmp_path, key="quiet")
    assert publish.run_stage(ctx(d), runner=never_run).reason == "script_quiet"


def test_script_quiet_none_is_refused(tmp_path):
    d = _script_without(tmp_path, quiet=None)
    assert publish.run_stage(ctx(d), runner=never_run).reason == "script_quiet"


def test_title_is_stripped_in_the_item(tmp_path):
    d = _script_without(tmp_path, dry=True, title="  Padded title \n")
    publish.run_stage(ctx(d, dry=True), runner=never_run)
    assert json.loads((d / "upload-item.json").read_text())["title"] == "Padded title"


def test_exit_zero_with_a_saved_stage_receipt_is_not_ok(work):
    def runner(cmd, **kw):
        put(work, "upload.json", {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": VID, "stage": "saved"})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    assert publish.run_stage(ctx(work), runner=runner).reason == "studio_receipt_missing"


def test_exit_zero_receipt_for_another_day_or_bad_id_is_not_ok(work):
    for rec in ({"day": "2026-10-15", "subject_sha256": sha(VIDEO), "video_id": VID, "stage": "private_verified"},
                {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": "short", "stage": "private_verified"},
                {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": 7, "stage": "private_verified"}):
        def runner(cmd, rec=rec, **kw):
            put(work, "upload.json", rec)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        assert publish.run_stage(ctx(work), runner=runner).reason == "studio_receipt_missing"
        (work / "upload.json").unlink()


@pytest.mark.parametrize("exc", [RuntimeError("x"), ValueError("y"), PermissionError("z")])
def test_any_runner_exception_is_not_runnable_without_a_traceback(work, exc):
    def runner(cmd, **kw):
        raise exc
    assert publish.run_stage(ctx(work), runner=runner) == StageResult(ok=False, reason="studio_not_runnable")


def test_pinned_constants_and_child_cwd(work):
    assert publish.UPLOAD_TIMEOUT_S == 5400 and publish.RELEASE_TIMEOUT_S == 180
    r = ok_child(work)
    publish.run_stage(ctx(work), runner=r)
    cmd, kw = r.seen[0]
    assert kw["cwd"] == str(publish.REPO) and publish.REPO == paths.REPO
    assert publish.STUDIO == paths.REPO / "release" / "studio.py" and publish.STUDIO.is_file()
    assert kw["capture_output"] is True and kw["text"] is True


def test_release_after_timeout_uses_the_release_timeout_and_same_cwd(work):
    calls = []

    def runner(cmd, **kw):
        calls.append((cmd, kw))
        if cmd[-1] == "release":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise subprocess.TimeoutExpired(cmd, 1)
    publish.run_stage(ctx(work), runner=runner)
    cmd, kw = calls[-1]
    assert kw["timeout"] == 180 and kw["cwd"] == str(publish.REPO) and kw["stdin"] == subprocess.DEVNULL


def test_generic_runner_exception_calls_the_best_effort_release(work):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        raise RuntimeError("boom")
    res = publish.run_stage(ctx(work), runner=runner)
    assert res == StageResult(ok=False, reason="studio_not_runnable")
    assert len(calls) == 2 and calls[0][-4] == "upload" and calls[1][-1] == "release"


# ---- a verified private upload survives a dirty exit ----

def _verified_then_exit(work, rc, out="release trouble\n"):
    def runner(cmd, **kw):
        put(work, "upload.json", {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": VID,
                                  "stage": "private_verified"})
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")
    return runner


@pytest.mark.parametrize("rc", [1, 2, 3, 4, 9, -9, 143])
def test_verified_receipt_after_any_exit_is_ok(work, rc):
    res = publish.run_stage(ctx(work), runner=_verified_then_exit(work, rc))
    assert res == StageResult(ok=True, reason=f"uploaded_private:{VID}")


def test_release_problem_after_a_verified_upload_goes_to_the_log_with_a_short_code(work):
    publish.run_stage(ctx(work), runner=_verified_then_exit(work, 1))
    log = (work / "publish.log").read_text()
    assert "release trouble" in log and "release_problem:studio_abort" in log


def test_clean_exit_writes_no_release_problem_line(work):
    publish.run_stage(ctx(work), runner=ok_child(work))
    assert "release_problem" not in (work / "publish.log").read_text()


@pytest.mark.parametrize("rc,reason", [(1, "studio_abort"), (143, "studio_signal"), (4, "needs_you")])
def test_nonzero_exit_without_a_verified_receipt_keeps_its_failure(work, rc, reason):
    assert publish.run_stage(ctx(work), runner=ok_child(work, rc=rc)).reason == reason


@pytest.mark.parametrize("rec", [
    {"day": "2026-10-15", "subject_sha256": sha(VIDEO), "video_id": VID, "stage": "private_verified"},
    {"day": "2026-10-16", "subject_sha256": "e" * 64, "video_id": VID, "stage": "private_verified"},
    {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": "short", "stage": "private_verified"},
    {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": VID, "stage": "saved"}])
def test_nonzero_exit_with_a_wrong_receipt_stays_a_failure(work, rec):
    def runner(cmd, **kw):
        put(work, "upload.json", rec)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
    assert publish.run_stage(ctx(work), runner=runner).reason == "studio_abort"


# ---- needs_you sub-codes from the child's last line ----

NEEDS = ["signed_out", "wrong_channel", "receipt_corrupt", "receipt_other_day", "receipt_other_render",
         "receipt_identity_changed", "upload_started_no_video", "receipt_not_resumable", "bad_video_id",
         "ledger_has_day", "unreadable_ledger", "draft_gone", "visibility_mismatch",
         "public_unconfirmed"]


@pytest.mark.parametrize("code", NEEDS)
def test_child_exit_4_with_a_known_code_maps_to_needs_you_code(work, code):
    out = f"NEEDS YOU at sign-in: free text\nreleased\nNEEDS_YOU {code}\n"
    res = publish.run_stage(ctx(work), runner=ok_child(work, rc=4, out=out))
    assert res == StageResult(ok=False, reason=f"needs_you:{code}")


@pytest.mark.parametrize("out", ["", "NEEDS YOU at x: y\n", "NEEDS_YOU\n", "NEEDS_YOU something_new\n",
                                 "NEEDS_YOU ../../etc\n", "NEEDS_YOU signed_out extra\n", "xNEEDS_YOU signed_out\n"])
def test_child_exit_4_with_an_unknown_or_missing_code_is_plain_needs_you(work, out):
    assert publish.run_stage(ctx(work), runner=ok_child(work, rc=4, out=out)).reason == "needs_you"


def test_the_last_needs_you_line_wins(work):
    out = "NEEDS_YOU signed_out\nNEEDS_YOU wrong_channel\n"
    assert publish.run_stage(ctx(work), runner=ok_child(work, rc=4, out=out)).reason == "needs_you:wrong_channel"


def test_the_code_line_is_ignored_for_other_exit_codes(work):
    assert publish.run_stage(ctx(work), runner=ok_child(work, rc=1, out="NEEDS_YOU signed_out\n")).reason == "studio_abort"


def test_code_line_on_stderr_is_read_too(work):
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 4, stdout="", stderr="NEEDS_YOU draft_gone\n")
    assert publish.run_stage(ctx(work), runner=runner).reason == "needs_you:draft_gone"


def test_publish_codes_equal_the_drivers_fixed_set():
    """Read from the driver's source (never executed here: importing it would patch the shared tab primitives)."""
    import ast
    tree = ast.parse(publish.STUDIO.read_text())
    codes = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "NEEDS_YOU_CODES" for t in n.targets))
    assert set(publish.NEEDS_YOU_CODES) | {"needs_you"} == set(codes)


# ---- the switch back and the tab close are logged, never a result ----

def test_a_failed_switch_back_is_logged_as_a_short_code_and_changes_no_result(work):
    res = publish.run_stage(ctx(work), runner=ok_child(work, out="ok\nSWITCHED_BACK false\n"))
    assert res == StageResult(ok=True, reason=f"uploaded_private:{VID}")
    assert "switched_back:false" in (work / "publish.log").read_text().splitlines()


def test_a_switch_back_that_worked_or_is_unreported_writes_no_note(work):
    publish.run_stage(ctx(work), runner=ok_child(work, out="ok\nSWITCHED_BACK true\n"))
    assert "switched_back" not in (work / "publish.log").read_text()
    publish.run_stage(ctx(work), runner=ok_child(work, out="ok\n"))
    assert "switched_back" not in (work / "publish.log").read_text()


def test_a_failed_switch_back_after_a_needs_you_stop_is_logged_and_the_reason_stays(work):
    out = "NEEDS YOU at identity\nSWITCHED_BACK false\nNEEDS_YOU wrong_channel\n"
    res = publish.run_stage(ctx(work), runner=ok_child(work, rc=4, out=out, receipt=False))
    assert res == StageResult(ok=False, reason="needs_you:wrong_channel")
    assert "switched_back:false" in (work / "publish.log").read_text().splitlines()


def test_a_failed_tab_close_is_logged_as_a_short_code_and_changes_no_result(work):
    publish.run_stage(ctx(work), runner=ok_child(work, out="ok\nCLOSED_TAB true\n"))
    assert "closed_tab" not in (work / "publish.log").read_text()
    (work / "upload.json").unlink()
    res = publish.run_stage(ctx(work), runner=ok_child(work, out="ok\nCLOSED_TAB false\n"))
    assert res == StageResult(ok=True, reason=f"uploaded_private:{VID}")
    assert "closed_tab:false" in (work / "publish.log").read_text().splitlines()


# ---- the off switch: public only when publish.json says exactly so ----

def set_mode(cfg_home, body):
    p = cfg_home / "publish.json"
    p.write_bytes(body) if isinstance(body, bytes) else p.write_text(body)


PUBLIC_ON = json.dumps({"visibility": "public"})


def test_no_publish_json_is_private(cfg_home):
    assert publish.visibility_mode() == "private"


@pytest.mark.parametrize("body,want", [
    (PUBLIC_ON, "public"), (json.dumps({"visibility": "public", "_comment": ["a note"]}), "public"),
    (json.dumps({"visibility": "private"}), "private"), (json.dumps({"visibility": "Public"}), "private"),
    (json.dumps({"visibility": "PUBLIC"}), "private"), (json.dumps({"visibility": " public"}), "private"),
    (json.dumps({"visibility": "unlisted"}), "private"), (json.dumps({"visibility": "schedule"}), "private"),
    (json.dumps({"visibility": True}), "private"), (json.dumps({"visibility": ["public"]}), "private"),
    (json.dumps({"Visibility": "public"}), "private"), (json.dumps({}), "private"),
    (json.dumps(["public"]), "private"), (json.dumps("public"), "private"), ("null", "private"),
    ("{not json", "private"), ("", "private"), (b"\xff\xfe\x00", "private"),
    pytest.param("[" * 100000, "private", id="deeply-nested")])
def test_mode_is_public_only_for_exactly_visibility_public(cfg_home, body, want):
    set_mode(cfg_home, body)
    assert publish.visibility_mode() == want


def test_the_shipped_example_is_private_and_switching_it_on_works(cfg_home):
    example = json.loads((paths.REPO / "config.example" / "publish.json").read_text())
    set_mode(cfg_home, json.dumps(example))
    assert publish.visibility_mode() == "private"
    set_mode(cfg_home, json.dumps({**example, "visibility": "public"}))   # the one edit the example asks for
    assert publish.visibility_mode() == "public"


def test_an_unreadable_publish_json_is_private_never_an_error(cfg_home):
    (cfg_home / "publish.json").mkdir()
    assert publish.visibility_mode() == "private"


def test_the_mode_comes_from_the_config_dir(cfg_home, tmp_path, monkeypatch):
    other = tmp_path / "other-cfg"
    other.mkdir()
    set_mode(other, PUBLIC_ON)
    assert publish.visibility_mode() == "private"
    monkeypatch.setenv(paths.ENV_CONFIG, str(other))
    assert publish.visibility_mode() == "public"


@pytest.mark.parametrize("body,mode", [(None, "private"), (PUBLIC_ON, "public"),
                                       (json.dumps({"visibility": "unlisted"}), "private")])
def test_the_child_argv_carries_the_mode_and_the_item_records_it(work, cfg_home, body, mode):
    if body is not None:
        set_mode(cfg_home, body)
    r = ok_child(work)
    publish.run_stage(ctx(work), runner=r)
    cmd, _ = r.seen[0]
    assert cmd[-4:] == ["upload", str(work), "--visibility", mode]
    assert json.loads((work / "upload-item.json").read_text())["visibility"] == mode


def test_a_dry_run_records_the_mode_in_the_item_and_never_starts_the_child(dwork, cfg_home):
    set_mode(cfg_home, PUBLIC_ON)
    assert publish.run_stage(ctx(dwork, dry=True), runner=never_run) == StageResult(ok=True, reason="dry_run")
    item = json.loads((dwork / "upload-item.json").read_text())
    assert item["visibility"] == "public" and item["dry"] is True


def test_a_publish_json_that_is_not_a_regular_file_is_private_without_blocking(cfg_home, tmp_path):
    os.mkfifo(cfg_home / "publish.json")         # a read would block forever: it must never be opened
    assert publish.visibility_mode() == "private"
    (cfg_home / "publish.json").unlink()
    target = tmp_path / "real.json"
    target.write_text(PUBLIC_ON)
    (cfg_home / "publish.json").symlink_to(target)    # a symlink to a regular file is still your file
    assert publish.visibility_mode() == "public"


# ---- publish reasons for a public upload ----

def test_a_verified_public_upload_is_uploaded_public(work, cfg_home):
    set_mode(cfg_home, PUBLIC_ON)
    r = ok_child(work, stage="public_verified", visibility="PUBLIC")
    assert publish.run_stage(ctx(work), runner=r) == StageResult(ok=True, reason=f"uploaded_public:{VID}")
    assert r.seen[0][0][-2:] == ["--visibility", "public"]


@pytest.mark.parametrize("rc,reason", [(1, "studio_abort"), (2, "studio_usage"), (3, "browser_busy"),
                                       (4, "needs_you"), (9, "studio_exit_9"), (-9, "studio_signal"),
                                       (143, "studio_signal")])
def test_a_verified_public_receipt_after_any_exit_is_ok_only_when_it_says_public(work, cfg_home, rc, reason):
    set_mode(cfg_home, PUBLIC_ON)
    for extra, want in (({"visibility": "PUBLIC"}, StageResult(ok=True, reason=f"uploaded_public:{VID}")),
                        ({}, StageResult(ok=False, reason=reason)),
                        ({"visibility": "PRIVATE"}, StageResult(ok=False, reason=reason))):
        def runner(cmd, extra=extra, **kw):
            put(work, "upload.json", {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": VID,
                                      "stage": "public_verified", **extra})
            return subprocess.CompletedProcess(cmd, rc, stdout="release trouble\n", stderr="")
        assert publish.run_stage(ctx(work), runner=runner) == want, extra
        (work / "upload.json").unlink()


@pytest.mark.parametrize("body", [None, PUBLIC_ON])
def test_a_public_verified_day_is_done_without_a_browser_in_either_mode(work, cfg_home, body):
    if body:
        set_mode(cfg_home, body)
    put(work, "upload.json", {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": VID,
                              "stage": "public_verified", "visibility": "PUBLIC"})
    assert publish.run_stage(ctx(work), runner=never_run) == StageResult(ok=True, reason=f"already_public:{VID}")


def test_a_private_verified_day_stays_a_noop_in_public_mode(work, cfg_home):
    set_mode(cfg_home, PUBLIC_ON)
    put(work, "upload.json", {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": VID,
                              "stage": "private_verified"})
    assert publish.run_stage(ctx(work), runner=never_run) == StageResult(ok=True, reason=f"already_private:{VID}")


@pytest.mark.parametrize("rec,reason", [
    ({"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": "x", "stage": "public_verified"},
     "needs_you:receipt_invalid"),
    ({"day": "2026-10-15", "subject_sha256": sha(VIDEO), "video_id": VID, "stage": "public_verified"},
     "needs_you:receipt_other_day"),
    ({"day": "2026-10-16", "subject_sha256": "f" * 64, "video_id": VID, "stage": "public_verified"},
     "needs_you:receipt_other_render")])
def test_a_wrong_public_receipt_is_never_trusted(work, rec, reason):
    put(work, "upload.json", rec)
    assert publish.run_stage(ctx(work), runner=never_run).reason == reason


@pytest.mark.parametrize("stage", ["saved", "unlisted_verified", "public", "PUBLIC_verified", "verified"])
def test_exit_zero_with_any_other_stage_is_not_an_upload(work, cfg_home, stage):
    set_mode(cfg_home, PUBLIC_ON)
    assert publish.run_stage(ctx(work), runner=ok_child(work, stage=stage)).reason == "studio_receipt_missing"


def test_the_childs_public_unconfirmed_stop_is_a_needs_you_reason(work, cfg_home):
    set_mode(cfg_home, PUBLIC_ON)
    out = "NEEDS YOU at verify: x\nSWITCHED_BACK true\nNEEDS_YOU public_unconfirmed\n"
    res = publish.run_stage(ctx(work), runner=ok_child(work, rc=4, out=out, receipt=False))
    assert res == StageResult(ok=False, reason="needs_you:public_unconfirmed")


# ---- a terminal stage counts only with its own visibility; a kill after the click resumes ----

@pytest.mark.parametrize("stage,extra", [("public_verified", {}), ("public_verified", {"visibility": "PRIVATE"}),
                                         ("public_verified", {"visibility": "public"}),
                                         ("public_verified", {"visibility": None}),
                                         ("private_verified", {"visibility": "PUBLIC"}),
                                         ("private_verified", {"visibility": None})])
def test_a_terminal_receipt_without_its_own_visibility_is_invalid(work, stage, extra):
    put(work, "upload.json", {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": VID, "stage": stage, **extra})
    assert publish.run_stage(ctx(work), runner=never_run) == StageResult(ok=False, reason="needs_you:receipt_invalid")


def test_a_private_verified_receipt_without_the_field_stays_valid(work):
    put(work, "upload.json", {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": VID,
                              "stage": "private_verified"})
    assert publish.run_stage(ctx(work), runner=never_run).reason == f"already_private:{VID}"
    put(work, "upload.json", {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": VID,
                              "stage": "private_verified", "visibility": "PRIVATE"})
    assert publish.run_stage(ctx(work), runner=never_run).reason == f"already_private:{VID}"


@pytest.mark.parametrize("body,stage,vis,reason", [(PUBLIC_ON, "public_verified", "PUBLIC", "uploaded_public"),
                                                   (None, "private_verified", "PRIVATE", "uploaded_private")])
def test_a_kill_after_the_click_then_a_rerun_ends_verified_with_the_right_reason_and_item(work, cfg_home, body, stage,
                                                                                       vis, reason):
    if body:
        set_mode(cfg_home, body)
    base = {"day": "2026-10-16", "subject_sha256": sha(VIDEO), "video_id": VID, "visibility": vis}

    def killed(cmd, **kw):            # the child was killed after it recorded the landed click
        put(work, "upload.json", {**base, "stage": "save_clicked"})
        return subprocess.CompletedProcess(cmd, 143, stdout="", stderr="")
    assert publish.run_stage(ctx(work), runner=killed) == StageResult(ok=False, reason="studio_signal")
    seen = []

    def resumed(cmd, **kw):           # the rerun goes to the child (save_clicked is resumable), which verifies only
        seen.append(cmd)
        assert json.loads((work / "upload.json").read_text())["stage"] == "save_clicked"
        put(work, "upload.json", {**base, "stage": stage})
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    assert publish.run_stage(ctx(work), runner=resumed) == StageResult(ok=True, reason=f"{reason}:{VID}")
    assert len(seen) == 1
    status = {"stages": {n: {"state": "done", "reason": ""} for n in notify.ORDER}}
    oc = notify._published(notify.outcome(status), work, "2026-10-16")
    item = notify.compose(DAY, oc, work)
    if vis == "PUBLIC":
        assert (item["kind"], item["key"], item["pointer"]) == ("fyi", "daily:2026-10-16:published",
                                                                 f"https://youtube.com/shorts/{VID}")
    else:
        assert (item["kind"], item["key"]) == ("approval", "daily:2026-10-16:ready")
