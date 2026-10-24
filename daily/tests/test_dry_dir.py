"""Dry runs get their own work folder: `<state>/<day>-dry/`. A dry run never creates, modifies, moves or deletes anything
under the real day's folder. The snapshot test is the heart: every file of a finished real day (path, bytes, mtime) is
unchanged, and nothing new appears there, after each dry entry point. Fakes only: no writer call, no voice, no render.
A rehearsal of a later stage (`--dry --from-stage voice` or later) is seeded by copy from the real day's gated text."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from daily import collect, files, gate, run, textcheck
from daily.ledger import RawSources
from daily.settings import Settings
from daily.sources.github import PR
from daily.stage import Ctx, StageResult
from daily.textcheck import TextChecks
from daily_helpers import good_script, make_world

DAY = dt.date(2026, 10, 16)
TODAY = dt.date(2026, 10, 17)
OK = gate.Deps(lambda t: ("PUBLISH", []), lambda t: [], lambda t: [])
SETTINGS = Settings(timezone="America/Los_Angeles", series="Demo Agents on the Clock", host_name="Sam",
                    disclosure="Narrated with an AI voice; agent lines are verbatim from logs.", github_owner="demo-owner")
SAID = "The importer now keeps every row in order on a full rerun."
MERGED = PR("owner/demo-tool", 7, "Keep rows in order", f"Intro.\n{SAID}", "https://github.com/owner/demo-tool/pull/7",
            "2026-10-16T21:00:00Z", "codex")


def snapshot(root: Path) -> dict:
    """Every entry under root: path -> (kind, sha256 for a file, mtime_ns). Directories count (a new empty one shows)."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        st = p.lstat()
        digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else None
        out[rel] = ("dir" if p.is_dir() else "file", digest, st.st_mtime_ns)
    return out


def gated_day(w: Path, cfg, dry: bool = False) -> Path:
    """A work folder holding a script, its ledger, the quoted source and a verdict the real gate wrote (scanners faked
    clean), so gate.read_verified_full accepts it."""
    w.mkdir(parents=True, exist_ok=True)
    _, qid, ledger = make_world(w)
    files.write_json(w / "ledger.json", ledger)
    files.write_json(w / "script.json", good_script(qid))
    files.write_text(w / "sources" / "github.jsonl", "")
    res = gate.run_stage(Ctx(DAY, w, cfg, dry, SETTINGS), deps=OK)
    assert res.ok, res.reason
    return w


@pytest.fixture
def world(monkeypatch, tmp_path, cfg_dir, cfg):
    state = tmp_path / "state"
    monkeypatch.setenv("ORION_CLOCK_STATE", str(state))
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(cfg_dir))
    calls = []
    # collect runs for real on a day with one merged PR; only its readers and the text checks are faked
    monkeypatch.setattr(collect, "gather", lambda day, cfg, settings: RawSources(prs=(MERGED,), arcs=(), claude=None,
                                                                                 codex=None, coverage={},
                                                                                 diagnostics={}))
    monkeypatch.setattr(textcheck, "load", lambda settings=None: TextChecks(lambda t: ("PUBLISH", []), lambda t: [],
                                                                                    lambda t: []))

    def fake_write(ctx):  # the writer: records the call; writes a script the gate can pass into ctx.work
        calls.append(("write", ctx.work))
        ledger = json.loads((ctx.work / "ledger.json").read_text())
        files.write_json(ctx.work / "writer-run.json", {"attempts": []})
        files.write_json(ctx.work / "script.json", good_script(ledger["quotes"][0]["id"]))
        return StageResult(ok=True)

    def fake_voice(ctx):  # what a voice stage leaves: voice/lines.json, in ctx.work
        calls.append(("voice", ctx.work))
        files.write_json(ctx.work / "voice" / "lines.json", [])
        return StageResult(ok=True)

    def fake_render(ctx):  # what a dry render leaves, in ctx.work
        calls.append(("render", ctx.work))
        sfx = "-dry" if ctx.dry else ""
        (ctx.work / f"episode{sfx}.mp4").write_bytes(b"dry episode")
        return StageResult(ok=True)

    stages = {"daily.collect": collect, "daily.writer": SimpleNamespace(run_stage=fake_write),
              "daily.gate": SimpleNamespace(run_stage=lambda ctx: gate.run_stage(ctx, deps=OK)),
              "daily.voice": SimpleNamespace(run_stage=fake_voice),
              "daily.render": SimpleNamespace(run_stage=fake_render)}
    monkeypatch.setattr(run, "load_stage", lambda name: stages.get(name))
    return SimpleNamespace(state=state, calls=calls, real_dir=state / DAY.isoformat(), cfg=cfg,
                           dry_dir=state / f"{DAY.isoformat()}-dry", tmp=tmp_path, stages=stages)


