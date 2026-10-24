"""The render stage: the five render checks and their verdict, the probe, the HyperFrames call, the contact sheet, and
the stage itself (text gate first, read-once bytes, voices bound to what was voiced, archive and clear of earlier
renders, rejected copies of a failed one, dry never touching the real artifacts). HyperFrames, the mix, the remux and
the probe are faked in the stage tests; ffmpeg is real where a test says so."""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import pytest

from daily import align, compose, downstream, files, gate, paths, render, sheet, voiceconf
from daily.settings import load_settings
from daily.stage import Ctx
from media_helpers import CASES, HAS_FFMPEG, VOICES, dry_work, make_kit, needs_ffmpeg, use_config

GOOD = {"vcodec": "h264", "width": 1080, "height": 1920, "pix_fmt": "yuv420p", "fps": "30/1", "has_audio": True,
        "duration": 65.8, "lufs": -14.1}
OK = (True, "ok")
HASHES = {"script_sha256": "a" * 64, "ledger_sha256": "b" * 64}
LONG_DASHES = "".join(chr(c) for c in (0x2013, 0x2014, 0x2015))  # built from code points: no dash in this file


def failed(checks):
    return [c["name"] for c in checks if not c["pass"]]


def test_single_sources():
    assert render.RENDER_CHECKS is gate.RENDER_REQUIRED and render.MIN_COVERAGE == align.MIN_COVERAGE


def test_render_checks_pass_on_a_good_file():
    checks = render.render_checks(GOOD, 65.8, OK)
    assert [c["name"] for c in checks] == list(render.RENDER_CHECKS) and not failed(checks)


@pytest.mark.parametrize("field,value,name", [("duration", 59.0, "duration"), ("duration", 75.5, "duration"),
                                              ("width", 1920, "format"), ("lufs", -17.0, "loudness"),
                                              ("lufs", None, "loudness"), ("lufs", float("nan"), "loudness"),
                                              ("lufs", float("-inf"), "loudness"), ("has_audio", False, "audio_present"),
                                              ("duration", float("nan"), "duration")])
def test_render_checks_fail(field, value, name):
    m = dict(GOOD, **{field: value})
    total = m["duration"] if field == "duration" and value == value else 65.8
    assert failed(render.render_checks(m, total, OK)) == [name]


def only_fails(m, total=None):
    return failed(render.render_checks(m, m["duration"] if total is None else total, OK))


@pytest.mark.parametrize("d,ok", [(60.0, True), (75.0, True), (59.999, False), (75.001, False)])
def test_duration_window_boundaries(d, ok):
    assert (only_fails(dict(GOOD, duration=d)) == []) is ok


@pytest.mark.parametrize("lufs,ok", [(-15.0, True), (-13.0, True), (-15.0001, False), (-12.9999, False)])
def test_loudness_window_boundaries(lufs, ok):
    assert (only_fails(dict(GOOD, lufs=lufs)) == []) is ok


def test_timeline_tolerance_is_exactly_one_frame():
    assert failed(render.render_checks(GOOD, 65.8 - 1 / 30, OK)) == []
    assert failed(render.render_checks(GOOD, 65.8 + 1 / 30, OK)) == []
    assert failed(render.render_checks(GOOD, 65.8 - 1 / 30 - 1e-4, OK)) == ["duration"]
    assert failed(render.render_checks(GOOD, 65.8 - 1 / 25, OK)) == ["duration"]
    assert failed(render.render_checks(GOOD, 65.8 - 2 / 30, OK)) == ["duration"]
    # float noise at exactly one frame must not flip the verdict (the 1e-6 slack)
    assert failed(render.render_checks(GOOD, 65.8 - 1 / 30 - 5e-7, OK)) == []


@pytest.mark.parametrize("bad", ["yes", 1, None, "True", [True]])
def test_audio_flag_must_be_the_boolean_true(bad):
    assert failed(render.render_checks(dict(GOOD, has_audio=bad), 65.8, OK)) == ["audio_present"]


@pytest.mark.parametrize("bad", ["yes", 1, None, "True", [True]])
def test_caption_flag_must_be_the_boolean_true(bad):
    assert failed(render.render_checks(GOOD, 65.8, (bad, "x"))) == ["captions_match"]


def test_numbers_that_are_booleans_never_count():
    assert "loudness" in failed(render.render_checks(dict(GOOD, lufs=True), 65.8, OK))
    assert "duration" in failed(render.render_checks(dict(GOOD, duration=True), True, OK))


def test_duration_must_match_the_timeline_within_a_frame():
    assert failed(render.render_checks(GOOD, 64.0, OK)) == ["duration"]
    assert failed(render.render_checks(GOOD, 65.8 - 1 / 60, OK)) == []


def test_caption_failure_is_its_own_check():
    assert failed(render.render_checks(GOOD, 65.8, (False, "mismatched out"))) == ["captions_match"]


def test_a_missing_measurement_fails_closed():
    assert "format" in failed(render.render_checks({"duration": 65.8, "lufs": -14.0, "has_audio": True}, 65.8, OK))


def test_check_log_tolerates_only_the_two_lints():
    log = "\n".join([render.WARN + " timeline_track_too_dense: x", render.WARN + " low_contrast: caption",
                     "info line", "  " + render.FAIL + " missing asset"])
    probs = render.check_log_problems(log)
    assert len(probs) == 2 and "low_contrast" in probs[0] and "missing asset" in probs[1]


def test_render_log_problems():
    text = "ok\nPAGEERROR boom\nContinuing render despite lint issues"
    assert render.render_log_problems(text) == ["PAGEERROR boom"]


TL = {"caption_groups": [{"line": "out", "words": ["Tomorrow", "we"]}, {"line": "out", "words": ["go."]}]}
LINES = [{"id": "out", "text": "Tomorrow we go."}, {"id": "confessional", "text": "I did it."}]


def test_captions_match_good():
    ok, ev = render.captions_match(TL, LINES, {"out": 0.9, "confessional": 0.8})
    assert ok and "3 of 3" in ev


def test_captions_match_mismatch():
    ok, ev = render.captions_match(TL, [{"id": "out", "text": "Tomorrow we went."}], {"out": 0.9})
    assert not ok and "mismatched out" in ev


def test_captions_match_low_coverage_even_on_an_uncaptioned_line():
    ok, ev = render.captions_match(TL, LINES, {"out": 0.9, "confessional": 0.4})
    assert not ok and "low coverage" in ev and "confessional 0.40" in ev


def test_captions_match_coverage_boundary_and_nan():
    assert render.captions_match(TL, LINES, {"out": render.MIN_COVERAGE, "confessional": 1.0})[0]
    assert not render.captions_match(TL, LINES, {"out": 0.9, "confessional": render.MIN_COVERAGE - 0.01})[0]
    ok, ev = render.captions_match(TL, LINES, {"out": 0.9, "confessional": math.nan})
    assert not ok and "no coverage" in ev


def test_captions_match_missing_coverage_or_missing_groups_fails():
    assert not render.captions_match(TL, LINES, {"out": 0.9})[0]
    ok, ev = render.captions_match({"caption_groups": []}, [{"id": "out", "text": "Tomorrow we go."}], {"out": 0.9})
    assert not ok and "mismatched out" in ev and "0 of 3" in ev
    assert not render.captions_match({}, [{"id": "out", "text": "Go."}], {"out": 0.9})[0]


def groups(*words_per_group, line="out"):
    return {"caption_groups": [{"line": line, "words": list(w)} for w in words_per_group]}


def cm(tl, text="Tomorrow we go."):
    return render.captions_match(tl, [{"id": "out", "text": text}], {"out": 0.9})


def test_caption_words_are_compared_exactly():
    assert cm(groups(["Tomorrow", "we", "go."]))[0]
    assert not cm(groups(["tomorrow", "we", "go."]))[0]  # case
    assert not cm(groups(["Tomorrow ", "we", "go."]))[0]  # trailing space in a caption word
    assert not cm(groups(["Tomorrow  we", "go."]))[0]  # words merged with a double space
    assert not cm(groups(["Tomorrow we", "go."]))[0]  # words merged
    assert not cm(groups(["Tomorrow", "we", "go."], ["Tomorrow", "we", "go."]))[0]  # a duplicated group
    assert not cm(groups(["Tomorrow", "we", "go."], ["go."]))[0]  # extra trailing words
    assert not cm(groups(["go.", "we", "Tomorrow"]))[0]  # order
    assert not cm(groups(["Tomorrow", "we"]))[0]  # missing words
    assert not cm(groups(["Tomorrow", "Tomorrow", "we", "go."]))[0]  # set-equal but not list-equal


def test_a_double_space_in_a_line_is_not_an_empty_token():
    ok, ev = cm(groups(["Tomorrow", "we", "go."]), "Tomorrow  we go.")
    assert ok and "3 of 3" in ev
    assert not cm(groups(["Tomorrow", "", "we", "go."]), "Tomorrow  we go.")[0]


def test_hostile_ids_show_as_a_placeholder_on_every_failure_path():
    hostile = "/etc/passwd secret"
    for tl, cov in ((TL, {}), (TL, {hostile: 0.1}), ({"caption_groups": []}, {hostile: 0.9})):
        ok, ev = render.captions_match(tl, [{"id": hostile, "text": "Tomorrow we go."}], cov)
        assert not ok and "passwd" not in ev and "secret" not in ev and "?" in ev
    ok, ev = render.captions_match(TL, [{"id": hostile, "text": "Tomorrow we go."}], {})
    assert "no coverage ?" in ev


def test_captions_match_no_confessional_day():
    ok, ev = render.captions_match(TL, [{"id": "out", "text": "Tomorrow we go."}], {"out": 0.9})
    assert ok and "3 of 3" in ev


