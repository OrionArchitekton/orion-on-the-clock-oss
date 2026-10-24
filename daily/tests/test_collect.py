"""The collect stage: gather every reader the settings switch on (fakes only), coverage for unreadable harness
sources, switched-off sources, the operator's adapters and tracker, the text prefilter, and the two files it writes
(ledger.json, sources/github.jsonl)."""
import dataclasses
import datetime as dt
import inspect
import json
import subprocess
from pathlib import Path

import pytest

from daily import collect, files, render, textcheck
from daily.ledger import LedgerError, writer_view
from daily.settings import Adapter
from daily.sources import adapters as adapter_sources
from daily.sources import claude, codex, github, tracker
from daily.sources.claude import ClaudeDay, ClaudeReadError, QuoteCandidate, SessionNote
from daily.sources.codex import CodexDay
from daily.sources.github import PR, AttributedPRs, GitHubReadError, SearchTruncated
from daily.sources.tracker import ActiveArc, ActiveArcs, ClosedArcs, TrackerReadError
from daily.sources.transcripts import digest_fragments
from daily.stage import Ctx
from daily.textcheck import TextChecks, TextcheckUnavailable

DAY = dt.date(2026, 10, 16)
GOOD = "The new browser pool shipped today and every single test passed."
OTHER = "Another line that names nothing private at all today."
PR_A = "The codex lane merged the export flag and the tests stayed green."
PR_B = "The importer now keeps every row in order on a full rerun."
FLAGGED = "The Zorblat helper restarted twice and then the queue drained today."  # the fake redactor abstains
HARNESSES = ("claude_code", "codex", "alpha_agent")
FRAGS = ("diary-app", "secret-garden")  # exclude_fragments of the conftest config
DIGEST = digest_fragments(FRAGS)
REAL = {"merged_prs": github.merged_prs, "attribute": github.attribute, "scan_claude": claude.scan_claude,
        "scan_codex": codex.scan_codex, "load_tracker": tracker.load_tracker, "load_scan": adapter_sources.load_scan,
        "run_adapter": adapter_sources.run_adapter}


def pr(n, repo="owner/demo-tool", body="Body"):
    return PR(repo, n, "Add the demo", body, f"https://github.com/{repo}/pull/{n}", "2026-10-16T21:00:00Z")


def cq(text, repo="demo-tool", line=3, harness="claude_code", path="/c/s.jsonl"):
    return QuoteCandidate(harness, repo, f"/w/{repo}", text, "2026-10-16T20:00:00Z", path, line, "u1", (repo,),
                          (f"/w/{repo}",), repo, False)


def note(harness, ref):
    return SessionNote(harness, ref, "Shipping the browser pool", ("demo-tool",), ("/w/demo-tool",), False,
                       "2026-10-16T20:00:00.000Z")


class Env:
    def __init__(self, tmp_path, cfg_dir, cfg, settings):
        self.tmp, self.cfg_dir, self.cfg = tmp_path, cfg_dir, cfg
        self.settings = dataclasses.replace(
            settings, claude_projects=tmp_path / "projects", codex_db=tmp_path / "codex.sqlite",
            tracker=tmp_path / "tracker.py", adapters=(Adapter("alpha_agent", "Alpha Agent", tmp_path / "alpha.py"),))
        self.calls: dict = {}
        self.prs = [pr(7), pr(8, repo="owner/backup-box"), pr(9, repo="owner/mystery-repo")]
        self.claude_quotes = (cq(GOOD), cq(OTHER, repo="mystery-repo", line=4))
        self.claude_digest = DIGEST
        self.alpha = {"sessions": 26, "subagents": 5}
        self.tracker_covers = True
        self.raise_for: dict = {}  # reader name -> exception it raises
        self.n = 0

    def ctx(self, work, dry=False, **over):
        return Ctx(DAY, work, self.cfg, dry, dataclasses.replace(self.settings, **over))

    def collect(self, dry=False, **over):
        self.n += 1
        work = self.tmp / f"work{self.n}"
        work.mkdir()
        res = collect.run_stage(self.ctx(work, dry, **over))
        return res, work

    def ledger(self, **kw):
        res, work = self.collect(**kw)
        assert res.ok, res.reason
        return json.loads((work / "ledger.json").read_text())


