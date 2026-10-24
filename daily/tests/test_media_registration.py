"""The media stages are registered after the text gate and resolve to real modules; run.py carries a day from the gated
text through voice and render: a dry rehearsal with the renderer faked, the same rehearsal end to end with only the
HyperFrames command faked (real compose, mix, remux, probe, checks and contact sheet), the rehearsal carried on through
a dry publish and notify, and a real run with the TTS runner, Whisper and the renderer faked."""
import importlib
import json
import sys
import textwrap
from pathlib import Path

import pytest

from daily import align, files, gate, paths, render, run, voice, voiceconf
from media_helpers import (DAY, HAS_FFMPEG, VOICES, make_config, make_kit, make_sound_kit, needs_ffmpeg, work_dir)

GOOD = {"vcodec": "h264", "width": 1080, "height": 1920, "pix_fmt": "yuv420p", "fps": "30/1", "has_audio": True,
        "duration": 65.8, "lufs": -14.1}
REF = "abc123def4567890"
DRY_ARGS = ["--day", DAY.isoformat(), "--from-stage", "voice", "--until", "render", "--dry"]


def toml(s) -> str:
    return json.dumps(str(s))  # a JSON string is a valid TOML basic string


def snapshot(w: Path) -> list:
    return sorted((p.relative_to(w).as_posix(), p.read_bytes()) for p in w.rglob("*") if p.is_file())


def stages_of(w: Path) -> dict:
    return json.loads((w / "status.json").read_text())["stages"]


# ---- registration ----

def test_voice_and_render_follow_the_gate_in_the_stage_order():
    order = list(paths.STAGE_ORDER)
    assert order.index("voice") == order.index("gate") + 1
    assert order.index("render") == order.index("voice") + 1
    assert [n for n, _ in run.STAGES] == order


@pytest.mark.parametrize("stage,module", [("voice", "daily.voice"), ("render", "daily.render")])
def test_modules_map_and_each_stage_is_callable(stage, module):
    assert run.MODULES[stage] == module
    assert dict(run.STAGES)[stage] == module
    mod = importlib.import_module(module)
    assert callable(mod.run_stage) and getattr(mod, "REASON", None) != "not_yet_ported"  # no longer a placeholder


# ---- a dry rehearsal, the renderer faked ----

def fake_render_steps(monkeypatch, work: Path) -> list:
    """Stand-ins for the HyperFrames calls, the mix, the remux, the probe and the contact sheet; returns the hf calls."""
    calls = []

    def fake_hf(args, comps, timeout, log, **kw):
        calls.append(args[0])
        log.write_text("ok\n")
        if args[0] == "render":
            Path(args[3]).write_bytes(b"raw video")
        return 0

    def fake_probe(p):
        total = json.loads((work / "build" / "timeline.json").read_text())["total"]
        return dict(GOOD, duration=total)

    monkeypatch.setattr(render, "hf", fake_hf)
    monkeypatch.setattr(render.mix, "mixdown", lambda tl, vd, kit, out: out.write_bytes(b"wav") and {})
    monkeypatch.setattr(render, "remux", lambda raw, wav, out: out.write_bytes(raw.read_bytes() + b"+mix"))
    monkeypatch.setattr(render, "probe", fake_probe)
    monkeypatch.setattr(render, "contact_sheet", lambda video, tl, out, font=None: out.write_bytes(b"jpg") or out)
    return calls


@needs_ffmpeg
def test_a_dry_run_through_render_records_voice_and_render_as_dry(tmp_path, monkeypatch):
    w = work_dir(tmp_path)  # gated: script, ledger, sources and a verdict the real gate wrote
    cfg = make_config(tmp_path / "run-cfg", kit=make_kit(tmp_path))
    monkeypatch.setenv("ORION_CLOCK_STATE", str(tmp_path))
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(cfg))
    d = tmp_path / "2026-10-16-dry"  # the rehearsal's own directory, seeded by copy from the gated real day
    calls = fake_render_steps(monkeypatch, d)
    before = snapshot(w)
    assert run.main(DRY_ARGS) == 0
    assert snapshot(w) == before  # the real day is only read
    got = stages_of(d)
    assert got["collect"]["state"] == got["write"]["state"] == got["gate"]["state"] == "done"
    assert got["voice"]["state"] == "dry" and got["render"]["state"] == "dry"
    assert "publish" not in got and "notify" not in got
    assert calls == ["check", "render"]
    assert (d / "voice" / "lines.json").exists() and (d / "hostlint.json").exists()
    assert (d / "render-dry.json").exists() and not (d / "render.json").exists()
    assert gate.accepts(d / "render-dry.json", d / "episode-dry.mp4", gate.RENDER_REQUIRED) == (True, "ok")
    assert not (w / "voice").exists() and not list(w.glob("render*.json"))