def test_captions_match_uses_aligns_tokens():
    text = "Tomorrow we go."
    assert align.match_tokens(text, [])[0] == text.split()
    # caption words are compared as a list, in order: a swapped pair fails
    swapped = {"caption_groups": [{"line": "out", "words": ["we", "Tomorrow", "go."]}]}
    assert not render.captions_match(swapped, [{"id": "out", "text": text}], {"out": 0.9})[0]


def test_captions_match_ignores_other_lines_groups_and_orders_by_lines():
    tl = {"caption_groups": [{"line": "other", "words": ["Tomorrow", "we", "go."]}]}
    assert not render.captions_match(tl, [{"id": "out", "text": "Tomorrow we go."}], {"out": 0.9})[0]


def test_evidence_never_holds_text_or_paths():
    hostile = [{"id": "/etc/passwd secret", "text": "Tomorrow we go."}]
    ok, ev = render.captions_match(TL, hostile, {"/etc/passwd secret": 0.1})
    assert not ok and "passwd" not in ev and "secret" not in ev and "Tomorrow" not in ev


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg and ffprobe are not installed")
    f = tmp_path_factory.mktemp("vid") / "v.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=navy:s=1080x1920:r=30:d=1",
                    "-f", "lavfi", "-i", "sine=f=440:d=1:r=48000", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(f)], check=True, timeout=60)
    return f


def test_probe_reads_a_real_vertical_file(tiny):
    m = render.probe(tiny)
    assert (m["vcodec"], m["width"], m["height"], m["pix_fmt"], m["fps"]) == render.FORMAT
    assert m["has_audio"] and 0.9 <= m["duration"] <= 1.3 and m["lufs"] is not None


def code_of(fn, *a):
    with pytest.raises(render.ProbeError) as e:
        fn(*a)
    return str(e.value)


def stub_run(monkeypatch, result):
    def fake(*a, **k):
        if isinstance(result, BaseException):
            raise result
        return subprocess.CompletedProcess(a, 0, stdout=result, stderr="")
    monkeypatch.setattr(render.subprocess, "run", fake)


@needs_ffmpeg
def test_probe_failure_codes_are_exact(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    assert code_of(render.probe, bad) == "probe_failed"
    assert code_of(render.probe, tmp_path / "missing.mp4") == "probe_failed"


def test_probe_timeout_code(monkeypatch, tmp_path):
    stub_run(monkeypatch, subprocess.TimeoutExpired("ffprobe", 1))
    assert code_of(render.probe, tmp_path / "v.mp4") == "probe_timeout"


def test_probe_unreadable_codes(monkeypatch, tmp_path):
    v = tmp_path / "v.mp4"
    stub_run(monkeypatch, "this is not json")  # ValueError from json.loads
    assert code_of(render.probe, v) == "probe_unreadable"
    stub_run(monkeypatch, OSError("no ffprobe"))
    assert code_of(render.probe, v) == "probe_unreadable"
    stub_run(monkeypatch, '{"streams": []}')  # no video stream: IndexError
    assert code_of(render.probe, v) == "probe_unreadable"
    stub_run(monkeypatch, '{"streams": [{"codec_name": "h264"}]}')  # KeyError
    assert code_of(render.probe, v) == "probe_unreadable"


def test_probe_loudness_error_codes(monkeypatch, tmp_path):
    answers = {"v:0": '{"streams": [{"codec_name": "h264", "width": 1080, "height": 1920, "pix_fmt": "yuv420p", '
                      '"r_frame_rate": "30/1"}]}',
               "a:0": '{"streams": [{"codec_type": "audio"}]}', "fmt": '{"format": {"duration": "1.0"}}'}

    def fake(argv, **k):
        key = "v:0" if "v:0" in argv else "a:0" if "a:0" in argv else "fmt"
        return subprocess.CompletedProcess(argv, 0, stdout=answers[key], stderr="")

    monkeypatch.setattr(render.subprocess, "run", fake)

    def raiser(exc):
        def boom(path):
            raise exc
        return boom
    monkeypatch.setattr(render.mix, "integrated_lufs", raiser(subprocess.TimeoutExpired("ffmpeg", 1)))
    assert code_of(render.probe, tmp_path / "v.mp4") == "probe_timeout"
    monkeypatch.setattr(render.mix, "integrated_lufs", raiser(OSError("gone")))
    assert code_of(render.probe, tmp_path / "v.mp4") == "probe_failed"
    monkeypatch.setattr(render.mix, "integrated_lufs", lambda path: -14.0)  # positive control: the same answers pass
    assert render.probe(tmp_path / "v.mp4")["lufs"] == -14.0


@pytest.fixture(scope="module")
def silent(tmp_path_factory):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg and ffprobe are not installed")
    f = tmp_path_factory.mktemp("silent") / "s.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=navy:s=1080x1920:r=30:d=1",
                    "-an", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(f)],
                   check=True, timeout=60)
    return f


def test_probe_of_a_silent_video_has_no_audio_and_fails_the_check(silent):
    m = render.probe(silent)
    assert m["has_audio"] is False
    assert "audio_present" in failed(render.render_checks(dict(m, duration=65.8, lufs=-14.0), 65.8, OK))


def test_probe_calls_carry_timeouts(monkeypatch, tiny):
    seen = []
    real = subprocess.run

    def spy(*a, **k):
        seen.append(k.get("timeout"))
        return real(*a, **k)
    monkeypatch.setattr(render.subprocess, "run", spy)
    render.probe(tiny)
    assert seen and all(t for t in seen)


def verdict(tmp_path, checks=None, dry=False):
    subject = tmp_path / "episode.mp4"
    subject.write_bytes(b"video")
    v = render.write_verdict(tmp_path / "render.json", "2026-10-16", subject,
                             checks or render.render_checks(GOOD, 65.8, OK), dry=dry, **HASHES)
    return subject, v


def test_verdict_round_trips_through_gate_accepts(tmp_path):
    subject, v = verdict(tmp_path)
    assert v["pass"] is True and gate.accepts(tmp_path / "render.json", subject, render.RENDER_CHECKS)[0] is True
    subject.write_bytes(b"another video")
    assert gate.accepts(tmp_path / "render.json", subject, render.RENDER_CHECKS) == (False, "sha_mismatch")


def test_verdict_goes_through_files_write_json(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(render.files, "write_json", lambda path, obj: calls.append((path, obj)))
    subject = tmp_path / "episode.mp4"
    subject.write_bytes(b"video")
    v = render.write_verdict(tmp_path / "render.json", "2026-10-16", subject,
                             render.render_checks(GOOD, 65.8, OK), False, **HASHES)
    assert calls == [(tmp_path / "render.json", v)] and not (tmp_path / "render.json").exists()


def test_verdict_day_dry_and_stage_are_recorded(tmp_path):
    _, real = verdict(tmp_path, dry=False)
    assert real["dry"] is False and real["day"] == "2026-10-16" and real["stage"] == "render"
    subject = tmp_path / "episode.mp4"
    other = render.write_verdict(tmp_path / "r2.json", "2026-11-02", subject,
                                 render.render_checks(GOOD, 65.8, OK), True, **HASHES)
    assert other["dry"] is True and other["day"] == "2026-11-02"
    assert json.loads((tmp_path / "r2.json").read_text())["dry"] is True


def test_verdict_hashes_go_to_the_right_keys_and_subject_is_hashed(tmp_path):
    import hashlib
    subject, v = verdict(tmp_path)
    assert v["script_sha256"] == "a" * 64 and v["ledger_sha256"] == "b" * 64
    assert v["subject_sha256"] == hashlib.sha256(b"video").hexdigest()


def test_verdict_records_hashes_and_is_atomic(tmp_path):
    subject, v = verdict(tmp_path, dry=True)
    on_disk = json.loads((tmp_path / "render.json").read_text())
    assert on_disk == v and on_disk["script_sha256"] == "a" * 64 and on_disk["ledger_sha256"] == "b" * 64
    assert on_disk["dry"] is True and on_disk["stage"] == "render" and on_disk["schema"] == gate.VERDICT_SCHEMA
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".render")] == []


def test_verdict_hashes_are_required(tmp_path):
    subject = tmp_path / "e.mp4"
    subject.write_bytes(b"x")
    with pytest.raises(TypeError):
        render.write_verdict(tmp_path / "r.json", "2026-10-16", subject, render.render_checks(GOOD, 65.8, OK), False)


def test_a_failing_check_makes_a_failing_verdict(tmp_path):
    subject, v = verdict(tmp_path, render.render_checks(dict(GOOD, lufs=-20.0), 65.8, OK))
    assert v["pass"] is False
    assert gate.accepts(tmp_path / "render.json", subject, render.RENDER_CHECKS) == (False, "failed:loudness")


@pytest.mark.parametrize("mutate", [lambda c: c[:-1], lambda c: c + [dict(c[0])], lambda c: c[::-1]])
def test_a_wrong_check_list_never_passes(tmp_path, mutate):
    checks = mutate(render.render_checks(GOOD, 65.8, OK))
    subject, v = verdict(tmp_path, checks)
    assert v["pass"] is False
    assert gate.accepts(tmp_path / "render.json", subject, render.RENDER_CHECKS)[0] is False


def test_a_truthy_non_true_pass_does_not_pass(tmp_path):
    checks = render.render_checks(GOOD, 65.8, OK)
    checks[0]["pass"] = 1
    assert verdict(tmp_path, checks)[1]["pass"] is False


# ---- the stage ----------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def gated(tmp_path_factory):
    """One gated, dry-voiced work dir per fixture case, built once; every test copies the case it needs."""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg and ffprobe are not installed (the dry voice stage writes silence with ffmpeg)")
    root = tmp_path_factory.mktemp("stage")
    return {case: dry_work(root / case, case) for case in CASES}


def fresh(gated, tmp_path, case="transcript"):
    w = tmp_path / "work"
    shutil.copytree(gated[case], w)
    return w