@pytest.fixture
def env(monkeypatch, tmp_path, cfg_dir, cfg, settings):
    e = Env(tmp_path, cfg_dir, cfg, settings)

    def call(name, value, *args, **kw):
        e.calls[name] = (args, kw)
        if name in e.raise_for:
            raise e.raise_for[name]
        return value() if callable(value) else value

    def fake_attribute(prs, runner, *, want, prefixes):
        out = AttributedPRs()
        for p in prs:
            if want(p):
                out.lookups += 1
                p = PR(p.repo, p.number, p.title, p.body, p.url, p.closed_at, "codex")
            out.append(p)
        out.failed_lookups = 1 if out.lookups else 0
        return call("attribute", out, prs, runner, prefixes=prefixes)

    def closed():
        out = ClosedArcs()
        out.skipped = 2
        return out

    def active():
        out = ActiveArcs([ActiveArc("a1", "Add the export button", "owner/demo-tool", "codex",
                                    "2026-10-16T20:00:00+00:00")])
        out.skipped = 3
        return out

    class FakeTracker:
        def covers(self, day):
            return call("tracker_covers", lambda: e.tracker_covers, day)

        def closed(self, day):
            return call("arcs_closed", closed, day)

        def active(self, day):
            return call("arcs_active", active, day)

    def fake_scan(day, start, end):
        return call("scan_alpha", lambda: dict(e.alpha) if isinstance(e.alpha, dict) else e.alpha, day, start, end)

    monkeypatch.setattr(github, "merged_prs", lambda day, runner, *, owner: call("merged_prs", list(e.prs), day,
                                                                                 runner, owner=owner))
    monkeypatch.setattr(github, "attribute", fake_attribute)
    monkeypatch.setattr(collect, "load_tracker", lambda path, harnesses: call("load_tracker", FakeTracker, path,
                                                                              frozenset(harnesses)))
    monkeypatch.setattr(adapter_sources, "load_scan", lambda path: call("load_scan", lambda: fake_scan, path))
    monkeypatch.setattr(claude, "scan_claude", lambda day, root, resolver, *, exclude_fragments: call(
        "scan_claude", lambda: ClaudeDay(1, 4, e.claude_quotes, skipped=1, vanished=2, no_lane=3, git_timeouts=4,
                                         unresolved=5, notes=(note("claude_code", "aaaa0001"),),
                                         fragments_checked=2, fragments_digest=e.claude_digest),
        day, root, resolver, exclude_fragments=exclude_fragments))
    monkeypatch.setattr(codex, "scan_codex", lambda day, db, resolver, sessions, *, exclude_fragments: call(
        "scan_codex", CodexDay(2, 1, (), skipped=6, vanished=7, zst_unread=8, missing_child=9, unindexed=10,
                               vanished_segment=17,
                               notes=(note("codex", "bbbb0002"),), fragments_checked=2, fragments_digest=DIGEST),
        day, db, resolver, sessions, exclude_fragments=exclude_fragments))
    redactor = lambda t: ("ABSTAIN", [("name", "Zo**at")]) if "Zorblat" in t else ("PUBLISH", [])  # noqa: E731
    monkeypatch.setattr(textcheck, "load", lambda settings=None: call("textcheck", TextChecks(redactor, lambda t: []),
                                                                      settings))
    return e


# ---- the fakes keep the real readers' call shapes ----

def test_the_real_readers_accept_the_calls_gather_makes():
    calls = {"merged_prs": ((DAY, github.run_cmd), {"owner": "o"}),
             "attribute": (([], github.run_cmd), {"want": bool, "prefixes": ()}),
             "scan_claude": ((DAY, Path("p"), claude.git_repo_for), {"exclude_fragments": FRAGS}),
             "scan_codex": ((DAY, Path("d"), claude.git_repo_for, None), {"exclude_fragments": FRAGS}),
             "load_tracker": ((Path("t.py"), {"claude_code": "Claude Code"}), {}),
             "load_scan": ((Path("a.py"),), {}), "run_adapter": ((len, DAY), {})}
    for name, (args, kw) in calls.items():
        inspect.signature(REAL[name]).bind(*args, **kw)