def build_real_day(world):
    """A finished real day: gated text, a voice folder, a stand-in episode, writer records, a done status, an
    archive and a lock file, every mtime set in the past so any touch shows."""
    w = gated_day(world.real_dir, world.cfg)
    files.write_json(w / "voice" / "lines.json", [{"id": "cold_open"}])
    (w / "episode.mp4").write_bytes(b"a real episode, byte for byte")
    (w / "writer-run.json").write_text("{}")
    (w / "writer-prompt.txt").write_text("prompt")
    files.write_json(w / "status.json", {"day": DAY.isoformat(), "stages": {
        n: {"state": "done", "reason": "", "at": "2026-10-17T00:00:00Z"} for n in run.NAMES}})
    (w / "previous" / "20261016T000000000000Z").mkdir(parents=True)
    (w / "previous" / "20261016T000000000000Z" / "episode.mp4").write_bytes(b"older")
    (w / "run.lock").write_text("")
    old = 1_700_000_000
    for p in w.rglob("*"):
        os.utime(p, (old, old), follow_symlinks=False)
    return w


def go(*extra):
    return run.main(["--day", "2026-10-16", "--dry", *extra], today=TODAY)


def dry_status(world):
    return json.loads((world.dry_dir / "status.json").read_text())["stages"]


ENTRY_POINTS = [("through_gate", ["--until", "gate"]), ("collect", ["--from-stage", "collect", "--until", "gate"]),
                ("voice", ["--from-stage", "voice", "--until", "voice"]),
                ("render", ["--from-stage", "render", "--until", "render"]), ("full", [])]


@pytest.mark.parametrize("name,argv", ENTRY_POINTS, ids=[n for n, _ in ENTRY_POINTS])
def test_a_dry_run_never_touches_a_finished_real_day(world, name, argv):
    real = build_real_day(world)
    before = snapshot(real)
    assert before and "episode.mp4" in before and "voice" in before and "previous" in before  # positive control
    go(*argv)
    assert snapshot(real) == before  # bytes, mtimes and entries: nothing changed, nothing created, nothing removed
    assert world.dry_dir.is_dir() and (world.dry_dir / "status.json").is_file()  # the run did happen, elsewhere
    assert (world.dry_dir / "run.lock").is_file()


def test_a_dry_run_through_the_gate_works_end_to_end_in_the_dry_directory(world):
    assert go("--until", "gate") == 0
    assert world.calls == [("write", world.dry_dir)]  # collect and the gate are real; only the writer is faked
    st = dry_status(world)
    assert [st[n]["state"] for n in ("collect", "write", "gate")] == ["dry", "dry", "dry"]
    d = world.dry_dir
    assert json.loads((d / "sources" / "github.jsonl").read_text())["number"] == 7  # the PR quote's record
    verdict = json.loads((d / "verdict.json").read_text())
    assert verdict["pass"] is True and verdict["dry"] is True
    assert gate.accepts(d / "verdict.json", d / "script.json", gate.REQUIRED, ledger_path=d / "ledger.json") == \
        (True, "ok")
    assert not world.real_dir.exists()  # the real day's directory is never even created


def test_a_full_dry_run_stops_closed_at_the_voice_stage_without_a_voices_file(world):
    """The real voice stage needs the operator's voices.json; without it a full run stops there, never skips past."""
    from daily import notify, voice
    world.stages.update({"daily.voice": voice, "daily.notify": notify})
    assert go() == 1
    st = dry_status(world)
    assert st["voice"] == {**st["voice"], "state": "failed", "reason": "voice_config:voices_file_unreadable"}
    assert st["notify"]["state"] == "dry" and "render" not in st and "publish" not in st   # a rehearsal reports nothing
    assert not world.real_dir.exists()


def test_a_real_run_still_uses_the_real_directory(world):
    run.main(["--day", "2026-10-16", "--until", "write"], today=TODAY)
    assert (world.real_dir / "status.json").is_file() and not world.dry_dir.exists()