@needs_ffmpeg
def test_a_dry_run_through_every_stage_rehearses_publish_and_notify_without_a_browser_or_a_report(tmp_path,
                                                                                                    monkeypatch):
    from daily import notify, publish
    w = work_dir(tmp_path)
    cfg = make_config(tmp_path / "run-cfg", kit=make_kit(tmp_path))
    (cfg / "publish.json").write_text(json.dumps({"visibility": "public"}))   # even with public mode on
    monkeypatch.setenv("ORION_CLOCK_STATE", str(tmp_path))
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(cfg))
    d = tmp_path / "2026-10-16-dry"
    fake_render_steps(monkeypatch, d)
    monkeypatch.setattr(publish, "_run_child", lambda *a, **k: pytest.fail("started the Studio driver"))
    monkeypatch.setattr(notify, "send", lambda *a, **k: pytest.fail("sent a report"))
    before = snapshot(w)
    assert run.main([a for a in DRY_ARGS if a not in ("--until", "render")]) == 0
    assert snapshot(w) == before  # the real day is only read
    got = stages_of(d)
    assert [got[n]["state"] for n in ("voice", "render", "publish", "notify")] == ["dry"] * 4
    assert got["publish"]["reason"] == "dry_run" and got["notify"]["reason"] == "dry run: nothing sent"
    item = json.loads((d / "upload-item.json").read_text())
    assert item["dry"] is True and item["visibility"] == "public"
    assert item["sha256"] == json.loads((d / "render-dry.json").read_text())["subject_sha256"]
    assert not (d / "upload.json").exists() and not (d / "notify.json").exists()
    assert not (tmp_path / "notify").exists() and not (tmp_path / "uploads").exists()


def test_a_dry_run_without_a_kit_stops_closed_at_render(tmp_path, monkeypatch):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg and ffprobe are not installed")
    work_dir(tmp_path)
    monkeypatch.setenv("ORION_CLOCK_STATE", str(tmp_path))
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(make_config(tmp_path / "run-cfg")))  # no [render] kit
    d = tmp_path / "2026-10-16-dry"
    calls = fake_render_steps(monkeypatch, d)
    assert run.main(DRY_ARGS) == 1
    got = stages_of(d)
    assert got["voice"]["state"] == "dry"
    assert (got["render"]["state"], got["render"]["reason"]) == ("failed", "missing:render.kit") and calls == []


# ---- the same rehearsal end to end: only the HyperFrames command is faked ----

FAKE_HYPERFRAMES = r'''
"""A stand-in for the HyperFrames CLI: `check <comp>` passes; `render <comp> -o OUT ...` writes a navy 1080x1920
30 fps h264 video as long as the composition says (its root's data-duration)."""
import re
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:1] == ["check"]:
    print("check passed")
    sys.exit(0)
if args[:1] != ["render"]:
    sys.exit(2)
out = args[args.index("-o") + 1]
html = (Path.cwd() / args[1] / "index.html").read_text(encoding="utf-8")
total = re.search(r'id="root"[^>]*data-duration="([0-9.]+)"', html).group(1)
subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=navy:s=1080x1920:r=30",
                "-t", total, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", out], check=True,
               timeout=600)
print("rendered", total)
'''


def media_stack() -> bool:
    try:
        import numpy  # noqa: F401
        import PIL  # noqa: F401
    except ImportError:
        return False
    return HAS_FFMPEG


