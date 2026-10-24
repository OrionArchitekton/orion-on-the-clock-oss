"""Verdict acceptance (gate.accepts) and the gate stage. accepts is the only way a consumer reads a verdict: it re-reads
both files, recomputes the hash and never raises. The stage reads script.json and ledger.json from the work directory,
writes verdict.json, and reports the first failing check by name, never script or quote text."""
import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest

from daily import files, paths
from daily import gate as G
from daily.ledger import quote_id
from daily.settings import Settings
from daily.stage import Ctx, StageResult
from daily_helpers import QUOTE, good_script, make_world, quiet_script, words, write_jsonl

DAY = dt.date(2026, 10, 16)
# Settings whose log roots do not exist: only the work directory can hold a quoted source.
SETTINGS = Settings(timezone="America/Los_Angeles", series="Demo Agents on the Clock", host_name="Sam",
                    disclosure="Narrated with an AI voice; agent lines are verbatim from logs.", github_owner="demo-owner",
                    claude_projects=Path("/nonexistent-root/projects"),
                    codex_db=Path("/nonexistent-root/codex/state_5.sqlite"))
OK = G.Deps(lambda t: ("PUBLISH", []), lambda t: [], lambda t: [])
TRIGGERS = G.Deps(lambda t: ("ABSTAIN", [("server", "ro***ud")]) if "rosebud" in t else ("PUBLISH", []),
                  lambda t: [], lambda t: [])


@pytest.fixture(autouse=True)
def only_the_work_dir_is_a_source_root(tmp_path, monkeypatch):
    """Nothing but the work directory is an allowed source root, so a quote proves only if the stage passes it on."""
    monkeypatch.setattr(paths, "DEFAULT_CLAUDE_PROJECTS", tmp_path / "no-projects")
    monkeypatch.setattr(paths, "DEFAULT_CODEX_DB", tmp_path / "no-codex" / "state_5.sqlite")


def prepare(tmp_path, cfg, **over):
    _, qid, ledger = make_world(tmp_path)
    (tmp_path / "ledger.json").write_text(json.dumps(ledger))
    (tmp_path / "script.json").write_text(json.dumps(good_script(qid, **over)))
    return Ctx(DAY, tmp_path, cfg, False, SETTINGS)


def accepts(tmp_path, required=G.REQUIRED):
    return G.accepts(tmp_path / "verdict.json", tmp_path / "script.json", required)


def read_verdict(tmp_path):
    return json.loads((tmp_path / "verdict.json").read_text())


def rewrite_verdict(tmp_path, fn):
    v = read_verdict(tmp_path)
    fn(v)
    (tmp_path / "verdict.json").write_text(json.dumps(v))


def set_check(name, **fields):
    def edit(v):
        for c in v["checks"]:
            if c["name"] == name:
                c.update(fields)
    return edit


def passed_stage(tmp_path, cfg):
    res = G.run_stage(prepare(tmp_path, cfg), deps=OK)
    assert (res.ok, res.reason) == (True, "")  # every refusal below starts from a verdict that was accepted
    assert accepts(tmp_path) == (True, "ok")


# ---- accepts ----

def test_a_passing_stage_writes_an_accepted_verdict(tmp_path, cfg):  # positive control
    res = G.run_stage(prepare(tmp_path, cfg), deps=OK)
    assert res == StageResult(ok=True, reason="")
    assert accepts(tmp_path) == (True, "ok")
    v = read_verdict(tmp_path)
    assert (v["schema"], v["stage"], v["day"], v["pass"]) == ("daily-build/verdict/v1", "gate", "2026-10-16", True)
    assert [c["name"] for c in v["checks"]] == list(G.REQUIRED)
    assert v["subject_sha256"] == hashlib.sha256((tmp_path / "script.json").read_bytes()).hexdigest()