def test_from_voice_on_a_gated_real_day_seeds_by_copy(world):
    real = build_real_day(world)
    before = snapshot(real)
    assert go("--from-stage", "voice", "--until", "voice") == 0
    d = world.dry_dir
    for name in ("ledger.json", "script.json", "verdict.json", "writer-run.json", "writer-prompt.txt",
                 "sources/github.jsonl"):
        if name == "verdict.json":  # written from the parse the judge made, so compared as JSON
            assert json.loads((d / name).read_text()) == json.loads((real / name).read_text())
        else:
            assert (d / name).read_bytes() == (real / name).read_bytes(), name  # copies, byte for byte
        assert not (d / name).is_symlink() and (d / name).stat().st_ino != (real / name).stat().st_ino
    st = dry_status(world)
    assert [st[n]["reason"] for n in ("collect", "write", "gate")] == ["seeded"] * 3
    assert world.calls == [("voice", d)]  # no collect, no writer, no gate: voiced in the dry directory
    assert snapshot(real) == before


def test_from_render_seeds_only_the_text_and_voices_dry_before_the_render(world):
    """A dry build takes only dry words, so the real voice/ is never copied: the rehearsal voices silence in its own
    folder first, then renders."""
    real = build_real_day(world)
    before = snapshot(real)
    assert go("--from-stage", "render", "--until", "render") == 0
    assert world.calls == [("voice", world.dry_dir), ("render", world.dry_dir)]
    st = dry_status(world)
    assert [st[n]["reason"] for n in ("collect", "write", "gate")] == ["seeded"] * 3
    assert (st["voice"]["state"], st["render"]["state"]) == ("dry", "dry")
    assert json.loads((world.dry_dir / "voice" / "lines.json").read_text()) == []   # the dry voicing, not the real one
    assert snapshot(real) == before


def _real_words_in(work):
    """A voicing with Whisper words (what a copy of a real voice/ would hold), which a dry build refuses."""
    text = "one two"
    files.write_json(work / "voice" / "lines.json", [{"id": "cold_open", "text": text}])
    files.write_json(work / "voice" / "cold_open.words.json", {
        "id": "cold_open", "text": text, "dur": 1.0, "coverage": 1.0, "source": "whisper",
        "words": [{"word": "one", "start": 0.0, "end": 0.5}, {"word": "two", "start": 0.5, "end": 1.0}]})


def test_a_rehearsal_from_render_reuses_a_dry_voicing_and_never_real_words(world):
    build_real_day(world)
    assert go("--from-stage", "render", "--until", "render") == 0       # seeds the text, voices dry, renders
    world.calls.clear()
    assert go("--from-stage", "render", "--until", "render") == 0
    assert world.calls == [("render", world.dry_dir)]                    # the dry voicing is reused
    assert dry_status(world)["voice"]["reason"] == "reused"
    _real_words_in(world.dry_dir)
    world.calls.clear()
    assert go("--from-stage", "render", "--until", "render") == 0
    assert world.calls == [("voice", world.dry_dir), ("render", world.dry_dir)]   # real words are voiced over


def test_seeding_is_skipped_when_the_dry_directory_already_holds_the_gated_outputs(world):
    build_real_day(world)
    go("--from-stage", "voice", "--until", "voice")
    mine = world.dry_dir / "script.json"
    old = 1_600_000_000
    os.utime(mine, (old, old))
    go("--from-stage", "voice", "--until", "voice")
    assert mine.stat().st_mtime == old  # verified as one read: not copied over again
    assert dry_status(world)["collect"]["reason"] == "reused"


def test_no_passing_real_verdict_starts_at_collect_in_the_dry_directory(world):
    real = build_real_day(world)
    (real / "verdict.json").write_text("{}")  # not a passing verdict
    os.utime(real / "verdict.json", (1_700_000_000, 1_700_000_000))
    before = snapshot(real)
    go("--from-stage", "voice", "--until", "gate")
    assert [c for c, _ in world.calls] == ["write"]  # collect is real (not recorded); the writer ran, in the dry dir
    assert all(w == world.dry_dir for _, w in world.calls)
    assert (world.dry_dir / "ledger.json").is_file()
    assert (world.dry_dir / "script.json").read_bytes() != (real / "script.json").read_bytes()  # nothing seeded
    assert snapshot(real) == before


@pytest.mark.parametrize("gone", ["verdict.json", "ledger.json", "script.json"])
def test_a_real_day_with_a_missing_gated_file_is_not_seeded(world, gone):
    real = build_real_day(world)
    (real / gone).unlink()
    before = snapshot(real)
    go("--from-stage", "voice", "--until", "write")
    assert "write" in [c for c, _ in world.calls]
    assert snapshot(real) == before


def test_a_dry_verdict_in_the_real_directory_is_not_seeded(world):
    w = gated_day(world.real_dir, world.cfg, dry=True)  # the verdict a dry gate wrote
    before = snapshot(w)
    go("--from-stage", "voice", "--until", "write")
    assert "write" in [c for c, _ in world.calls]
    assert snapshot(w) == before