@pytest.mark.skipif(not media_stack(), reason="needs ffmpeg, numpy and Pillow")
def test_a_dry_run_end_to_end_with_only_the_hyperframes_command_faked(tmp_path, monkeypatch):
    w = work_dir(tmp_path)
    kit = make_sound_kit(tmp_path)
    fake = tmp_path / "bin" / "fake_hyperframes.py"
    fake.parent.mkdir()
    fake.write_text(FAKE_HYPERFRAMES)
    extra = f"\n[render]\nkit = {toml(kit)}\nhyperframes = [{toml(sys.executable)}, {toml(fake)}]\n"
    monkeypatch.setenv("ORION_CLOCK_STATE", str(tmp_path))
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(make_config(tmp_path / "run-cfg", extra=extra)))
    before = snapshot(w)
    assert run.main(DRY_ARGS) == 0, stages_of(tmp_path / "2026-10-16-dry")
    assert snapshot(w) == before
    d = tmp_path / "2026-10-16-dry"
    got = stages_of(d)
    assert got["voice"]["state"] == "dry" and got["render"]["state"] == "dry", got
    v = json.loads((d / "render-dry.json").read_text())
    assert v["pass"] is True and v["dry"] is True and v["stage"] == "render"
    assert [c["name"] for c in v["checks"]] == list(gate.RENDER_REQUIRED) and all(c["pass"] for c in v["checks"])
    assert gate.accepts(d / "render-dry.json", d / "episode-dry.mp4", gate.RENDER_REQUIRED) == (True, "ok")
    assert (d / "contact-sheet-dry.jpg").stat().st_size > 1000
    assert v["voices_sha256"] == files.sha256_bytes(json.dumps(VOICES).encode("utf-8")) and v["ref_sha256"] is None
    assert v["script_sha256"] == files.sha256_bytes((d / "script.json").read_bytes())
    html = (d / "build" / "comps" / "daily" / "index.html").read_text(encoding="utf-8")
    assert "DEMO AGENTS ON THE CLOCK" in html and "@DemoChannel" in html
    for real in ("episode.mp4", "render.json", "contact-sheet.jpg"):
        assert not (d / real).exists() and not (w / real).exists()


# ---- a real run, voice to render: the TTS runner, Whisper and the renderer faked ----

FAKE_RUNNER = textwrap.dedent(f'''
    import json, sys, wave
    from pathlib import Path

    lines = json.loads(Path(sys.argv[1]).read_text())
    out = Path(sys.argv[2])
    for ln in lines:
        with wave.open(str(out / (ln["id"] + ".wav")), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\\x00\\x00" * int(24000 * 0.3 * len(ln["text"].split())))  # a cloned voice's pace
    rows = [{{"id": ln["id"], "voice": ln["voice"], "text": ln["text"], "chunks": 1, "dur": 1.0}} for ln in lines]
    (out / "tts-results.json").write_text(json.dumps({{"device": "cpu", "ref_sha256": "{REF}", "lines": rows}}))
    print("device cpu: %d line(s)" % len(rows))
    print("RUNNER_OK")
''')


def heard_every_word(jobs, manifest, python, model=None, log=None):
    for j in jobs:
        Path(j["out"]).write_text(json.dumps([{"word": t, "start": 0.3 * i, "end": 0.3 * (i + 1)}
                                              for i, t in enumerate(j["text"].split())]))


@needs_ffmpeg
def test_a_real_run_from_voice_to_render_ends_in_a_render_verdict_the_gate_accepts(tmp_path, monkeypatch):
    w = work_dir(tmp_path)
    runner = tmp_path / "bin" / "fake_runner.py"
    runner.parent.mkdir()
    runner.write_text(FAKE_RUNNER)
    extra = (f"\n[tts]\npython = {toml(sys.executable)}\n\n[whisper]\npython = {toml(tmp_path / 'no-whisper')}\n"
             f"\n[render]\nkit = {toml(make_kit(tmp_path))}\n")
    cfg = make_config(tmp_path / "run-cfg", extra=extra)
    monkeypatch.setenv("ORION_CLOCK_STATE", str(tmp_path))
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(cfg))
    monkeypatch.setattr(voice, "TTS_RUNNER", runner)
    monkeypatch.setattr(voiceconf, "ref_ok", lambda v: (True, REF))
    monkeypatch.setattr(align, "run_whisper", heard_every_word)
    calls = fake_render_steps(monkeypatch, w)
    files.write_json(w / "status.json", {"day": DAY.isoformat(), "stages": {
        n: {"state": "done", "reason": "", "at": "2026-10-17T12:30:00Z"} for n in ("collect", "write", "gate")}})
    assert run.main(["--day", DAY.isoformat(), "--from-stage", "voice", "--until", "render"]) == 0, stages_of(w)
    got = stages_of(w)
    assert got["voice"]["state"] == "done" and got["render"]["state"] == "done", got
    assert calls == ["check", "render"]
    lines = json.loads((w / "voice" / "lines.json").read_text())
    for ln in lines:
        rec = json.loads((w / "voice" / f"{ln['id']}.words.json").read_text())
        assert rec["source"] == "whisper" and rec["coverage"] == 1.0
    assert (w / "voice" / "confessional.raw.wav").exists()  # the agent chain really ran on the quote
    manifest = json.loads((w / "voice" / voice.MANIFEST).read_text())
    assert manifest["tts_target"] == "local" and manifest["device"] == "cpu" and manifest["ref_sha256"] == REF
    assert gate.accepts(w / "render.json", w / "episode.mp4", gate.RENDER_REQUIRED) == (True, "ok")
    v = json.loads((w / "render.json").read_text())
    assert v["dry"] is False and v["ref_sha256"] == REF
    assert v["voices_sha256"] == files.sha256_bytes((cfg / "voices.json").read_bytes())
    assert v["script_sha256"] == files.sha256_bytes((w / "script.json").read_bytes())
    assert v["ledger_sha256"] == files.sha256_bytes((w / "ledger.json").read_bytes())
    assert not (tmp_path / "2026-10-16-dry").exists()