def stage_fakes(monkeypatch, w, **probe_overrides):
    calls = []

    def fake_hf(args, comps, timeout, log, **kw):
        calls.append(args[0])
        log.write_text("ok\n")
        if args[0] == "render":
            Path(args[3]).write_bytes(b"raw video")
        return 0

    def fake_mix(tl, voice_dir, kit, out):
        out.write_bytes(b"wav")
        return {}

    def fake_probe(p):
        total = json.loads((w / "build" / "timeline.json").read_text())["total"]
        return dict(GOOD, duration=total, **probe_overrides)

    def fake_sheet(video, tl, out, font=None):
        out.write_bytes(b"jpg")
        return out

    monkeypatch.setattr(render, "hf", fake_hf)
    monkeypatch.setattr(render.mix, "mixdown", fake_mix)
    monkeypatch.setattr(render, "remux", lambda raw, wav, out: out.write_bytes(raw.read_bytes() + b"+mix"))
    monkeypatch.setattr(render, "probe", fake_probe)
    monkeypatch.setattr(render, "contact_sheet", fake_sheet)
    return calls


def setup(gated, tmp_path, monkeypatch, case="transcript", dry_words=False, extra="", **probe_overrides):
    """A copy of the case's work dir, and a config folder (ORION_CLOCK_CONFIG) holding the voices.json the case was
    voiced with and settings with a stub kit. The fixtures are voiced in dry mode; unless dry_words, the words files
    are marked as a real (whisper) run, which is what a real render requires."""
    w = fresh(gated, tmp_path, case)
    if not dry_words:
        for f in (w / "voice").glob("*.words.json"):
            rec = json.loads(f.read_text())
            rec["source"] = "whisper"
            f.write_text(json.dumps(rec))
    use_config(monkeypatch, tmp_path, kit=make_kit(tmp_path), extra=extra)
    return w, stage_fakes(monkeypatch, w, **probe_overrides)


def ctx(w, dry=False):
    """The stage context, with the settings of the config folder setup() put in place."""
    return Ctx(day=date(2026, 10, 16), work=w, cfg=None, dry=dry, settings=load_settings(paths.config_dir()))


def sha(p):
    return files.sha256_bytes(Path(p).read_bytes())


@pytest.mark.parametrize("case", CASES)
def test_stage_writes_a_render_json_the_gate_accepts(gated, tmp_path, monkeypatch, case):
    w, calls = setup(gated, tmp_path, monkeypatch, case)
    r = render.run_stage(ctx(w))
    assert r.ok, r.reason
    assert calls == ["check", "render"]
    assert gate.accepts(w / "render.json", w / "episode.mp4", render.RENDER_CHECKS)[0] is True
    assert (w / "contact-sheet.jpg").exists()
    v = json.loads((w / "render.json").read_text())
    assert v["script_sha256"] == sha(w / "script.json") and v["ledger_sha256"] == sha(w / "ledger.json")
    total = json.loads((w / "build" / "timeline.json").read_text())["total"]
    assert v["dry"] is False and r.reason == f"{total:.2f} s, -14.1 LUFS"


def test_without_settings_the_stage_fails_closed(gated, tmp_path, monkeypatch):
    w, calls = setup(gated, tmp_path, monkeypatch)
    r = render.run_stage(Ctx(day=date(2026, 10, 16), work=w, cfg=None, dry=False, settings=None))
    assert (r.ok, r.reason) == (False, "settings_missing") and calls == []


def test_without_a_media_kit_the_stage_names_the_setting(gated, tmp_path, monkeypatch):
    w, calls = setup(gated, tmp_path, monkeypatch)
    no_kit = load_settings(use_config(monkeypatch, tmp_path / "nokit")[0])
    assert no_kit.render_kit is None
    r = render.run_stage(Ctx(day=date(2026, 10, 16), work=w, cfg=None, dry=False, settings=no_kit))
    assert (r.ok, r.reason) == (False, "missing:render.kit") and calls == []
    assert not (w / "render.json").exists()


def manifest(w):
    return json.loads((w / "voice" / "voice-manifest.json").read_text())


def set_manifest(w, **over):
    m = manifest(w)
    m.update(over)
    files.write_json(w / "voice" / "voice-manifest.json", m)


def test_the_case_was_voiced_with_the_config_folders_voices_bytes(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    assert paths.voices_path().read_bytes() == json.dumps(VOICES).encode("utf-8")
    assert manifest(w)["voices_sha256"] == sha(paths.voices_path())


def test_render_json_carries_the_values_the_voice_stage_recorded(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    set_manifest(w, ref_sha256="0123456789abcdef")  # the voice stage's record, not tts-results.json or a fresh read
    files.write_json(w / "voice" / "tts-results.json", {"device": "cpu", "ref_sha256": "ffffffffffffffff", "lines": []})
    assert render.run_stage(ctx(w)).ok
    v = json.loads((w / "render.json").read_text())
    assert v["voices_sha256"] == manifest(w)["voices_sha256"] == sha(paths.voices_path())
    assert v["ref_sha256"] == "0123456789abcdef"


def test_a_dry_render_json_carries_the_dry_voice_record(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch, dry_words=True)
    assert manifest(w)["ref_sha256"] is None  # the dry voice stage's own record
    files.write_json(w / "voice" / "tts-results.json", {"ref_sha256": "0123456789abcdef"})  # even if one is lying around
    assert render.run_stage(ctx(w, dry=True)).ok
    v = json.loads((w / "render-dry.json").read_text())
    assert v["voices_sha256"] == manifest(w)["voices_sha256"] and v["ref_sha256"] is None


def test_voices_json_changed_between_voicing_and_rendering_is_refused(gated, tmp_path, monkeypatch):
    w, calls = setup(gated, tmp_path, monkeypatch)
    live = paths.voices_path()
    live.write_bytes(live.read_bytes() + b"\n")
    r = render.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "voice_config_changed") and calls == []
    assert not (w / "render.json").exists()


def test_a_missing_voices_json_is_refused(gated, tmp_path, monkeypatch):
    w, calls = setup(gated, tmp_path, monkeypatch)
    paths.voices_path().unlink()
    r = render.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "voice_config_changed") and calls == []


def test_changed_voice_settings_are_refused_not_just_changed_text(gated, tmp_path, monkeypatch):
    """Same line ids, voices and texts, but a host param edited: the hash binding sees it."""
    w, calls = setup(gated, tmp_path, monkeypatch)
    data = json.loads(paths.voices_path().read_text())
    data["voices"]["host"]["params"]["exaggeration"] = 0.91
    paths.voices_path().write_text(json.dumps(data))
    r = render.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "voice_config_changed") and calls == []


def test_voices_bytes_that_hash_right_but_do_not_parse_are_refused(gated, tmp_path, monkeypatch):
    w, calls = setup(gated, tmp_path, monkeypatch)
    paths.voices_path().write_text("{not json")
    set_manifest(w, voices_sha256=sha(paths.voices_path()))
    r = render.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "voice_config_changed") and calls == []


@pytest.mark.parametrize("body", [None, "{broken", "[]", '{"schema": "x"}'])
def test_a_missing_or_foreign_voice_manifest_is_refused(gated, tmp_path, monkeypatch, body):
    w, calls = setup(gated, tmp_path, monkeypatch)
    p = w / "voice" / "voice-manifest.json"
    p.unlink() if body is None else p.write_text(body)
    r = render.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "no_voice_manifest") and calls == []


def test_lines_whose_params_differ_from_the_recorded_ones_are_refused(gated, tmp_path, monkeypatch):
    w, calls = setup(gated, tmp_path, monkeypatch)
    m = manifest(w)
    m["lines"][0]["params"] = dict(m["lines"][0]["params"], exaggeration=0.99)
    set_manifest(w, lines=m["lines"])
    r = render.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "voice_mismatch") and calls == []


def test_dry_stage_never_writes_the_real_artifacts(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch, dry_words=True)
    r = render.run_stage(ctx(w, dry=True))
    assert r.ok, r.reason
    assert r.reason.endswith(" (dry)")
    assert (w / "episode-dry.mp4").exists() and (w / "render-dry.json").exists()
    assert (w / "contact-sheet-dry.jpg").exists()
    for real in ("episode.mp4", "render.json", "contact-sheet.jpg"):
        assert not (w / real).exists()
    v = json.loads((w / "render-dry.json").read_text())
    assert v["dry"] is True and v["script_sha256"] == sha(w / "script.json")


def test_compose_gets_the_parsed_objects_the_settings_and_the_dry_flag(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch, dry_words=True)
    seen = {}
    real = compose.build

    def spy(work, day, script, ledger, **kw):
        seen.update(script=script, ledger=ledger, **kw)
        return real(work, day, script, ledger, **kw)

    monkeypatch.setattr(render.compose, "build", spy)
    assert render.run_stage(ctx(w, dry=True)).ok
    assert seen["script"] == json.loads((w / "script.json").read_text())
    assert seen["ledger"] == json.loads((w / "ledger.json").read_text()) and seen["dry"] is True
    s = load_settings(paths.config_dir())
    assert seen["kit"] == s.render_kit and seen["series"] == s.series and seen["handle"] == s.handle
    assert seen["voices"] == voiceconf.parse_voices_bytes(paths.voices_path().read_bytes())


def test_a_check_warning_stops_the_render(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)

    def warn_hf(args, comps, timeout, log, **kw):
        log.write_text(render.WARN + " low_contrast: caption box /secret/path\n")
        return 0

    monkeypatch.setattr(render, "hf", warn_hf)
    r = render.run_stage(ctx(w))
    assert not r.ok and r.reason == "hf_check"
    assert "low_contrast" in (w / "build" / "logs" / "check-problems.txt").read_text()
    assert not (w / "render.json").exists()