# ---- the stage ----

def test_collect_writes_the_ledger_and_only_attributes_public_prs(env):
    res, work = env.collect()
    assert res.ok and not res.quiet and res.reason == ""
    data = json.loads((work / "ledger.json").read_text())
    assert data["day"] == "2026-10-16"
    assert [i["id"] for i in data["items"]] == ["pr:demo-tool#7", "lane:backup-box"]
    assert [p.number for p in env.calls["attribute"][0][0]] == [7, 8, 9]  # all of them pass through attribute
    assert data["diagnostics"]["github"] == {"lookups": 1, "failed_lookups": 1, "bad_time": 0}  # only #7 was looked up
    assert [i["harness"] for i in data["items"] if i["kind"] == "pr"] == ["codex"]
    assert [t["title"] for t in data["teasers"]] == ["Add the export button"]
    assert env.calls["merged_prs"][1] == {"owner": "demo-owner"}
    assert env.calls["attribute"][1] == {"prefixes": env.settings.harness_prefixes}


def test_every_harness_reaches_the_scoreboard(env):
    data = env.ledger()
    sb = data["scoreboard"]
    assert sb["sessions"] == {"claude_code": 1, "codex": 2, "alpha_agent": 26}
    assert sb["subagents"] == {"claude_code": 4, "codex": 1, "alpha_agent": 5}
    assert (sb["prs_merged"], sb["arcs_closed"]) == (3, 0)
    assert list(data["coverage"].items()) == [(h, "ok") for h in HARNESSES]


def test_diagnostics_hold_every_reader_counter(env):
    assert env.ledger()["diagnostics"] == {
        "github": {"lookups": 1, "failed_lookups": 1, "bad_time": 0},
        "tracker": {"state": "ok", "skipped": 2, "active_skipped": 3},
        "claude_code": {"skipped": 1, "vanished": 2, "no_lane": 3, "unresolved": 5, "git_timeouts": 4},
        "codex": {"skipped": 6, "vanished": 7, "zst_unread": 8, "missing_child": 9, "unindexed": 10,
                  "vanished_segment": 17},
    }


def test_quotes_follow_the_allowlist(env):
    quotes = env.ledger()["quotes"]
    assert [(q["harness"], q["text"]) for q in quotes] == [("claude_code", GOOD)]


def test_the_readers_get_the_settings_roots_and_the_exact_fragments(env):
    env.ledger()
    args, kw = env.calls["scan_claude"]
    assert args[:2] == (DAY, env.tmp / "projects") and args[2] is claude.git_repo_for
    assert kw == {"exclude_fragments": FRAGS}
    args, kw = env.calls["scan_codex"]
    assert args[:2] == (DAY, env.tmp / "codex.sqlite") and args[3] is None and kw == {"exclude_fragments": FRAGS}
    assert env.calls["load_tracker"][0] == (env.tmp / "tracker.py", frozenset(HARNESSES))
    assert env.calls["load_scan"][0] == (env.tmp / "alpha.py",)
    assert env.calls["arcs_closed"][0] == env.calls["arcs_active"][0] == (DAY,)
    args, _ = env.calls["scan_alpha"]
    assert args[0] == DAY and args[2] - args[1] == dt.timedelta(hours=24) and args[1].tzinfo is not None
    assert env.calls["textcheck"][0][0].github_owner == "demo-owner"  # the checks load with the run's settings


def test_a_reader_that_ignored_the_fragments_fails_the_stage(env):
    env.claude_digest = ""  # quotes handed over with no exclude check behind them
    with pytest.raises(LedgerError, match="^exclude_not_applied:claude_code$"):
        env.collect()
    assert not list(env.tmp.glob("work*/ledger.json"))