def test_a_real_verdict_missing_one_required_check_is_not_seeded(world):
    real = build_real_day(world)
    v = json.loads((real / "verdict.json").read_text())
    v["checks"] = v["checks"][1:]
    files.write_json(real / "verdict.json", v)
    before = snapshot(real)
    go("--from-stage", "voice", "--until", "write")
    assert "write" in [c for c, _ in world.calls]
    assert dry_status(world)["collect"]["reason"] != "seeded"
    assert snapshot(real) == before


def test_a_real_run_never_reads_the_dry_directory(world):
    go("--until", "write")
    assert world.dry_dir.is_dir()
    run.main(["--day", "2026-10-16", "--until", "gate"], today=TODAY)
    assert not (world.real_dir / "status.json").read_text().count('"dry"')
    assert [w for _, w in world.calls if w == world.real_dir]  # the real run worked in the real directory


def test_a_half_copy_is_never_marked_done_and_the_run_starts_at_collect(world, monkeypatch):
    real = build_real_day(world)
    before = snapshot(real)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(run, "_copy_file", boom)
    go("--from-stage", "voice", "--until", "write")
    st = dry_status(world)
    assert st["collect"]["reason"] != "seeded" and "write" in [c for c, _ in world.calls]
    assert snapshot(real) == before


def test_a_dry_folder_whose_gated_text_does_not_verify_is_reseeded(world):
    real = build_real_day(world)
    go("--from-stage", "voice", "--until", "voice")
    (world.dry_dir / "script.json").write_text("{}")  # no longer what the verdict names
    go("--from-stage", "voice", "--until", "voice")
    assert (world.dry_dir / "script.json").read_bytes() == (real / "script.json").read_bytes()


def test_symlinks_inside_the_real_sources_are_copied_as_content(world):
    real = build_real_day(world)
    target = world.tmp / "outside.txt"
    target.write_text("outside content")
    (real / "sources" / "link.jsonl").symlink_to(target)
    before = snapshot(real)
    go("--from-stage", "voice", "--until", "voice")
    p = world.dry_dir / "sources" / "link.jsonl"
    assert p.is_file() and not p.is_symlink() and p.read_text() == "outside content"
    assert snapshot(real) == before and target.read_text() == "outside content"


def test_seeding_merges_into_an_existing_dry_folder_and_never_writes_through_a_symlink(world):
    real = build_real_day(world)
    (world.dry_dir / "sources").mkdir(parents=True)
    (world.dry_dir / "extra.txt").write_text("keep me")
    (world.dry_dir / "sources" / "old.jsonl").write_text("keep me too")
    outside = world.tmp / "outside.json"
    outside.write_text("do not overwrite")
    (world.dry_dir / "script.json").symlink_to(outside)
    (world.dry_dir / "writer-run.json").symlink_to(outside)
    go("--from-stage", "voice", "--until", "voice")
    assert outside.read_text() == "do not overwrite"
    for n in ("script.json", "writer-run.json"):
        assert not (world.dry_dir / n).is_symlink()
    assert (world.dry_dir / "script.json").read_bytes() == (real / "script.json").read_bytes()
    assert (world.dry_dir / "extra.txt").read_text() == "keep me"
    assert (world.dry_dir / "sources" / "old.jsonl").read_text() == "keep me too"
    assert (world.dry_dir / "sources" / "github.jsonl").is_file()


def test_the_seed_writes_the_bytes_the_judge_returned(world, monkeypatch):
    real = build_real_day(world)
    good = (real / "script.json").read_bytes()
    real_read = gate.read_verified_full

    def swapping(*a, **k):
        out = real_read(*a, **k)
        if Path(a[0]) == real / "verdict.json":
            (real / "script.json").write_text("swapped after the judgement")
        return out

    monkeypatch.setattr(gate, "read_verified_full", swapping)
    go("--from-stage", "voice", "--until", "voice")
    assert (world.dry_dir / "script.json").read_bytes() == good


@pytest.mark.parametrize("inside", [False, True])
def test_a_dry_folder_that_resolves_into_the_real_one_is_refused_with_nothing_written(world, capsys, inside):
    real = build_real_day(world)
    target = real / "sub" if inside else real
    if inside:
        target.mkdir()
        os.utime(target, (1_700_000_000, 1_700_000_000))
    os.utime(real, (1_700_000_000, 1_700_000_000))
    world.dry_dir.symlink_to(target)
    before = snapshot(real)
    assert go() == 1
    assert snapshot(real) == before and world.calls == []
    assert "dry_dir_is_real" in capsys.readouterr().err