def test_a_render_log_problem_stops_the_render(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)

    def bad_hf(args, comps, timeout, log, **kw):
        log.write_text("ok\n" if args[0] == "check" else "PAGEERROR /home/x/secret\n")
        if args[0] == "render":
            Path(args[3]).write_bytes(b"raw")
        return 0

    monkeypatch.setattr(render, "hf", bad_hf)
    r = render.run_stage(ctx(w))
    assert not r.ok and r.reason == "hf_render"
    assert "PAGEERROR" in (w / "build" / "logs" / "render-problems.txt").read_text()
    assert not (w / "render.json").exists() and not (w / "episode.mp4").exists()


def test_failed_checks_fail_the_stage_and_record_a_failing_verdict(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch, lufs=-20.0)
    r = render.run_stage(ctx(w))
    assert not r.ok and r.reason == "render_checks:loudness"
    assert json.loads((w / "render.json").read_text())["pass"] is False


def fail_hf(monkeypatch):
    monkeypatch.setattr(render, "hf", lambda args, comps, timeout, log, **kw: (log.write_text("x"), 1)[1])


def test_a_failed_rerun_removes_the_previous_render_and_build(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    (w / "render.json").write_text("{}")
    (w / "episode.mp4").write_bytes(b"old episode")
    (w / "contact-sheet.jpg").write_bytes(b"old")
    (w / "build" / "comps" / "daily" / "assets").mkdir(parents=True)
    (w / "build" / "comps" / "daily" / "assets" / ".mix.part.wav").write_bytes(b"leftover")
    (w / "build" / "old.txt").write_text("x")
    fail_hf(monkeypatch)
    r = render.run_stage(ctx(w))
    assert not r.ok and r.reason == "hf_check_failed"
    for gone in ("render.json", "episode.mp4", "contact-sheet.jpg"):
        assert not (w / gone).exists()
    assert not (w / "build" / "old.txt").exists()
    assert not (w / "build" / "comps" / "daily" / "assets" / ".mix.part.wav").exists()


def test_a_dry_rerun_leaves_the_real_artifacts_alone_but_clears_its_own(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch, dry_words=True)
    (w / "render-dry.json").write_text("{}")
    (w / "episode-dry.mp4").write_bytes(b"old")
    (w / "render.json").write_text("real")
    fail_hf(monkeypatch)
    assert not render.run_stage(ctx(w, dry=True)).ok
    assert not (w / "render-dry.json").exists() and not (w / "episode-dry.mp4").exists()
    assert (w / "render.json").read_text() == "real"


def test_a_symlinked_build_dir_is_unlinked_not_followed(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    shutil.rmtree(w / "build", ignore_errors=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (w / "build").symlink_to(outside)
    assert render.run_stage(ctx(w)).ok
    assert (outside / "keep.txt").read_text() == "keep" and not (outside / "timeline.json").exists()
    assert not (w / "build").is_symlink()


def test_no_verdict_or_a_changed_script_is_not_accepted(gated, tmp_path, monkeypatch):
    w, calls = setup(gated, tmp_path, monkeypatch)
    (w / "script.json").write_text((w / "script.json").read_text() + " ")
    r = render.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "gate_not_accepted") and calls == []
    w2, calls2 = setup(gated, tmp_path / "b", monkeypatch)
    (w2 / "verdict.json").unlink()
    assert render.run_stage(ctx(w2)).reason == "gate_not_accepted" and calls2 == []


def test_the_gate_is_asked_for_the_text_checks_and_this_ledger_in_one_read(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    asked = []
    real = gate.read_verified

    def spy(*a, **k):
        asked.append(a)
        return real(*a, **k)

    monkeypatch.setattr(render.gate, "read_verified", spy)
    monkeypatch.setattr(render.gate, "accepts", lambda *a, **k: pytest.fail("a second, separate read"))
    assert render.run_stage(ctx(w)).ok
    assert asked == [(w / "verdict.json", w / "script.json", w / "ledger.json", gate.REQUIRED)]


def write_before_read(monkeypatch, write):
    """`write` runs just before the stage's single read of the gate files (gate.read_verified)."""
    real = gate.read_verified

    def write_then_read(*a, **k):
        write()
        return real(*a, **k)

    monkeypatch.setattr(render.gate, "read_verified", write_then_read)


@pytest.mark.parametrize("victim", ["script.json", "ledger.json"])
def test_bytes_that_change_before_the_gate_read_are_refused(gated, tmp_path, monkeypatch, victim):
    w, calls = setup(gated, tmp_path, monkeypatch)
    write_before_read(monkeypatch, lambda: (w / victim).write_text((w / victim).read_text() + " "))
    r = render.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "gate_not_accepted") and calls == []
    assert not (w / "render.json").exists()


SWAPS = {
    "pass_false": lambda v: v.update({"pass": False}),
    "pass_string": lambda v: v.update({"pass": "true"}),
    "no_checks": lambda v: v.update({"checks": []}),
    "missing_required": lambda v: v.update({"checks": [c for c in v["checks"] if c["name"] != gate.REQUIRED[-1]]}),
    "failing_check": lambda v: v["checks"][1].update({"pass": False}),
    "check_pass_string": lambda v: v["checks"][1].update({"pass": "true"}),
    "duplicated_check": lambda v: v["checks"].append(dict(v["checks"][1])),
}


@pytest.mark.parametrize("swap", list(SWAPS))
def test_a_verdict_swapped_in_before_the_read_is_judged_in_full(gated, tmp_path, monkeypatch, swap):
    """Hashes intact, but a short, failing or mistyped check list: the render does not start."""
    w, calls = setup(gated, tmp_path, monkeypatch)

    def swap_verdict():
        v = json.loads((w / "verdict.json").read_text())
        SWAPS[swap](v)
        (w / "verdict.json").write_text(json.dumps(v))

    write_before_read(monkeypatch, swap_verdict)
    r = render.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "gate_not_accepted") and calls == []
    assert not (w / "render.json").exists()


def test_a_vanishing_file_before_the_gate_read_is_refused(gated, tmp_path, monkeypatch):
    w, calls = setup(gated, tmp_path, monkeypatch)
    write_before_read(monkeypatch, lambda: (w / "ledger.json").unlink())
    assert render.run_stage(ctx(w)).reason == "gate_not_accepted" and calls == []


def test_no_voice_lines(gated, tmp_path, monkeypatch):
    w, calls = setup(gated, tmp_path, monkeypatch)
    shutil.rmtree(w / "voice")
    assert render.run_stage(ctx(w)).reason == "no_voice_lines" and calls == []


def mutate_lines(w, fn):
    lines = json.loads((w / "voice" / "lines.json").read_text())
    fn(lines)
    (w / "voice" / "lines.json").write_text(json.dumps(lines))


@pytest.mark.parametrize("fn", [
    lambda ls: ls[0].update(text=ls[0]["text"] + " extra"),
    lambda ls: ls[0].update(voice="other"),
    lambda ls: ls[0].update(id="cold_open2"),
    lambda ls: ls.pop(),
    lambda ls: ls.reverse(),
    lambda ls: ls.append(dict(ls[0])),
])
def test_voice_outputs_of_another_script_are_refused(gated, tmp_path, monkeypatch, fn):
    w, calls = setup(gated, tmp_path, monkeypatch)
    mutate_lines(w, fn)
    r = render.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "voice_mismatch") and calls == []