# ---- coverage: an unreadable harness source drops out, the run continues ----

READER = {"claude_code": "scan_claude", "codex": "scan_codex", "alpha_agent": "scan_alpha"}


@pytest.mark.parametrize("harness", HARNESSES)
def test_each_harness_can_drop_out_on_its_own(env, harness):
    env.raise_for[READER[harness]] = subprocess.TimeoutExpired("ssh", 60)
    res, work = env.collect()
    assert res.ok and res.reason == f"coverage gaps: {harness}"
    data = json.loads((work / "ledger.json").read_text())
    assert data["coverage"][harness] == "unavailable:TimeoutExpired"
    assert all(data["coverage"][h] == "ok" for h in HARNESSES if h != harness)  # positive control
    sb = data["scoreboard"]
    assert harness not in sb["sessions"] and harness not in sb["subagents"]
    assert harness not in data["diagnostics"]
    if harness == "claude_code":
        assert data["quotes"] == [] and all(s["harness"] == "codex" for s in data["sessions"])


def test_two_gaps_are_both_named(env):
    env.raise_for["scan_alpha"] = ConnectionError("down")
    env.raise_for["scan_codex"] = ConnectionError("down")
    res, work = env.collect()
    assert res.reason == "coverage gaps: codex, alpha_agent"


@pytest.mark.parametrize("exc, reason", [
    (ClaudeReadError("empty_root"), "empty_root"),
    (ClaudeReadError("transcript_unreadable:PermissionError"), "transcript_unreadable:PermissionError"),
    (FileNotFoundError("agent_sessions_missing"), "agent_sessions_missing"),
    (FileNotFoundError(2, "No such file or directory", "/home/someone/private-notes"), "FileNotFoundError"),
    (ValueError("bad_agent_output"), "bad_agent_output"),
    (RuntimeError("a message with spaces"), "RuntimeError"),
    (KeyError("diary-app"), "KeyError"),
    (ValueError("x" * 65), "ValueError"),
])
def test_the_reason_is_a_short_code_or_the_class_name(env, exc, reason):
    env.raise_for["scan_alpha"] = exc
    assert env.ledger()["coverage"]["alpha_agent"] == f"unavailable:{reason}"


@pytest.mark.parametrize("output, reason", [
    ({"sessions": 1}, "bad_adapter_output"),
    ({"sessions": 1, "subagents": -1}, "bad_adapter_output"),
    ({"sessions": True, "subagents": 0}, "bad_adapter_output"),
    ({"sessions": 1, "subagents": 0, "quotes": ["a"]}, "bad_adapter_output"),
    ([1, 0], "bad_adapter_output"),
])
def test_an_adapter_that_answers_out_of_shape_is_a_coverage_gap(env, output, reason):
    env.alpha = output
    data = env.ledger()
    assert data["coverage"]["alpha_agent"] == f"unavailable:{reason}"
    assert "alpha_agent" not in data["scoreboard"]["sessions"]


def test_a_missing_adapter_file_is_a_coverage_gap(env, monkeypatch):
    monkeypatch.setattr(adapter_sources, "load_scan", REAL["load_scan"])  # the real loader, on a file that is not there
    data = env.ledger()
    assert data["coverage"]["alpha_agent"] == "unavailable:adapter_unavailable"
    assert data["coverage"]["claude_code"] == "ok"  # positive control


# ---- the main story sources never fail soft ----

@pytest.mark.parametrize("reader, exc", [
    ("merged_prs", GitHubReadError("gh search prs exited 1: rate limit")),
    ("merged_prs", SearchTruncated("cap")),
    ("attribute", GitHubReadError("boom")),
    ("load_tracker", TrackerReadError("adapter_unavailable")),
    ("arcs_closed", TrackerReadError("closed_failed:OSError")),
    ("arcs_active", TrackerReadError("active_failed:OSError")),
])
def test_github_and_tracker_failures_fail_the_stage(env, reader, exc):
    env.raise_for[reader] = exc
    with pytest.raises(type(exc)):
        env.collect()
    assert not list(env.tmp.glob("work*/ledger.json")) and not list(env.tmp.glob("work*/sources"))
    assert not {"scan_claude", "scan_codex", "scan_alpha"} & set(env.calls)  # no harness read was wasted