def test_editing_the_script_after_the_verdict_is_refused(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    (tmp_path / "script.json").write_text((tmp_path / "script.json").read_text() + " ")
    assert accepts(tmp_path) == (False, "sha_mismatch")


def test_a_missing_verdict_is_refused(tmp_path, cfg):
    prepare(tmp_path, cfg)
    assert accepts(tmp_path) == (False, "unreadable:FileNotFoundError")


def test_a_missing_subject_is_refused(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    (tmp_path / "script.json").unlink()
    assert accepts(tmp_path) == (False, "unreadable:FileNotFoundError")


@pytest.mark.parametrize("raw,reason", [
    ("{not json", "unreadable:JSONDecodeError"),
    ("", "unreadable:JSONDecodeError"),
    ("[" * 100000, "unreadable:RecursionError"),
    (b"\xff\xfe\x00", "unreadable:UnicodeDecodeError"),
])
def test_an_unreadable_verdict_is_refused(tmp_path, cfg, raw, reason):
    passed_stage(tmp_path, cfg)
    path = tmp_path / "verdict.json"
    path.write_bytes(raw if isinstance(raw, bytes) else raw.encode())
    assert accepts(tmp_path) == (False, reason)


def test_a_verdict_path_that_is_a_folder_is_refused(tmp_path, cfg):
    prepare(tmp_path, cfg)
    (tmp_path / "verdict.json").mkdir()
    assert accepts(tmp_path) == (False, "unreadable:IsADirectoryError")


def test_a_repeated_key_is_refused_even_when_the_last_copy_passes(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    path = tmp_path / "verdict.json"
    path.write_text('{"pass": false,' + path.read_text().lstrip()[1:])  # json.loads alone would keep the last: true
    assert accepts(tmp_path) == (False, "duplicate_key")


@pytest.mark.parametrize("verdict", ["[]", "null", '"daily-build/verdict/v1"', "7"])
def test_a_verdict_that_is_not_an_object_is_refused(tmp_path, cfg, verdict):
    passed_stage(tmp_path, cfg)
    (tmp_path / "verdict.json").write_text(verdict)
    assert accepts(tmp_path) == (False, "bad_schema")


def test_a_wrong_schema_tag_is_refused(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    rewrite_verdict(tmp_path, lambda v: v.update(schema="something/else"))
    assert accepts(tmp_path) == (False, "bad_schema")


def test_a_verdict_without_a_schema_tag_is_refused(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    rewrite_verdict(tmp_path, lambda v: v.pop("schema"))
    assert accepts(tmp_path) == (False, "bad_schema")


def test_a_verdict_without_a_hash_is_refused(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    rewrite_verdict(tmp_path, lambda v: v.pop("subject_sha256"))
    assert accepts(tmp_path) == (False, "sha_mismatch")


def test_a_verdict_missing_a_check_is_refused(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    rewrite_verdict(tmp_path, lambda v: v.update(checks=[c for c in v["checks"] if c["name"] != "word_budget"]))
    assert accepts(tmp_path) == (False, "missing:word_budget")


def test_every_missing_check_is_listed_in_order(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    rewrite_verdict(tmp_path, lambda v: v.update(checks=v["checks"][:2]))
    assert accepts(tmp_path) == (False, "missing:" + ",".join(G.REQUIRED[2:]))


@pytest.mark.parametrize("checks", [None, {"schema": {"name": "schema", "pass": True}}, "schema", 3, [None, 5, "x"]])
def test_a_checks_field_that_is_not_a_list_of_checks_is_refused(tmp_path, cfg, checks):
    passed_stage(tmp_path, cfg)
    rewrite_verdict(tmp_path, lambda v: v.update(checks=checks))
    assert accepts(tmp_path) == (False, "missing:" + ",".join(G.REQUIRED))


def test_a_check_whose_name_is_not_text_does_not_count(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    rewrite_verdict(tmp_path, lambda v: v["checks"][0].update(name=["schema"]))
    assert accepts(tmp_path) == (False, "missing:schema")


def test_a_failing_check_is_refused(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    rewrite_verdict(tmp_path, set_check("names", **{"pass": False}))
    assert accepts(tmp_path) == (False, "failed:names")


@pytest.mark.parametrize("flag", ["true", "yes", 1, 1.0, None, [True], {}])
def test_a_check_pass_that_is_not_the_boolean_true_is_refused(tmp_path, cfg, flag):
    passed_stage(tmp_path, cfg)
    rewrite_verdict(tmp_path, set_check("names", **{"pass": flag}))
    assert accepts(tmp_path) == (False, "failed:names")


def test_a_check_with_no_pass_field_is_refused(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    rewrite_verdict(tmp_path, lambda v: v["checks"][3].pop("pass"))
    assert accepts(tmp_path) == (False, "failed:quote_in_ledger")


def test_a_check_listed_twice_is_refused_even_when_one_copy_passes(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    rewrite_verdict(tmp_path, lambda v: v["checks"].append({"name": "names", "pass": False, "evidence": "x"}))
    assert accepts(tmp_path) == (False, "duplicate_check:names")


def test_a_false_pass_flag_is_refused(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    rewrite_verdict(tmp_path, lambda v: v.update({"pass": False}))
    assert accepts(tmp_path) == (False, "verdict_not_pass")


@pytest.mark.parametrize("flag", ["true", 1, None])
def test_a_verdict_pass_that_is_not_the_boolean_true_is_refused(tmp_path, cfg, flag):
    passed_stage(tmp_path, cfg)
    rewrite_verdict(tmp_path, lambda v: v.update({"pass": flag}))
    assert accepts(tmp_path) == (False, "verdict_not_pass")


def test_a_verdict_with_no_pass_flag_is_refused(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    rewrite_verdict(tmp_path, lambda v: v.pop("pass"))
    assert accepts(tmp_path) == (False, "verdict_not_pass")


def test_a_check_beyond_the_required_ones_is_not_demanded(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    rewrite_verdict(tmp_path, lambda v: v["checks"].append({"name": "extra", "pass": False, "evidence": "x"}))
    assert accepts(tmp_path) == (True, "ok")
    assert accepts(tmp_path, G.REQUIRED + ("extra",)) == (False, "failed:extra")


def test_the_required_names_are_the_callers_to_choose(tmp_path, cfg):
    """The render gate reuses accepts with RENDER_REQUIRED over a different subject (a video's bytes)."""
    video = tmp_path / "episode.mp4"
    video.write_bytes(b"\x00\x01video bytes\xff")
    render = {"schema": G.VERDICT_SCHEMA, "day": "2026-10-16", "stage": "render", "pass": True,
              "subject_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
              "checks": [{"name": n, "pass": True, "evidence": "x"} for n in G.RENDER_REQUIRED]}
    (tmp_path / "render.json").write_text(json.dumps(render))
    assert G.accepts(tmp_path / "render.json", video, G.RENDER_REQUIRED) == (True, "ok")
    assert G.accepts(tmp_path / "render.json", video, G.REQUIRED)[1].startswith("missing:schema,quiet")
    video.write_bytes(video.read_bytes() + b"\x00")
    assert G.accepts(tmp_path / "render.json", video, G.RENDER_REQUIRED) == (False, "sha_mismatch")


@pytest.mark.parametrize("required", [None, 5, [3, 4]])
def test_accepts_never_raises_even_for_bad_required_names(tmp_path, cfg, required):
    passed_stage(tmp_path, cfg)
    ok, reason = G.accepts(tmp_path / "verdict.json", tmp_path / "script.json", required)
    assert ok is False and reason.startswith(("error:", "missing:"))


def test_a_refusal_reason_holds_only_codes_and_required_names(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    rewrite_verdict(tmp_path, lambda v: v["checks"].append({"name": QUOTE, "pass": False, "evidence": QUOTE}))
    rewrite_verdict(tmp_path, set_check("slop", **{"pass": False}))
    assert accepts(tmp_path) == (False, "failed:slop") and QUOTE not in accepts(tmp_path)[1]


# ---- the stage ----

def test_a_passing_stage_leaves_no_temp_file_behind(tmp_path, cfg):
    G.run_stage(prepare(tmp_path, cfg), deps=OK)
    names = {p.name for p in tmp_path.iterdir()}
    assert "verdict.json" in names and not [n for n in names if n.startswith(".")]  # write_text's temp file is gone


def test_stage_without_a_script_fails_and_writes_nothing(tmp_path, cfg):
    res = G.run_stage(Ctx(DAY, tmp_path, cfg, False, SETTINGS), deps=OK)
    assert (res.ok, res.reason) == (False, "missing:script.json")
    assert not (tmp_path / "verdict.json").exists()


def test_stage_without_a_ledger_fails_and_writes_nothing(tmp_path, cfg):
    ctx = prepare(tmp_path, cfg)
    (tmp_path / "ledger.json").unlink()
    res = G.run_stage(ctx, deps=OK)
    assert (res.ok, res.reason) == (False, "missing:ledger.json")
    assert not (tmp_path / "verdict.json").exists()


def test_a_script_that_is_a_folder_counts_as_missing(tmp_path, cfg):
    ctx = prepare(tmp_path, cfg)
    (tmp_path / "script.json").unlink()
    (tmp_path / "script.json").mkdir()
    assert G.run_stage(ctx, deps=OK) == StageResult(ok=False, reason="missing:script.json")


@pytest.mark.parametrize("raw", ["{broken", "[]", "null", '"x"', "[" * 100000])
def test_an_unreadable_ledger_fails_the_stage_and_writes_nothing(tmp_path, cfg, raw):
    ctx = prepare(tmp_path, cfg)
    (tmp_path / "ledger.json").write_text(raw)
    res = G.run_stage(ctx, deps=OK)
    assert (res.ok, res.reason) == (False, "unreadable:ledger.json")
    assert not (tmp_path / "verdict.json").exists()


def test_a_failed_run_does_not_leave_an_older_verdict_to_be_accepted(tmp_path, cfg):
    ctx = prepare(tmp_path, cfg)
    assert G.run_stage(ctx, deps=OK).ok is True
    assert accepts(tmp_path) == (True, "ok")  # the older verdict is valid, until the next run fails
    (tmp_path / "ledger.json").unlink()
    assert G.run_stage(ctx, deps=OK) == StageResult(ok=False, reason="missing:ledger.json")
    assert not (tmp_path / "verdict.json").exists()
    assert accepts(tmp_path) == (False, "unreadable:FileNotFoundError")


def test_a_failing_gate_fails_the_stage_and_keeps_the_verdict(tmp_path, cfg):
    res = G.run_stage(prepare(tmp_path, cfg, **{"cold_open.on_screen": "Zephyr rules"}), deps=OK)
    assert (res.ok, res.reason) == (False, "names")
    v = read_verdict(tmp_path)
    assert v["pass"] is False and [c["name"] for c in v["checks"] if not c["pass"]] == ["names"]
    assert accepts(tmp_path) == (False, "failed:names")  # a consumer refuses what the stage refused


def test_the_reason_is_the_first_failing_check_in_the_required_order(tmp_path, cfg):
    ctx = prepare(tmp_path, cfg, **{"cold_open.on_screen": "Zephyr rules", "story.host": [words(5)]})
    res = G.run_stage(ctx, deps=OK)
    assert [c["name"] for c in read_verdict(tmp_path)["checks"] if not c["pass"]] == ["names", "word_budget"]
    assert (res.ok, res.reason) == (False, "names")


def test_a_quiet_script_that_reaches_the_gate_fails_on_quiet(tmp_path, cfg):
    ctx = prepare(tmp_path, cfg)
    (tmp_path / "script.json").write_text(json.dumps(quiet_script()))
    res = G.run_stage(ctx, deps=OK)
    assert (res.ok, res.reason) == (False, "quiet")
    assert read_verdict(tmp_path)["pass"] is False


def test_a_script_that_is_not_json_fails_on_schema(tmp_path, cfg):
    ctx = prepare(tmp_path, cfg)
    (tmp_path / "script.json").write_text("not json at all")
    res = G.run_stage(ctx, deps=OK)
    assert (res.ok, res.reason) == (False, "schema")
    assert [c["name"] for c in read_verdict(tmp_path)["checks"]] == list(G.REQUIRED)


def test_the_reason_never_holds_script_or_quote_text(tmp_path, cfg):
    secret_words = "Zephyr rosebud " + QUOTE
    ctx = prepare(tmp_path, cfg, **{"cold_open.on_screen": secret_words, "description": secret_words})
    res = G.run_stage(ctx, deps=TRIGGERS)
    assert res.ok is False and res.reason in G.REQUIRED
    for fragment in ("Zephyr", "rosebud", "single test"):
        assert fragment not in res.reason


def test_a_scanner_that_raises_fails_the_stage_by_name_instead_of_crashing(tmp_path, cfg):
    def boom(text):
        raise RuntimeError("scanner down")
    res = G.run_stage(prepare(tmp_path, cfg), deps=G.Deps(boom, lambda t: [], lambda t: []))
    assert (res.ok, res.reason) == (False, "redactor")


def test_the_stage_is_not_fooled_by_a_verdict_that_does_not_read_back_as_written(tmp_path, cfg, monkeypatch):
    """ok comes from re-reading the file on disk through accepts, not from the in-memory verdict."""
    def tampered(path, obj):
        files.write_json(path, {**obj, "subject_sha256": "0" * 64})
    monkeypatch.setattr(G, "write_json", tampered)
    res = G.run_stage(prepare(tmp_path, cfg), deps=OK)
    assert (res.ok, res.reason) == (False, "sha_mismatch")


def test_the_script_changing_while_the_gate_runs_is_refused(tmp_path, cfg, monkeypatch):
    ctx = prepare(tmp_path, cfg)
    real = G.gate

    def gate_then_edit(*args, **kwargs):
        verdict = real(*args, **kwargs)
        (tmp_path / "script.json").write_text((tmp_path / "script.json").read_text() + " ")
        return verdict
    monkeypatch.setattr(G, "gate", gate_then_edit)
    assert G.run_stage(ctx, deps=OK) == StageResult(ok=False, reason="sha_mismatch")


# ---- the day and the ledger the verdict was made from ----

def test_a_ledger_for_another_day_than_the_run_fails_before_gating(tmp_path, cfg):
    prepare(tmp_path, cfg)  # a 10-16 ledger and a 10-16 script
    assert G.run_stage(Ctx(DAY, tmp_path, cfg, False, SETTINGS), deps=OK).ok is True  # positive control: the run day matches
    res = G.run_stage(Ctx(dt.date(2026, 10, 20), tmp_path, cfg, False, SETTINGS), deps=OK)
    assert res == StageResult(ok=False, reason="day_mismatch")
    assert not (tmp_path / "verdict.json").exists()  # nothing was gated, and the earlier verdict is gone


@pytest.mark.parametrize("day", ["2026-10-17", "", None, 20261016, "2026-10-16T00:00:00"])
def test_a_ledger_whose_day_is_not_the_run_day_fails(tmp_path, cfg, day):
    ctx = prepare(tmp_path, cfg)
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    ledger["day"] = day
    (tmp_path / "ledger.json").write_text(json.dumps(ledger))
    assert G.run_stage(ctx, deps=OK) == StageResult(ok=False, reason="day_mismatch")
    assert not (tmp_path / "verdict.json").exists()


def test_a_ledger_with_no_day_fails(tmp_path, cfg):
    ctx = prepare(tmp_path, cfg)
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    del ledger["day"]
    (tmp_path / "ledger.json").write_text(json.dumps(ledger))
    assert G.run_stage(ctx, deps=OK) == StageResult(ok=False, reason="day_mismatch")


def test_the_verdict_records_the_hash_of_the_ledger_it_was_made_from(tmp_path, cfg):
    G.run_stage(prepare(tmp_path, cfg), deps=OK)
    v = read_verdict(tmp_path)
    assert v["ledger_sha256"] == hashlib.sha256((tmp_path / "ledger.json").read_bytes()).hexdigest()
    assert v["ledger_sha256"] != v["subject_sha256"]
    assert accepts(tmp_path) == (True, "ok")  # an audit field: accepts still binds the verdict to the script


def test_a_failing_verdict_records_the_ledger_hash_too(tmp_path, cfg):
    G.run_stage(prepare(tmp_path, cfg, **{"cold_open.on_screen": "Zephyr rules"}), deps=OK)
    assert read_verdict(tmp_path)["ledger_sha256"] == hashlib.sha256(
        (tmp_path / "ledger.json").read_bytes()).hexdigest()


def test_another_ledger_gives_another_ledger_hash(tmp_path, cfg):
    ctx = prepare(tmp_path, cfg)
    G.run_stage(ctx, deps=OK)
    first = read_verdict(tmp_path)["ledger_sha256"]
    (tmp_path / "ledger.json").write_text((tmp_path / "ledger.json").read_text() + "\n")  # same ledger, other bytes
    G.run_stage(ctx, deps=OK)
    assert read_verdict(tmp_path)["ledger_sha256"] != first


def test_the_recorded_ledger_hash_is_of_the_bytes_the_gate_read(tmp_path, cfg, monkeypatch):
    ctx = prepare(tmp_path, cfg)
    before = hashlib.sha256((tmp_path / "ledger.json").read_bytes()).hexdigest()
    real = G.gate

    def gate_then_edit_ledger(*args, **kwargs):
        verdict = real(*args, **kwargs)
        (tmp_path / "ledger.json").write_text("{}")
        return verdict
    monkeypatch.setattr(G, "gate", gate_then_edit_ledger)
    assert G.run_stage(ctx, deps=OK) == StageResult(ok=False, reason="ledger_mismatch")  # the stage binds the ledger
    assert read_verdict(tmp_path)["ledger_sha256"] == before


# ---- what the stage passes to the gate ----

def test_the_stage_passes_the_work_dir_and_the_ctx_config_to_the_gate(tmp_path, cfg, monkeypatch):
    ctx = prepare(tmp_path, cfg)
    seen = {}
    real = G.gate

    def spy(script_bytes, ledger, config, deps, **kwargs):
        seen.update(script=script_bytes, ledger=ledger, cfg=config, deps=deps, kwargs=kwargs)
        return real(script_bytes, ledger, config, deps, **kwargs)
    monkeypatch.setattr(G, "gate", spy)
    assert G.run_stage(ctx, deps=OK).ok is True
    assert seen["kwargs"] == {"work_dir": tmp_path, "settings": SETTINGS} and seen["cfg"] is cfg and seen["deps"] is OK
    assert seen["script"] == (tmp_path / "script.json").read_bytes()
    assert seen["ledger"] == json.loads((tmp_path / "ledger.json").read_text())


def pr_quote_world(work, source_dir):
    """A ledger whose one quote is a PR body line in <source_dir>/github.jsonl; returns (ledger, quote id)."""
    src = write_jsonl(source_dir / "github.jsonl", [
        {"repo": "owner/demo-tool", "number": 7, "harness": "codex", "closed_at": "2026-10-16T21:00:00Z",
         "body": "## Summary\n" + QUOTE + "\nThe rest of it."}])
    qid = quote_id("codex", QUOTE, str(src), 1)
    ledger = {"schema": "daily-build/ledger/v1", "day": "2026-10-16", "items": [{"id": "pr:demo-tool#7", "kind": "pr"}],
              "sessions": [], "quotes": [{"id": qid, "kind": "pr", "harness": "codex", "lane": "demo-tool",
                                          "disclosure": "public", "text": QUOTE, "ts": "2026-10-16T21:00:00Z",
                                          "source": {"path": str(src), "line": 1, "record": "pr:demo-tool#7"}}]}
    (work / "ledger.json").write_text(json.dumps(ledger))
    (work / "script.json").write_text(json.dumps(good_script(qid)))
    return ledger


def test_a_pr_quote_in_the_work_dir_sources_folder_proves_through_the_stage(tmp_path, cfg):  # positive control
    work = tmp_path / "work"
    work.mkdir()
    pr_quote_world(work, work / "sources")
    res = G.run_stage(Ctx(DAY, work, cfg, False, SETTINGS), deps=OK)
    assert (res.ok, res.reason) == (True, "")
    evidence = {c["name"]: c["evidence"] for c in read_verdict(work)["checks"]}
    assert evidence["quote_verbatim"] == "matched github.jsonl:1"
    assert G.accepts(work / "verdict.json", work / "script.json", G.REQUIRED) == (True, "ok")


def test_the_same_pr_quote_outside_the_work_dir_fails_on_quote_verbatim(tmp_path, cfg):
    work = tmp_path / "work"
    work.mkdir()
    pr_quote_world(work, tmp_path / "elsewhere")
    res = G.run_stage(Ctx(DAY, work, cfg, False, SETTINGS), deps=OK)
    assert (res.ok, res.reason) == (False, "quote_verbatim")
    evidence = {c["name"]: c["evidence"] for c in read_verdict(work)["checks"]}
    assert evidence["quote_verbatim"] == "outside_roots"


def test_without_deps_the_stage_uses_the_default_scanners(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(G, "default_deps", lambda settings=None: TRIGGERS)
    assert G.run_stage(prepare(tmp_path, cfg, description="A rosebud day in the build.")) == StageResult(
        ok=False, reason="redactor")
    monkeypatch.setattr(G, "default_deps", lambda settings=None: OK)  # positive control: clean scanners pass
    assert G.run_stage(prepare(tmp_path, cfg)) == StageResult(ok=True, reason="")


def test_the_stage_hands_the_ctx_settings_to_the_scanners_and_the_roots(tmp_path, cfg, monkeypatch, settings):
    seen = {}

    def deps(s=None):
        seen["deps"] = s
        return OK
    monkeypatch.setattr(G, "default_deps", deps)
    ctx = prepare(tmp_path, cfg)
    ctx = Ctx(ctx.day, ctx.work, ctx.cfg, ctx.dry, settings)
    assert G.run_stage(ctx).ok and seen["deps"] is settings
    roots = G.source_roots(tmp_path, settings)
    assert roots[0] == settings.claude_projects and roots[-1] == tmp_path


def test_without_settings_the_stage_fails_closed_and_writes_no_verdict(tmp_path, cfg):
    ctx = prepare(tmp_path, cfg)
    (tmp_path / "verdict.json").write_text("{}")  # an older verdict must not survive either
    res = G.run_stage(Ctx(ctx.day, ctx.work, ctx.cfg, ctx.dry), deps=OK)
    assert res == StageResult(ok=False, reason="settings_missing") and not (tmp_path / "verdict.json").exists()


def test_an_empty_public_host_list_allows_no_host_at_all():
    from types import SimpleNamespace
    none = SimpleNamespace(checks_dir=None, redact_patterns=(), harness_prefixes=(), public_hosts=())
    assert G.default_deps(none).secrets_scan("Watch https://youtube.com/x") != []  # not the default list
    assert G.default_deps(SETTINGS).secrets_scan("Watch https://youtube.com/x") == []  # positive control: the default


def test_the_built_in_scanners_pass_a_clean_script_end_to_end(tmp_path, cfg):
    """No fakes: the stage's own default deps (the built-in checks) accept the clean fixture script."""
    assert G.run_stage(prepare(tmp_path, cfg)) == StageResult(ok=True, reason="")


# ---- the verdict is bound to its ledger ----

def accepts_bound(tmp_path, ledger_path=None):
    return G.accepts(tmp_path / "verdict.json", tmp_path / "script.json", G.REQUIRED,
                     ledger_path=tmp_path / "ledger.json" if ledger_path is None else ledger_path)


def test_a_verdict_bound_to_its_own_ledger_is_accepted(tmp_path, cfg):  # positive control
    passed_stage(tmp_path, cfg)
    assert accepts_bound(tmp_path) == (True, "ok")


def test_a_ledger_edited_after_the_verdict_is_refused(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    (tmp_path / "ledger.json").write_text((tmp_path / "ledger.json").read_text() + " ")
    assert accepts_bound(tmp_path) == (False, "ledger_mismatch")
    assert accepts(tmp_path) == (True, "ok")  # without ledger_path the ledger is not read (render verdicts)


def test_another_ledger_is_refused(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    other = tmp_path / "other-ledger.json"
    other.write_text("{}")
    assert accepts_bound(tmp_path, other) == (False, "ledger_mismatch")


@pytest.mark.parametrize("value", [None, "", 7, "0" * 64], ids=["absent", "empty", "not_text", "wrong"])
def test_a_verdict_without_the_right_ledger_hash_is_refused_when_bound(tmp_path, cfg, value):
    passed_stage(tmp_path, cfg)
    rewrite_verdict(tmp_path, lambda v: v.pop("ledger_sha256") if value is None else v.update(ledger_sha256=value))
    assert accepts_bound(tmp_path) == (False, "ledger_mismatch")


def test_a_missing_ledger_is_refused_when_bound(tmp_path, cfg):
    passed_stage(tmp_path, cfg)
    (tmp_path / "ledger.json").unlink()
    assert accepts_bound(tmp_path) == (False, "ledger_unreadable:FileNotFoundError")


def test_the_stage_accepts_through_the_ledger_it_read(tmp_path, cfg, monkeypatch):
    seen = {}
    real = G.accepts

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)
    monkeypatch.setattr(G, "accepts", spy)
    assert G.run_stage(prepare(tmp_path, cfg), deps=OK).ok
    assert seen == {"ledger_path": tmp_path / "ledger.json"}


@pytest.mark.parametrize("dry", [False, True])
def test_the_verdict_records_dry_and_when(tmp_path, cfg, dry):
    ctx = prepare(tmp_path, cfg)
    res = G.run_stage(Ctx(ctx.day, ctx.work, ctx.cfg, dry, SETTINGS), deps=OK)
    v = read_verdict(tmp_path)
    assert res.ok and v["dry"] is dry
    at = dt.datetime.fromisoformat(v["at"].replace("Z", "+00:00"))
    assert v["at"].endswith("Z") and at.utcoffset() == dt.timedelta(0)
    assert abs((dt.datetime.now(dt.timezone.utc) - at).total_seconds()) < 60