def test_unreadable_lines_json_is_a_voice_mismatch(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    (w / "voice" / "lines.json").write_text("{not json")
    assert render.run_stage(ctx(w)).reason == "voice_mismatch"


def test_the_none_case_has_no_confessional_in_the_render(gated, tmp_path, monkeypatch):
    w, calls = setup(gated, tmp_path, monkeypatch, "none")
    assert render.run_stage(ctx(w)).ok
    tl = json.loads((w / "build" / "timeline.json").read_text())
    assert "confessional" not in tl["scenes"] and tl["confessional_harness"] is None


def test_the_composition_carries_the_series_and_handle_from_settings(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    assert render.run_stage(ctx(w)).ok
    html = (w / "build" / "comps" / "daily" / "index.html").read_text(encoding="utf-8")
    s = load_settings(paths.config_dir())
    assert f'"brand": "{s.series.upper()}"' in html and f'"channel": "{s.handle}"' in html


def test_compose_errors_are_short_codes(gated, tmp_path, monkeypatch):
    w, calls = setup(gated, tmp_path, monkeypatch)

    def boom(*a, **k):
        raise ValueError("secret quote text /home/x/y")

    monkeypatch.setattr(render.compose, "build", boom)
    r = render.run_stage(ctx(w))
    assert r.reason == "compose:ValueError" and calls == []


def test_a_real_render_refuses_dry_words(gated, tmp_path, monkeypatch):
    w, calls = setup(gated, tmp_path, monkeypatch, dry_words=True)
    r = render.run_stage(ctx(w, dry=False))
    assert (r.ok, r.reason) == (False, "compose:ValueError") and calls == []
    assert not (w / "render.json").exists()


def test_mix_errors_are_short_codes(gated, tmp_path, monkeypatch):
    w, calls = setup(gated, tmp_path, monkeypatch, dry_words=True)

    def boom(*a, **k):
        raise FileNotFoundError("mix input missing: /home/x/secret.wav")

    monkeypatch.setattr(render.mix, "mixdown", boom)
    r = render.run_stage(ctx(w, dry=True))
    assert r.reason == "mix:FileNotFoundError" and calls == []


def test_hf_timeouts_and_failures(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch, dry_words=True)

    def slow(args, comps, timeout, log, **kw):
        raise subprocess.TimeoutExpired("hf", timeout)

    monkeypatch.setattr(render, "hf", slow)
    assert render.run_stage(ctx(w, dry=True)).reason == "hf_timeout"
    w2, _ = setup(gated, tmp_path / "b", monkeypatch, dry_words=True)

    def render_fails(args, comps, timeout, log, **kw):
        log.write_text("ok\n")
        return 0 if args[0] == "check" else 3

    monkeypatch.setattr(render, "hf", render_fails)
    assert render.run_stage(ctx(w2, dry=True)).reason == "hf_render_failed"


def test_probe_and_remux_errors_are_short_codes(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch, dry_words=True)

    def bad_probe(p):
        raise render.ProbeError("probe_failed")

    monkeypatch.setattr(render, "probe", bad_probe)
    r = render.run_stage(ctx(w, dry=True))
    assert r.reason == "render_probe:probe_failed" and not (w / "render-dry.json").exists()
    w2, _ = setup(gated, tmp_path / "b", monkeypatch, dry_words=True)

    def bad_remux(raw, wav, out):
        raise subprocess.CalledProcessError(1, ["ffmpeg", "/home/x/secret"])

    monkeypatch.setattr(render, "remux", bad_remux)
    assert render.run_stage(ctx(w2, dry=True)).reason == "render_probe:CalledProcessError"


def test_the_mix_goes_into_the_composition_assets_from_the_settings_kit(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch, dry_words=True)
    seen = {}

    def spy(tl, voice_dir, kit, out):
        seen.update(voice_dir=voice_dir, kit=kit, out=out)
        out.write_bytes(b"wav")

    monkeypatch.setattr(render.mix, "mixdown", spy)
    assert render.run_stage(ctx(w, dry=True)).ok
    assert seen["voice_dir"] == w / "voice" and seen["out"] == w / "build" / "comps" / "daily" / "assets" / "mix.wav"
    assert seen["kit"] == load_settings(paths.config_dir()).render_kit == tmp_path / "kit"


# ---- the HyperFrames call ------------------------------------------------------------------------------------------

def test_hf_argv_is_the_sandbox_then_the_command_then_the_args():
    hostile = "x; touch pwned; $(id)"
    argv = render.hf_argv(["render", hostile], 77, ("npx", "--no-install", "hyperframes"),
                          ("/opt/sandbox.sh", "--limit={timeout}", "{timeout}"))
    assert argv == ["/opt/sandbox.sh", "--limit=77", "77", "npx", "--no-install", "hyperframes", "render", hostile]
    assert render.hf_argv(["check", "daily"], 600) == ["hyperframes", "check", "daily"]  # the defaults: no sandbox
    assert render.hf_argv(["check"], 5, ("hf",), ()) == ["hf", "check"]


class FakeProc:
    def __init__(self, rc=0, hang=False):
        self.rc, self.hang, self.pid, self.waited = rc, hang, 424242, []

    def wait(self, timeout=None):
        self.waited.append(timeout)
        if self.hang and timeout is not None:
            raise subprocess.TimeoutExpired("hf", timeout)
        return self.rc


def test_hf_runs_from_comps_with_a_clean_env_in_a_session_of_its_own(tmp_path, monkeypatch):
    seen = {}
    proc = FakeProc(rc=0)

    def popen(argv, **kw):
        seen.update(argv=argv, kw=kw)
        return proc

    monkeypatch.setattr(render.subprocess, "Popen", popen)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    rc = render.hf(["render", "daily"], tmp_path, 77, tmp_path / "l.log", command=("hf",), sandbox=("sb", "{timeout}"))
    assert rc == 0 and seen["argv"] == ["sb", "77", "hf", "render", "daily"]
    kw = seen["kw"]
    assert kw["cwd"] == str(tmp_path) and kw["start_new_session"] is True and not kw.get("shell")
    assert kw["stdin"] is subprocess.DEVNULL and kw["stderr"] is subprocess.STDOUT
    assert kw["stdout"].name == str(tmp_path / "l.log")
    assert "DISPLAY" not in kw["env"] and "WAYLAND_DISPLAY" not in kw["env"]
    assert all(kw["env"][k] == v for k, v in render.HF_QUIET_ENV.items())
    assert proc.waited == [77 + render.KILL_GRACE_S]


def test_a_hung_hf_has_its_whole_process_group_killed(tmp_path, monkeypatch):
    proc = FakeProc(hang=True)
    killed = []
    monkeypatch.setattr(render.subprocess, "Popen", lambda argv, **kw: proc)
    monkeypatch.setattr(render.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    with pytest.raises(subprocess.TimeoutExpired):
        render.hf(["render"], tmp_path, 5, tmp_path / "l.log")
    assert killed == [(proc.pid, render.signal.SIGKILL)] and proc.waited[-1] is None  # killed, then reaped


FAKE_HF = r'''
import json, os, sys
from pathlib import Path
out = Path(os.environ["FAKE_HF_OUT"])
out.write_text(json.dumps({"cwd": os.getcwd(), "argv": sys.argv[1:], "display": os.environ.get("DISPLAY"),
                           "quiet": os.environ.get("HYPERFRAMES_NO_TELEMETRY"), "pgid_is_own": os.getpgid(0) == os.getpid()}))
print("checked", flush=True)
sys.exit(int(os.environ.get("FAKE_HF_RC", "0")))
'''


def test_hf_really_runs_the_command_with_the_values_as_plain_arguments(tmp_path, monkeypatch):
    script = tmp_path / "fake_hf.py"
    script.write_text(FAKE_HF)
    comps = tmp_path / "comps dir"
    comps.mkdir()
    monkeypatch.setenv("FAKE_HF_OUT", str(tmp_path / "out.json"))
    monkeypatch.setenv("DISPLAY", ":0")
    hostile = "$(touch pwned); rm -rf x"
    rc = render.hf(["render", "a b", hostile], comps, 42, tmp_path / "l.log", command=(sys.executable, str(script)))
    assert rc == 0
    got = json.loads((tmp_path / "out.json").read_text())
    assert got["cwd"] == str(comps) and got["argv"] == ["render", "a b", hostile]
    assert got["display"] is None and got["quiet"] == "1" and got["pgid_is_own"] is True
    assert (tmp_path / "l.log").read_text() == "checked\n" and not (comps / "pwned").exists()
    monkeypatch.setenv("FAKE_HF_RC", "3")
    assert render.hf(["check"], comps, 42, tmp_path / "l.log", command=(sys.executable, str(script))) == 3


SPAWNER = r'''
import os, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path(os.environ["FAKE_HF_CHILD"]).write_text(str(child.pid))
time.sleep(60)
'''


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return False
    return not any(line.startswith("State:") and "Z" in line.split()[1] for line in state.splitlines())


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs /proc to watch the child")
def test_a_real_hf_that_outlasts_its_timeout_is_killed_with_its_children(tmp_path, monkeypatch):
    script = tmp_path / "spawner.py"
    script.write_text(SPAWNER)
    marker = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_HF_CHILD", str(marker))
    monkeypatch.setattr(render, "KILL_GRACE_S", 0)
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        render.hf(["render"], tmp_path, 2, tmp_path / "l.log", command=(sys.executable, str(script)))
    assert time.monotonic() - t0 < 20
    pid = int(marker.read_text())
    deadline = time.monotonic() + 10
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid), "the renderer's child outlived the kill"


def test_the_stage_gives_hf_the_comps_dir_the_timeouts_and_the_settings_command(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch,
                 extra='hyperframes = ["npx", "--no-install", "hyperframes"]\nsandbox = ["/opt/sb.sh", "{timeout}"]\n')
    seen = []
    inner = render.hf

    def spy(args, comps, timeout, log, **kw):
        seen.append((args[0], comps, timeout, log, kw))
        return inner(args, comps, timeout, log, **kw)

    monkeypatch.setattr(render, "hf", spy)
    assert render.run_stage(ctx(w)).ok
    comps, logs = w / "build" / "comps", w / "build" / "logs"
    kw = {"command": ("npx", "--no-install", "hyperframes"), "sandbox": ("/opt/sb.sh", "{timeout}")}
    assert seen == [("check", comps, render.CHECK_TIMEOUT, logs / "check.log", kw),
                    ("render", comps, render.RENDER_TIMEOUT, logs / "render.log", kw)]


def test_remux_argv_nostdin_timeout_and_atomic(tmp_path, monkeypatch):
    raw, wav, out = tmp_path / "raw.mp4", tmp_path / "m.wav", tmp_path / "ep.mp4"
    raw.write_bytes(b"r")
    wav.write_bytes(b"w")
    out.write_bytes(b"old")
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, kw=kw, tmp=Path(argv[-1]))
        Path(argv[-1]).write_bytes(b"new")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(render.subprocess, "run", run)
    render.remux(raw, wav, out)
    argv, kw = seen["argv"], seen["kw"]
    assert argv[0] == "ffmpeg" and "-nostdin" in argv and kw["check"] is True and kw["timeout"] > 0
    assert out.read_bytes() == b"new" and not seen["tmp"].exists() and seen["tmp"] != out


def test_a_failed_remux_leaves_no_temp_and_the_old_file(tmp_path, monkeypatch):
    raw, wav, out = tmp_path / "raw.mp4", tmp_path / "m.wav", tmp_path / "ep.mp4"
    raw.write_bytes(b"r")
    wav.write_bytes(b"w")
    out.write_bytes(b"old")

    def run(argv, **kw):
        Path(argv[-1]).write_bytes(b"part")
        raise subprocess.TimeoutExpired("ffmpeg", 1)

    monkeypatch.setattr(render.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        render.remux(raw, wav, out)
    assert out.read_bytes() == b"old" and sorted(p.name for p in tmp_path.iterdir()) == ["ep.mp4", "m.wav", "raw.mp4"]


# ---- the contact sheet -----------------------------------------------------------------------------------------------

def test_strip_times_cover_the_video_and_its_boundaries():
    times = sheet.strip_times(3.0, boundaries=[1.0], every=2.0)
    assert times == [0.0, 0.7, 0.85, 1.0, 1.15, 2.0]
    assert sheet.strip_times(4.0, every=2.0) == [0.0, 2.0, 3.96]  # the end is clamped inside the video
    with pytest.raises(ValueError):
        sheet.strip_times(3.0, every=0)


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg and ffprobe are not installed")
    f = tmp_path_factory.mktemp("clip") / "c.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=navy:s=108x192:r=30:d=3",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(f)], check=True, timeout=60)
    return f