def test_a_missing_tracker_file_fails_the_stage(env, monkeypatch):
    monkeypatch.setattr(collect, "load_tracker", REAL["load_tracker"])
    with pytest.raises(TrackerReadError, match="^adapter_unavailable$"):
        env.collect()


# ---- what the settings switch off is never read ----

def test_github_off_reads_no_prs_and_the_count_is_unknown(env):
    data = env.ledger(github=False, github_owner=None)
    assert "merged_prs" not in env.calls and "attribute" not in env.calls
    assert data["scoreboard"]["prs_merged"] is None and "github" not in data["diagnostics"]
    assert [i["kind"] for i in data["items"]] == []


def test_a_tracker_whose_record_stops_before_the_day_leaves_the_arcs_unknown(env):
    env.tracker_covers = False
    data = env.ledger()
    assert "arcs_closed" not in env.calls and "arcs_active" not in env.calls  # not even read
    assert data["scoreboard"]["arcs_closed"] is None and data["teasers"] == []
    assert data["diagnostics"]["tracker"] == {"state": "stale"}


def test_no_tracker_reads_no_arcs_and_the_count_is_unknown(env):
    data = env.ledger(tracker=None)
    assert "load_tracker" not in env.calls
    assert data["scoreboard"]["arcs_closed"] is None and data["teasers"] == [] and "tracker" not in data["diagnostics"]


@pytest.mark.parametrize("off", ["claude_code", "codex"])
def test_a_switched_off_reader_is_never_called_and_never_a_gap(env, off):
    res, work = env.collect(**{off: False})
    assert res.ok and res.reason == ""
    data = json.loads((work / "ledger.json").read_text())
    assert READER[off] not in env.calls and off not in data["coverage"] and off not in data["scoreboard"]["sessions"]


def test_no_adapters_reads_no_adapter(env):
    data = env.ledger(adapters=())
    assert "load_scan" not in env.calls and list(data["coverage"]) == ["claude_code", "codex"]


def test_without_settings_the_stage_fails_closed_before_reading(env):
    work = env.tmp / "nosettings"
    work.mkdir()
    res = collect.run_stage(Ctx(DAY, work, env.cfg, False))
    assert not res.ok and res.reason == "settings_missing"
    assert list(work.iterdir()) == [] and env.calls == {}


# ---- the text prefilter ----

def test_the_text_prefilter_drops_what_the_gate_would_block(env):
    env.claude_quotes = (cq(GOOD), cq(FLAGGED, line=5))
    data = env.ledger()
    assert [q["text"] for q in data["quotes"]] == [GOOD]  # positive control: the clean one stays
    assert data["excluded"]["prefiltered"] == 1


CODEY = "The agent ran git push and the whole branch landed cleanly today."  # quote_ok passes it; no_code does not


def test_the_text_prefilter_also_drops_what_no_code_would_block(env):
    env.claude_quotes = (cq(GOOD), cq(CODEY, line=5))
    data = env.ledger()
    assert [q["text"] for q in data["quotes"]] == [GOOD]  # positive control: the clean one stays
    assert data["excluded"]["prefiltered"] == 1


def titled(n, title, body="Body"):
    return PR("owner/demo-tool", n, title, body, f"https://github.com/owner/demo-tool/pull/{n}", "2026-10-16T21:00:00Z")


def test_a_backtick_in_an_item_title_or_body_line_is_removed_and_the_item_kept(env):
    env.prs = [titled(7, "Fix the `foo` flag", "Keeps the `foo` flag working.\nThen run git push again.")]
    data = env.ledger()
    [item] = [i for i in data["items"] if i["kind"] == "pr"]
    assert item["title"] == "Fix the foo flag"
    assert item["body"] == "Keeps the foo flag working."  # a line with another code shape is still cut
    assert data["excluded"]["prefiltered"] == 1