@pytest.mark.skipif(not media_stack(), reason="needs ffmpeg, numpy and Pillow")
def test_a_dry_rehearsal_from_render_after_a_real_voicing_renders_with_the_real_compose(tmp_path, monkeypatch):
    """A day really voiced (Whisper words, the agent chain applied), then `--dry --from-stage render`: the rehearsal
    copies only the gated text, voices silence in its own folder before the dry render (a dry build takes only dry
    words), and the real compose, mix and checks pass with only the HyperFrames command faked. The real takes are never
    copied into the rehearsal, and the real day is only read."""
    w = work_dir(tmp_path)
    runner = tmp_path / "bin" / "fake_runner.py"
    runner.parent.mkdir()
    runner.write_text(FAKE_RUNNER)
    fake = tmp_path / "bin" / "fake_hyperframes.py"
    fake.write_text(FAKE_HYPERFRAMES)
    extra = (f"\n[tts]\npython = {toml(sys.executable)}\n\n[whisper]\npython = {toml(tmp_path / 'no-whisper')}\n"
             f"\n[render]\nkit = {toml(make_sound_kit(tmp_path))}\nhyperframes = [{toml(sys.executable)}, {toml(fake)}]\n")
    monkeypatch.setenv("ORION_CLOCK_STATE", str(tmp_path))
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(make_config(tmp_path / "run-cfg", extra=extra)))
    monkeypatch.setattr(voice, "TTS_RUNNER", runner)
    monkeypatch.setattr(voiceconf, "ref_ok", lambda v: (True, REF))
    monkeypatch.setattr(align, "run_whisper", heard_every_word)
    files.write_json(w / "status.json", {"day": DAY.isoformat(), "stages": {
        n: {"state": "done", "reason": "", "at": "2026-10-17T12:30:00Z"} for n in ("collect", "write", "gate")}})
    assert run.main(["--day", DAY.isoformat(), "--from-stage", "voice", "--until", "voice"]) == 0, stages_of(w)
    assert json.loads((w / "voice" / "cold_open.words.json").read_text())["source"] == "whisper"
    before = snapshot(w)
    d = tmp_path / "2026-10-16-dry"
    assert run.main(["--day", DAY.isoformat(), "--from-stage", "render", "--until", "render", "--dry"]) == 0, \
        stages_of(d)
    assert snapshot(w) == before
    got = stages_of(d)
    assert [got[n]["state"] for n in ("collect", "write", "gate")] == ["done"] * 3
    assert (got["voice"]["state"], got["render"]["state"]) == ("dry", "dry"), got
    lines = json.loads((d / "voice" / "lines.json").read_text())
    assert {json.loads((d / "voice" / f"{ln['id']}.words.json").read_text())["source"] for ln in lines} == {"dry"}
    assert not (d / "voice" / "confessional.raw.wav").exists()   # no real take was copied in
    v = json.loads((d / "render-dry.json").read_text())
    assert v["pass"] is True and v["dry"] is True
    assert gate.accepts(d / "render-dry.json", d / "episode-dry.mp4", gate.RENDER_REQUIRED) == (True, "ok")


@needs_ffmpeg
def test_a_real_run_without_the_tts_interpreter_stops_closed_at_voice(tmp_path, monkeypatch):
    w = work_dir(tmp_path)
    extra = f"\n[whisper]\npython = {toml(tmp_path / 'no-whisper')}\n\n[render]\nkit = {toml(make_kit(tmp_path))}\n"
    monkeypatch.setenv("ORION_CLOCK_STATE", str(tmp_path))
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(make_config(tmp_path / "run-cfg", extra=extra)))
    monkeypatch.setattr(voiceconf, "ref_ok", lambda v: (True, REF))
    calls = fake_render_steps(monkeypatch, w)
    files.write_json(w / "status.json", {"day": DAY.isoformat(), "stages": {
        n: {"state": "done", "reason": "", "at": "2026-10-17T12:30:00Z"} for n in ("collect", "write", "gate")}})
    assert run.main(["--day", DAY.isoformat(), "--from-stage", "voice", "--until", "render"]) == 1
    got = stages_of(w)
    assert (got["voice"]["state"], got["voice"]["reason"]) == ("failed", "missing:tts.python")
    assert "render" not in got and calls == []