def test_the_contact_sheet_tiles_vertical_frames_six_across(clip, tmp_path):
    pil = pytest.importorskip("PIL.Image")
    tl = {"total": 3.0, "scenes": {"cold_open": {"start": 0.0}, "scoreboard": {"start": 1.0}}}
    stub_font = tmp_path / "not-a-font.ttf"
    stub_font.write_bytes(b"font")  # an unusable font falls back to the default one
    out = render.contact_sheet(clip, tl, tmp_path / "contact-sheet.jpg", font=stub_font)
    assert out == tmp_path / "contact-sheet.jpg" and sorted(p.name for p in tmp_path.iterdir()) == [
        "contact-sheet.jpg", "not-a-font.ttf"]
    with pil.open(out) as im:  # 6 frames (every 2 s plus four around the scoreboard start), one row of six
        assert im.size == (6 * sheet.TILE[0], sheet.TILE[1] + sheet.LABEL_H)


def test_a_long_render_makes_a_second_sheet_beside_the_first(clip, tmp_path):
    pytest.importorskip("PIL.Image")
    made = sheet.contact_sheets(clip, [0.0, 0.5, 1.0, 1.5, 2.0], tmp_path / "contact-sheet.jpg", cols=2, per_sheet=4)
    assert [p.name for p in made] == ["contact-sheet-1.jpg", "contact-sheet-2.jpg"] and all(p.is_file() for p in made)
    assert sheet.contact_sheets(clip, [], tmp_path / "none.jpg") == []


def test_a_frame_that_cannot_be_grabbed_is_an_error(tmp_path, monkeypatch):
    pytest.importorskip("PIL.Image")
    monkeypatch.setattr(sheet.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=b"", stderr=b"/home/x/secret"))
    with pytest.raises(RuntimeError) as e:
        sheet.contact_sheets(tmp_path / "v.mp4", [0.0], tmp_path / "s.jpg")
    assert "secret" not in str(e.value)


def test_frame_grabs_are_argv_lists_with_nostdin_and_a_timeout(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(sheet.subprocess, "run", lambda argv, **kw: (seen.update(argv=argv, kw=kw),
                                                                     subprocess.CompletedProcess(argv, 0))[1])
    sheet._grab(tmp_path / "v.mp4", 1.5)
    assert seen["argv"][0] == "ffmpeg" and "-nostdin" in seen["argv"] and "1.500" in seen["argv"]
    assert seen["kw"]["timeout"] == sheet.GRAB_TIMEOUT_S and seen["kw"]["stdin"] is subprocess.DEVNULL


# ---- review round: stage wiring, ordering, catch-all ------------------------------------------------------------------

def test_caption_mismatch_fails_the_stage_on_captions_match(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    real = compose.build

    def drop_a_word(*a, **k):
        tl = real(*a, **k)
        g = next(g for g in tl["caption_groups"] if g["words"])
        g["words"] = g["words"][:-1]
        return tl

    monkeypatch.setattr(render.compose, "build", drop_a_word)
    r = render.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "render_checks:captions_match")
    assert json.loads((w / "render.json").read_text())["pass"] is False


def test_the_probe_duration_is_held_to_the_timeline_total(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)

    def long_probe(p):
        total = json.loads((w / "build" / "timeline.json").read_text())["total"]
        return dict(GOOD, duration=total + 0.5)  # in the 60-75 window, but not the timeline's length

    monkeypatch.setattr(render, "probe", long_probe)
    r = render.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "render_checks:duration")


@pytest.mark.parametrize("dry", [False, True])
def test_remux_gets_this_runs_mix_and_raw_render(gated, tmp_path, monkeypatch, dry):
    w, _ = setup(gated, tmp_path, monkeypatch, dry_words=dry)
    got = []

    def spy(raw, wav, out):
        got.append((raw, wav, out, raw.read_bytes()))
        out.write_bytes(b"final")

    monkeypatch.setattr(render, "remux", spy)
    assert render.run_stage(ctx(w, dry=dry)).ok
    sfx = "-dry" if dry else ""
    assert got == [(w / "build" / f"episode-raw{sfx}.mp4", w / "build" / "comps" / "daily" / "assets" / "mix.wav",
                    w / f"episode{sfx}.mp4", b"raw video")]


def test_the_contact_sheet_gets_the_kits_body_font(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    seen = {}

    def spy(video, tl, out, font=None):
        seen.update(video=video, out=out, font=font)
        out.write_bytes(b"jpg")
        return out

    monkeypatch.setattr(render, "contact_sheet", spy)
    assert render.run_stage(ctx(w)).ok
    assert seen == {"video": w / "episode.mp4", "out": w / "contact-sheet.jpg",
                    "font": tmp_path / "kit" / "fonts" / "InterVariable.ttf"}


def test_a_dry_rerun_clears_build_and_never_remuxes_a_stale_raw(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch, dry_words=True)
    (w / "build").mkdir(exist_ok=True)
    (w / "build" / "episode-raw-dry.mp4").write_bytes(b"stale raw")
    remuxed = []
    monkeypatch.setattr(render, "hf", lambda args, comps, timeout, log, **kw: (log.write_text("ok\n"), 0)[1])
    monkeypatch.setattr(render, "remux", lambda raw, wav, out: remuxed.append(raw))
    r = render.run_stage(ctx(w, dry=True))
    assert (r.ok, r.reason) == (False, "hf_render_failed") and remuxed == []
    assert not (w / "episode-dry.mp4").exists() and not (w / "render-dry.json").exists()


def test_a_sheet_failure_leaves_no_render_json(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)

    def bad_sheet(video, tl, out, font=None):
        raise IndexError("no frames /home/x")

    monkeypatch.setattr(render, "contact_sheet", bad_sheet)
    r = render.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "render_probe:IndexError")
    assert not (w / "render.json").exists()


@pytest.mark.parametrize("target,attr,exc,code", [
    (render.mix, "mixdown", RuntimeError("/home/x"), "mix:RuntimeError"),
    (render.compose, "build", RuntimeError("/home/x"), "compose:RuntimeError"),
    (render, "remux", IndexError("/home/x"), "render_probe:IndexError"),
    (render, "probe", RuntimeError("/home/x"), "render_probe:RuntimeError"),
    (render, "hf", RuntimeError("/home/x"), "hf:RuntimeError"),
])
def test_any_exception_in_a_step_becomes_a_class_code(gated, tmp_path, monkeypatch, target, attr, exc, code):
    w, _ = setup(gated, tmp_path, monkeypatch)

    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(target, attr, boom)
    r = render.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, code)


def test_a_keyboard_interrupt_still_propagates(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)

    def stop(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(render.mix, "mixdown", stop)
    with pytest.raises(KeyboardInterrupt):
        render.run_stage(ctx(w))


def test_hf_oserror_is_hf_check_failed(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)

    def boom(args, comps, timeout, log, **kw):
        raise OSError("/home/x/secret")

    monkeypatch.setattr(render, "hf", boom)
    assert render.run_stage(ctx(w)).reason == "hf_check_failed"


def test_a_cleanup_failure_is_stale_cleanup(gated, tmp_path, monkeypatch):
    w, calls = setup(gated, tmp_path, monkeypatch)
    (w / "build").mkdir(exist_ok=True)

    def boom(path):
        raise PermissionError("/home/x/secret")

    monkeypatch.setattr(downstream.shutil, "rmtree", boom)
    r = render.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "stale_cleanup") and calls == []


@pytest.mark.parametrize("dry", [False, True])
def test_a_stale_remux_temp_is_cleared_at_start(gated, tmp_path, monkeypatch, dry):
    w, _ = setup(gated, tmp_path, monkeypatch, dry_words=dry)
    sfx = "-dry" if dry else ""
    temp = w / f".episode{sfx}.mp4.part.mp4"
    temp.write_bytes(b"killed mid-remux")
    fail_hf(monkeypatch)
    assert not render.run_stage(ctx(w, dry=dry)).ok
    assert not temp.exists()


@pytest.mark.parametrize("code", sorted(render.PROBE_CODES))
def test_each_probe_code_is_carried_into_the_reason(code):
    assert render.probe_failure_code(render.ProbeError(code)) == f"render_probe:{code}"


@pytest.mark.parametrize("msg", ["probe_failed /home/x/secret", "/home/x/secret", "", "PROBE_FAILED", "probe_failed\n"])
def test_probe_text_that_is_not_a_code_never_leaks(msg):
    assert render.probe_failure_code(render.ProbeError(msg)) == "render_probe:ProbeError"
    assert render.probe_failure_code(render.ProbeError("probe_failed", "/home/x/secret")) == "render_probe:ProbeError"
    assert render.probe_failure_code(RuntimeError("probe_failed")) == "render_probe:RuntimeError"


@pytest.mark.parametrize("fail,code", [(subprocess.TimeoutExpired("ffprobe", 1), "probe_timeout"),
                                       (subprocess.CalledProcessError(1, "ffprobe"), "probe_failed"),
                                       (ValueError("/home/x/secret"), "probe_unreadable")])
def test_a_real_ffprobe_failure_reaches_the_reason_as_its_code(tmp_path, monkeypatch, fail, code):
    def boom(*a, **k):
        raise fail
    monkeypatch.setattr(render.subprocess, "run", boom)
    with pytest.raises(render.ProbeError) as ei:
        render.probe(tmp_path / "x.mp4")
    assert render.probe_failure_code(ei.value) == f"render_probe:{code}"