def test_an_item_title_with_another_code_shape_is_still_dropped(env):
    env.prs = [titled(7, "Fix the flag, see https://example.invalid/x"), titled(8, "Run `git push` in CI"),
               titled(9, "Fix the `foo` flag")]  # positive control: only the backticks, kept
    data = env.ledger()
    assert [i["title"] for i in data["items"] if i["kind"] == "pr"] == ["Fix the foo flag"]
    assert (data["excluded"]["path_or_url"], data["excluded"]["prefiltered"]) == (1, 1)


def test_the_prefilter_reads_the_normal_form_too(env, monkeypatch):
    """A figure dash survives NFKC; only the normal form shows it as the long dash the slop gate blocks."""
    slop = lambda t: [("banned_dash", "U+2014")] if "\u2014" in t else []  # noqa: E731
    monkeypatch.setattr(textcheck, "load", lambda settings=None: TextChecks(lambda t: ("PUBLISH", []), slop))
    env.prs = [titled(7, "Add the demo \u2012 fast"), titled(8, "Add the demo fast")]  # positive control: 8 stays
    data = env.ledger()
    assert [i["title"] for i in data["items"] if i["kind"] == "pr"] == ["Add the demo fast"]
    assert data["excluded"]["prefiltered"] == 1


def test_a_text_check_that_raises_fails_the_stage(env, monkeypatch):
    def broken(t):
        raise RuntimeError("redactor crashed")

    monkeypatch.setattr(textcheck, "load", lambda settings=None: TextChecks(broken, lambda t: []))
    with pytest.raises(RuntimeError, match="redactor crashed"):
        env.collect()
    assert not list(env.tmp.glob("work*/ledger.json"))


def test_without_the_text_checks_the_stage_fails_closed(env, monkeypatch):
    def missing(settings=None):
        raise TextcheckUnavailable("textcheck_unavailable")

    monkeypatch.setattr(textcheck, "load", missing)
    res, work = env.collect()
    assert not res.ok and res.reason == "textcheck_unavailable"
    assert list(work.iterdir()) == [] and not {"merged_prs", "scan_claude"} & set(env.calls)


# ---- sources/github.jsonl ----

def test_pr_quote_lines_point_at_their_record(env):
    env.prs = [pr(7, body=f"{PR_A}\n\nNothing quotable."), pr(8, repo="owner/backup-box", body=PR_A),
               pr(12, body=f"Intro.\n{PR_B}"), pr(3, body="Too short.")]
    res, work = env.collect()
    src = work / "sources" / "github.jsonl"
    lines = src.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["number"] for x in lines] == [7, 12]  # #3 gave no quote, #8 is a headline lane
    assert all(x == json.dumps(json.loads(x), separators=(",", ":")) for x in lines)  # one compact object a line
    quotes = [q for q in json.loads((work / "ledger.json").read_text())["quotes"] if q["kind"] == "pr"]
    assert sorted(q["text"] for q in quotes) == sorted([PR_A, PR_B])
    for q in quotes:
        assert q["source"]["path"] == str(src.absolute())
        rec = json.loads(lines[q["source"]["line"] - 1])
        assert q["source"]["record"] == f"pr:demo-tool#{rec['number']}" and q["text"] in rec["body"]
        assert set(rec) == {"repo", "number", "harness", "closed_at", "body"}


def test_a_day_without_pr_quotes_writes_an_empty_record_file(env):
    res, work = env.collect()
    assert (work / "sources" / "github.jsonl").read_text() == ""


def test_a_crash_after_the_record_file_leaves_no_ledger(env, monkeypatch):
    """A ledger on disk always matches its record file: the old one goes before the new record file is written."""
    work = env.tmp / "crash"
    work.mkdir()
    (work / "ledger.json").write_text('{"stale": true}')

    def crash(path, obj):
        raise OSError("disk full")

    monkeypatch.setattr(files, "write_json", crash)
    with pytest.raises(OSError, match="disk full"):
        collect.run_stage(env.ctx(work))
    assert (work / "sources" / "github.jsonl").is_file() and not (work / "ledger.json").exists()