def test_story_line_ids_show_in_evidence_instead_of_a_question_mark():
    lines = [{"id": "story_0", "text": "We shipped the fix."}, {"id": "story_1", "text": "Tests passed."}]
    ok, ev = render.captions_match({"caption_groups": []}, lines, {"story_0": 1.0, "story_1": 0.5})
    assert not ok and "mismatched story_0,story_1" in ev and "low coverage story_1 0.50" in ev and "?" not in ev
    assert render._name("story_0") == "story_0"
    assert [render._name(x) for x in ("A", "x y", "a" * 33, "", None, "story_0\n")] == ["?"] * 6


@pytest.mark.parametrize("sfx", ["", "-dry"])
def test_the_second_contact_sheet_of_an_earlier_long_render_is_archived_with_the_first(tmp_path, sfx):
    for name in (f"contact-sheet{sfx}.jpg", f"contact-sheet{sfx}-2.jpg", f"contact-sheet{sfx}-3.jpg",
                 f"episode{sfx}.mp4"):
        (tmp_path / name).write_bytes(b"old")
    write_passing_verdict(tmp_path, sfx)
    other = "contact-sheet.jpg" if sfx else "contact-sheet-dry-2.jpg"  # the other mode's files are not touched
    (tmp_path / other).write_bytes(b"keep")
    downstream.clear_stale(tmp_path, sfx)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([other, "previous"])
    (kept,) = (tmp_path / "previous").iterdir()
    assert sorted(p.name for p in kept.iterdir()) == [f"contact-sheet{sfx}-2.jpg", f"contact-sheet{sfx}-3.jpg",
                                                      f"contact-sheet{sfx}.jpg", f"episode{sfx}.mp4",
                                                      f"render{sfx}.json"]


@pytest.mark.parametrize("dry", [False, True])
@pytest.mark.parametrize("over,name", [({"lufs": -20.0}, "loudness"), ({"has_audio": False}, "audio_present"),
                                       ({"width": 720}, "format"), ({"fps": 24}, "format")])
def test_a_failing_check_leaves_no_episode_but_a_rejected_copy(gated, tmp_path, monkeypatch, over, name, dry):
    w, _ = setup(gated, tmp_path, monkeypatch, dry_words=dry, **over)
    sfx = "-dry" if dry else ""
    r = render.run_stage(ctx(w, dry=dry))
    assert not r.ok and name in r.reason
    assert not (w / f"episode{sfx}.mp4").exists() and not (w / f"contact-sheet{sfx}.jpg").exists()
    assert (w / f"episode-rejected{sfx}.mp4").read_bytes() == b"raw video+mix"
    assert (w / f"contact-sheet-rejected{sfx}.jpg").read_bytes() == b"jpg"
    v = json.loads((w / f"render{sfx}.json").read_text())
    assert v["pass"] is False
    assert v["rejected"] == [f"episode-rejected{sfx}.mp4", f"contact-sheet-rejected{sfx}.jpg"]
    assert v["subject_sha256"] == sha(w / f"episode-rejected{sfx}.mp4")


def test_a_failing_rerun_overwrites_older_rejected_files(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch, lufs=-20.0)
    (w / "episode-rejected.mp4").write_bytes(b"older")
    (w / "contact-sheet-rejected.jpg").write_bytes(b"older")
    assert not render.run_stage(ctx(w)).ok
    assert (w / "episode-rejected.mp4").read_bytes() == b"raw video+mix"
    assert (w / "contact-sheet-rejected.jpg").read_bytes() == b"jpg"


def test_a_passing_render_leaves_the_episode_and_no_rejected_file(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    (w / "episode-rejected.mp4").write_bytes(b"from an earlier run")
    (w / "contact-sheet-rejected.jpg").write_bytes(b"from an earlier run")
    r = render.run_stage(ctx(w))
    assert r.ok, r.reason
    assert (w / "episode.mp4").exists() and (w / "contact-sheet.jpg").exists()
    assert not list(w.glob("*rejected*"))
    assert "rejected" not in json.loads((w / "render.json").read_text())


def test_a_sheet_failure_leaves_no_episode_either(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    monkeypatch.setattr(render, "contact_sheet", lambda v, tl, out, font=None: (_ for _ in ()).throw(IndexError("x")))
    assert not render.run_stage(ctx(w)).ok
    assert not (w / "episode.mp4").exists() and (w / "episode-rejected.mp4").exists()


# ---- a rerun archives the previous render instead of deleting it ----

def old_render(w, tag=b"old", sfx="", verdict=True):
    (w / f"episode{sfx}.mp4").write_bytes(b"episode " + tag)
    (w / f"contact-sheet{sfx}.jpg").write_bytes(b"sheet " + tag)
    if verdict:
        write_passing_verdict(w, sfx)
    else:
        (w / f"render{sfx}.json").write_text("render " + tag.decode())


def archives(w, suffix=""):
    return sorted(p for p in (w / "previous").iterdir() if p.name.endswith(suffix)) if (w / "previous").exists() else []


def test_a_failed_rerun_archives_the_last_render_and_leaves_none_current(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    old_render(w)
    fail_hf(monkeypatch)
    assert not render.run_stage(ctx(w)).ok
    for name in ("episode.mp4", "render.json", "contact-sheet.jpg"):
        assert not (w / name).exists()
    (kept,) = archives(w)
    assert (kept / "episode.mp4").read_bytes() == b"episode old"
    assert json.loads((kept / "render.json").read_text())["pass"] is True
    assert (kept / "contact-sheet.jpg").read_bytes() == b"sheet old"
    assert not (w / "build").exists() or not (w / "build" / "old.txt").exists()


def test_every_rerun_archives_even_one_that_passes(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    old_render(w)
    r = render.run_stage(ctx(w))
    assert r.ok, r.reason
    (kept,) = archives(w)
    assert (kept / "episode.mp4").read_bytes() == b"episode old"
    assert (w / "episode.mp4").read_bytes() == b"raw video+mix"


def test_a_first_run_makes_no_archive(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    assert render.run_stage(ctx(w)).ok and not (w / "previous").exists()


def test_the_archive_is_named_by_a_utc_timestamp(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    old_render(w)
    fail_hf(monkeypatch)
    render.run_stage(ctx(w))
    (kept,) = archives(w)
    assert re.fullmatch(r"\d{8}T\d{6}\d{6}Z", kept.name)


def test_at_most_three_archived_runs_remain_and_the_oldest_go_first(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    fail_hf(monkeypatch)
    for i in range(6):
        old_render(w, tag=str(i).encode())
        assert not render.run_stage(ctx(w)).ok
    kept = archives(w)
    assert [(p / "episode.mp4").read_bytes() for p in kept] == [b"episode 3", b"episode 4", b"episode 5"]


def test_a_dry_rerun_neither_touches_nor_prunes_the_real_archives(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch, dry_words=True)
    fail_hf(monkeypatch)
    for i in range(3):
        old_render(w, tag=str(i).encode())
        render.run_stage(ctx(w))  # real runs would refuse dry words; the archive step still ran first
    real = archives(w)
    for i in range(5):
        old_render(w, tag=str(i).encode(), sfx="-dry")
        render.run_stage(ctx(w, dry=True))
    assert [p for p in archives(w) if not p.name.endswith("-dry")] == real
    assert len(archives(w, "-dry")) == 3


def test_symlinks_are_archived_as_links_and_never_followed(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "ep.mp4").write_bytes(b"precious")
    (w / "episode.mp4").symlink_to(outside / "ep.mp4")
    checks = [{"name": n, "pass": True, "evidence": "x"} for n in render.RENDER_CHECKS]
    render.write_verdict(outside / "v.json", "2026-10-16", outside / "ep.mp4", checks, False, script_sha256="a",
                         ledger_sha256="b")
    (w / "render.json").symlink_to(outside / "v.json")
    fail_hf(monkeypatch)
    assert not render.run_stage(ctx(w)).ok
    (kept,) = archives(w)
    assert (kept / "episode.mp4").is_symlink() and os.readlink(kept / "episode.mp4") == str(outside / "ep.mp4")
    assert (kept / "render.json").is_symlink()
    assert (outside / "ep.mp4").read_bytes() == b"precious"
    assert not (w / "episode.mp4").is_symlink() and not (w / "episode.mp4").exists()


def test_a_symlinked_previous_dir_is_replaced_not_followed(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    for n in ("20200101T000000000000Z", "20200102T000000000000Z", "20200103T000000000000Z"):
        (outside / n).mkdir()
    (w / "previous").symlink_to(outside)
    old_render(w)
    fail_hf(monkeypatch)
    assert not render.run_stage(ctx(w)).ok
    assert len(list(outside.iterdir())) == 3 and (outside / "20200101T000000000000Z").is_dir()
    assert not (w / "previous").is_symlink() and len(archives(w)) == 1


def test_a_symlinked_archive_entry_is_pruned_as_a_link(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    outside = tmp_path / "victim"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (outside / "episode.mp4").write_bytes(b"x")
    (w / "previous").mkdir()
    (w / "previous" / "20200101T000000000000Z").symlink_to(outside)
    for i in range(3):
        old_render(w, tag=str(i).encode())
        render.run_stage(ctx(w))
    assert (outside / "keep.txt").read_text() == "keep"
    assert not (w / "previous" / "20200101T000000000000Z").is_symlink()
    assert len(archives(w)) == 3


def test_an_archive_failure_is_stale_cleanup_and_loses_nothing(gated, tmp_path, monkeypatch):
    w, calls = setup(gated, tmp_path, monkeypatch)
    old_render(w)

    def boom(src, dst):
        raise PermissionError("/home/x")

    monkeypatch.setattr(render.os, "replace", boom)
    r = render.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "stale_cleanup") and calls == []
    assert (w / "episode.mp4").read_bytes() == b"episode old"


# ---- failed renders never use an archive slot ----

def setup_probe_fail(monkeypatch, w):
    total_of = lambda: json.loads((w / "build" / "timeline.json").read_text())["total"]  # noqa: E731
    monkeypatch.setattr(render, "probe", lambda p: dict(GOOD, duration=total_of(), lufs=-20.0))


def test_three_failed_reruns_after_a_good_render_keep_the_good_episode(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    assert render.run_stage(ctx(w)).ok
    good = (w / "episode.mp4").read_bytes()
    setup_probe_fail(monkeypatch, w)
    for _ in range(3):
        r = render.run_stage(ctx(w))
        assert not r.ok and "loudness" in r.reason
        assert (w / "render.json").exists() and not (w / "episode.mp4").exists()  # a lone failing verdict
    kept = [p for p in archives(w) if (p / "episode.mp4").exists()]
    assert len(kept) == 1 and (kept[0] / "episode.mp4").read_bytes() == good
    assert all((p / "episode.mp4").exists() for p in archives(w))  # no metadata-only archive was made


def test_a_lone_failing_render_json_and_its_sheets_are_removed_not_archived(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    (w / "render.json").write_text("failing")
    (w / "contact-sheet.jpg").write_bytes(b"s")
    (w / "contact-sheet-2.jpg").write_bytes(b"s")
    fail_hf(monkeypatch)
    render.run_stage(ctx(w))
    assert not (w / "previous").exists()
    assert not (w / "render.json").exists() and not (w / "contact-sheet.jpg").exists()
    assert not (w / "contact-sheet-2.jpg").exists()


def test_prune_counts_only_archives_with_an_episode_and_ignores_strays(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    prev = w / "previous"
    prev.mkdir()
    (prev / "zzz-stray.txt").write_text("stray")
    (prev / "99999999T999999999999Z-notes").mkdir()  # not an archive name
    meta_only = prev / "20200101T000000000000Z"
    meta_only.mkdir()
    (meta_only / "render.json").write_text("old metadata")
    fail_hf(monkeypatch)
    for i in range(5):
        old_render(w, tag=str(i).encode())
        render.run_stage(ctx(w))
    assert (prev / "zzz-stray.txt").read_text() == "stray" and (prev / "99999999T999999999999Z-notes").is_dir()
    withep = [p for p in prev.iterdir() if re.fullmatch(r"\d{8}T\d{12}Z(-\d+)?(-dry)?", p.name)
              and (p / "episode.mp4").exists()]
    assert [(p / "episode.mp4").read_bytes() for p in sorted(withep)] == [b"episode 2", b"episode 3", b"episode 4"]


def test_a_same_microsecond_archive_name_never_merges_into_an_archive(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    monkeypatch.setattr(downstream, "_utc_stamp", lambda: "20261018T010101000001Z")
    fail_hf(monkeypatch)
    for i in range(2):
        old_render(w, tag=str(i).encode())
        render.run_stage(ctx(w))
    got = {p.name: (p / "episode.mp4").read_bytes() for p in archives(w)}
    assert got == {"20261018T010101000001Z": b"episode 0", "20261018T010101000001Z-1": b"episode 1"}


def test_every_contact_sheet_of_a_failed_render_is_rejected(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch, lufs=-20.0)

    def two_sheets(video, tl, out, font=None):
        out.write_bytes(b"jpg")
        out.with_name("contact-sheet-2.jpg").write_bytes(b"jpg2")
        return out

    monkeypatch.setattr(render, "contact_sheet", two_sheets)
    assert not render.run_stage(ctx(w)).ok
    assert not list(w.glob("contact-sheet-[0-9]*.jpg")) and not (w / "contact-sheet.jpg").exists()
    assert (w / "contact-sheet-rejected-2.jpg").read_bytes() == b"jpg2"
    assert json.loads((w / "render.json").read_text())["rejected"] == [
        "episode-rejected.mp4", "contact-sheet-rejected.jpg", "contact-sheet-rejected-2.jpg"]
    assert render.run_stage(ctx(w)).reason  # the rerun clears the old rejected sheets
    assert (w / "contact-sheet-rejected-2.jpg").read_bytes() == b"jpg2"


# ---- only a render whose verdict passes and binds the episode is archived ----

def write_passing_verdict(w, sfx=""):
    checks = [{"name": n, "pass": True, "evidence": "x"} for n in render.RENDER_CHECKS]
    render.write_verdict(w / f"render{sfx}.json", "2026-10-16", w / f"episode{sfx}.mp4", checks, bool(sfx),
                         script_sha256="a", ledger_sha256="b")


def test_a_kill_after_remux_leaves_an_unverified_episode_that_takes_no_archive_slot(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    fail_hf(monkeypatch)
    for i in range(3):  # three genuine passing archives
        old_render(w, tag=str(i).encode())
        write_passing_verdict(w)
        render.run_stage(ctx(w))
    before = {p.name: (p / "episode.mp4").read_bytes() for p in archives(w)}
    assert len(before) == 3
    (w / "episode.mp4").write_bytes(b"remuxed, then the process died")  # no render.json
    (w / "contact-sheet.jpg").write_bytes(b"sheet")
    render.run_stage(ctx(w))
    assert {p.name: (p / "episode.mp4").read_bytes() for p in archives(w)} == before
    assert not (w / "episode.mp4").exists()
    assert (w / "episode-rejected.mp4").read_bytes() == b"remuxed, then the process died"


@pytest.mark.parametrize("breakage", ["failing", "other_episode", "wrong_stage", "garbage", "duplicate_key"])
def test_an_episode_without_a_verdict_that_passes_and_binds_it_is_rejected_not_archived(gated, tmp_path, monkeypatch,
                                                                                         breakage):
    w, _ = setup(gated, tmp_path, monkeypatch)
    old_render(w)
    write_passing_verdict(w)
    v = json.loads((w / "render.json").read_text())
    if breakage == "failing":
        v["pass"] = False
    elif breakage == "other_episode":
        v["subject_sha256"] = "0" * 64
    elif breakage == "wrong_stage":
        v["stage"] = "text"
    text = json.dumps(v)
    if breakage == "garbage":
        text = "{not json"
    if breakage == "duplicate_key":
        text = text.replace('"stage"', '"stage": "render", "stage"', 1)
    (w / "render.json").write_text(text)
    fail_hf(monkeypatch)
    render.run_stage(ctx(w))
    assert not (w / "previous").exists()
    assert (w / "episode-rejected.mp4").read_bytes() == b"episode old"
    assert not (w / "render.json").exists() and not (w / "episode.mp4").exists()


def test_a_passing_verdict_that_binds_the_episode_is_archived_with_its_verdict(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    old_render(w)
    write_passing_verdict(w)
    fail_hf(monkeypatch)
    render.run_stage(ctx(w))
    (kept,) = archives(w)
    assert (kept / "episode.mp4").read_bytes() == b"episode old" and json.loads((kept / "render.json").read_text())["pass"]
    assert not list(w.glob("*rejected*"))


# ---- what a new ledger makes stale (the collect stage calls this) ----

def test_clear_downstream_archives_a_good_render_and_removes_the_voice_outputs(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    old_render(w)
    old_render(w, tag=b"dry", sfx="-dry")
    (w / "episode-rejected.mp4").write_bytes(b"inspect me")
    downstream.clear_downstream(w)
    assert not (w / "voice").exists() and not (w / "hostlint.json").exists()
    assert not list(w.glob("episode*")) and not list(w.glob("render*.json")) and not (w / "build").exists()
    assert sorted((p / ("episode-dry.mp4" if p.name.endswith("-dry") else "episode.mp4")).read_bytes()
                  for p in archives(w)) == [b"episode dry", b"episode old"]


def test_a_dry_clear_downstream_touches_only_the_dry_media(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    old_render(w)
    old_render(w, tag=b"dry", sfx="-dry")
    downstream.clear_downstream(w, dry=True)
    assert (w / "voice" / "lines.json").exists() and (w / "episode.mp4").read_bytes() == b"episode old"
    assert (w / "render.json").exists() and not (w / "episode-dry.mp4").exists()
    (kept,) = archives(w)
    assert kept.name.endswith("-dry")


def test_clear_downstream_drops_an_unverified_episode_entirely(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    old_render(w, verdict=False)
    downstream.clear_downstream(w)
    assert not list(w.glob("episode*")) and not (w / "previous").exists()


# ---- voices.json is read once per render ----

def test_a_voices_file_replaced_after_the_stage_read_it_changes_nothing(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    live = paths.voices_path()
    used = live.read_bytes()
    assert b"#5CC8FF" in used
    real = compose.build

    def swap_then_build(*a, **k):
        live.write_bytes(used.replace(b"#5CC8FF", b"#FF0001"))  # a replacement after the stage's one read
        return real(*a, **k)

    monkeypatch.setattr(render.compose, "build", swap_then_build)
    r = render.run_stage(ctx(w))
    assert r.ok, r.reason
    html = (w / "build" / "comps" / "daily" / "index.html").read_text()
    assert "#5CC8FF" in html and "#FF0001" not in html
    assert json.loads((w / "render.json").read_text())["voices_sha256"] == files.sha256_bytes(used)


def test_the_stage_never_reopens_voices_after_its_one_read(gated, tmp_path, monkeypatch):
    w, _ = setup(gated, tmp_path, monkeypatch)
    seen = {}
    real = compose.build

    def spy(*a, **k):
        seen["voices"] = k.get("voices")
        return real(*a, **k)

    monkeypatch.setattr(render.compose, "build", spy)
    for name in ("load_voices", "load_voices_hashed", "load_voices_raw"):
        monkeypatch.setattr(voiceconf, name, lambda *a, **k: pytest.fail("voices.json reopened"))
    assert render.run_stage(ctx(w)).ok and isinstance(seen["voices"], dict)


def test_no_long_dashes_in_this_test_file():
    assert not [c for c in Path(__file__).read_text(encoding="utf-8") if c in LONG_DASHES]