def test_a_rerun_replaces_the_old_ledger(env):  # positive control for the crash test
    res, work = env.collect()
    (work / "ledger.json").write_text('{"stale": true}')
    assert collect.run_stage(env.ctx(work)).ok
    assert json.loads((work / "ledger.json").read_text())["day"] == "2026-10-16"


DOWNSTREAM = ("script.json", "verdict.json", "writer-run.json", "writer-prompt.txt")


def _stale_run(work):
    """What a finished earlier run leaves beside its ledger: the writer's and the gate's files, and a bystander."""
    (work / "ledger.json").write_text('{"stale": true}')
    for name in DOWNSTREAM:
        (work / name).write_text("stale")
    (work / "notes.txt").write_text("keep")


def test_a_recollect_removes_the_script_verdict_and_writer_files(env):
    res, work = env.collect()
    _stale_run(work)
    assert collect.run_stage(env.ctx(work)).ok
    assert not [n for n in DOWNSTREAM if (work / n).exists()]  # no accepted verdict can sit beside a new ledger
    assert json.loads((work / "ledger.json").read_text())["day"] == "2026-10-16"
    assert (work / "notes.txt").read_text() == "keep"  # only the downstream artifacts go


def test_a_collect_that_fails_before_writing_keeps_the_old_set_together(env):
    res, work = env.collect()
    _stale_run(work)
    env.raise_for["merged_prs"] = GitHubReadError("boom")
    with pytest.raises(GitHubReadError):
        collect.run_stage(env.ctx(work))
    assert all((work / n).exists() for n in DOWNSTREAM) and (work / "ledger.json").read_text() == '{"stale": true}'


def test_a_crash_while_writing_the_new_ledger_leaves_no_downstream_file(env, monkeypatch):
    work = env.tmp / "crash2"
    work.mkdir()
    _stale_run(work)

    def crash(path, obj):
        raise OSError("disk full")

    monkeypatch.setattr(files, "write_json", crash)
    with pytest.raises(OSError, match="disk full"):
        collect.run_stage(env.ctx(work))
    assert not [n for n in DOWNSTREAM + ("ledger.json",) if (work / n).exists()]


def test_dry_mode_still_writes_into_the_work_dir(env):
    res, work = env.collect(dry=True)
    assert res.ok and (work / "ledger.json").is_file() and (work / "sources" / "github.jsonl").is_file()


# ---- what the writer is sent ----

def test_the_writer_view_of_a_collected_ledger_has_no_diagnostics_or_reasons(env):
    env.raise_for["scan_alpha"] = subprocess.TimeoutExpired("ssh", 60)
    data = env.ledger()
    assert data["diagnostics"] and data["coverage"]["alpha_agent"] == "unavailable:TimeoutExpired"  # positive control
    view = writer_view(data)
    assert "diagnostics" not in view
    assert view["coverage"]["alpha_agent"] == "unavailable"
    dumped = json.dumps(view)
    assert "TimeoutExpired" not in dumped and "failed_lookups" not in dumped


def test_search_rows_dropped_for_a_bad_merge_time_reach_the_diagnostics(env, monkeypatch):
    def merged(day, runner, *, owner):
        out = github.MergedPRs(env.prs)
        out.bad_time = 2
        return out
    monkeypatch.setattr(github, "merged_prs", merged)
    assert env.ledger()["diagnostics"]["github"] == {"lookups": 1, "failed_lookups": 1, "bad_time": 2}


# ---- a new ledger clears what the media stages made from the old one ----

def test_a_collect_asks_the_render_stage_to_clear_downstream(env, monkeypatch):
    seen = []
    monkeypatch.setattr(render, "clear_downstream", lambda work, dry=False: seen.append((work, dry)))
    res, work = env.collect()
    res2, work2 = env.collect(dry=True)
    assert res.ok and res2.ok and seen == [(work.absolute(), False), (work2.absolute(), True)]
