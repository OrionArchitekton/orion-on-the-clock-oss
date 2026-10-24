"""The collect stage: every reader for the local day, gathered into RawSources, then the ledger and the PR record file
written to the work directory. build_ledger (daily.ledger) stays pure; the reading and writing live here.

Which readers run is settings.toml `[sources]`: the built-in Claude Code and Codex transcript readers, merged PRs from
GitHub, the operator's counts-only adapters, and an optional tracker adapter for closed and open tasks (arcs).
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Callable

from daily import files, gate, textcheck
from daily.ledger import RawSources, build_ledger, exclude_fragments
from daily.sources import adapters as adapter_sources
from daily.sources import claude, codex, github
from daily.sources.tracker import load_tracker
from daily.stage import StageResult

DIAGNOSTICS = {  # the reader counters copied into ledger.diagnostics, per harness that read its source
    "claude_code": ("skipped", "vanished", "no_lane", "unresolved", "git_timeouts"),
    "codex": ("skipped", "vanished", "zst_unread", "missing_child", "unindexed", "vanished_segment"),
}
_CODE = re.compile(r"[a-z][a-z0-9_]*(?::[A-Za-z0-9_]+)?")  # a reader's failure code: empty_root, db_unreadable:X


def _reason(e: Exception) -> str:
    """The exception's message when it is a short code, else its class name: a free-text message can spell a path."""
    msg = str(e)
    return msg if len(msg) <= 64 and _CODE.fullmatch(msg) else type(e).__name__


def _covered(read: Callable) -> tuple:
    """(day, "ok"), or (None, "unavailable:<reason>") for a reader that raised: its harness drops out for the day."""
    try:
        return read(), "ok"
    except Exception as e:  # a coverage gap, not a privacy decision: build_ledger still checks everything kept
        return None, f"unavailable:{_reason(e)}"


def gather(day: dt.date, cfg, settings, *, runner=github.run_cmd, claude_root=None, codex_db=None,
           codex_sessions=None, resolver=claude.git_repo_for, tracker=None, adapters=None) -> RawSources:
    """Every reader the settings switch on, for the day. GitHub and the tracker are the main story: their errors
    (GitHubReadError, SearchTruncated, TrackerReadError) propagate, and they are read first so a failed day reads
    nothing else. Each harness reader runs through _covered. A tracker whose record stops before the day leaves the
    arcs unknown (`arcs_read` False) rather than empty. Roots default to the settings' paths; `tracker` (an object with
    covers(day), closed(day) and active(day)) and `adapters` (harness id -> scan function) stand in for the files the
    settings name, for tests."""

    def public(pr) -> bool:
        lane = cfg.resolve_lane(pr.repo)
        return lane is not None and lane.disclosure == "public"

    diagnostics: dict = {}
    prs, prs_read = (), False
    if settings.github:
        merged = github.merged_prs(day, runner, owner=settings.github_owner)
        attributed = github.attribute(merged, runner, want=public, prefixes=settings.harness_prefixes)
        diagnostics["github"] = {"lookups": attributed.lookups, "failed_lookups": attributed.failed_lookups,
                                 "bad_time": getattr(merged, "bad_time", 0)}  # before tuple()
        prs, prs_read = tuple(attributed), True
    if tracker is None and settings.tracker is not None:
        tracker = load_tracker(settings.tracker, settings.harness_display())
    arcs, active, arcs_read = (), (), False
    if tracker is not None and not tracker.covers(day):
        diagnostics["tracker"] = {"state": "stale"}  # its record stops before the day: the arcs stay unknown
    elif tracker is not None:
        closed, moving = tracker.closed(day), tracker.active(day)
        diagnostics["tracker"] = {"state": "ok", "skipped": closed.skipped, "active_skipped": moving.skipped}
        arcs, active, arcs_read = tuple(closed), tuple(moving), True
    frags = exclude_fragments(cfg)
    readers: dict = {}
    if settings.claude_code:
        root = claude_root or settings.claude_projects
        readers["claude_code"] = lambda: claude.scan_claude(day, root, resolver, exclude_fragments=frags)
    if settings.codex:
        db = codex_db or settings.codex_db
        readers["codex"] = lambda: codex.scan_codex(day, db, resolver, codex_sessions, exclude_fragments=frags)
    scans = adapters or {}
    for a in settings.adapters:
        def read(a=a):
            scan = scans[a.harness] if a.harness in scans else adapter_sources.load_scan(a.module)
            return adapter_sources.run_adapter(scan, day)
        readers[a.harness] = read
    days: dict = {}
    coverage: dict = {}
    for harness, read in readers.items():
        days[harness], coverage[harness] = _covered(read)
        if days[harness] is not None and harness in DIAGNOSTICS:
            diagnostics[harness] = {k: getattr(days[harness], k, 0) for k in DIAGNOSTICS[harness]}
    others = {h: d for h, d in days.items() if h not in ("claude_code", "codex")}
    return RawSources(prs=prs, arcs=arcs, claude=days.get("claude_code"), codex=days.get("codex"), active=active,
                      others=others, coverage=coverage, diagnostics=diagnostics, prs_read=prs_read,
                      arcs_read=arcs_read)


def text_ok(checks: textcheck.TextChecks) -> Callable[[str], bool]:
    """The prefilter build_ledger applies to every text bound for the writer: the gate's redactor and slop checks pass
    it and the gate's no_code check finds no code shape in it, so the writer never picks a quote the gate would block.
    What it rejects is counted in excluded.prefiltered."""
    return lambda text: checks.ok(text) and not gate.code_shapes(text)


# What a new ledger makes stale, verdict first: a re-collect must never leave an accepted verdict (or the script and
# writer files it was made from) beside a different ledger.
STALE_AFTER_COLLECT = ("verdict.json", "script.json", "writer-run.json", "writer-prompt.txt", "ledger.json")


def run_stage(ctx) -> StageResult:
    """The collect stage. Without the gate's text checks it fails closed (`textcheck_unavailable`) before reading
    anything. It removes the old `<work>/ledger.json` and everything downstream of it (STALE_AFTER_COLLECT, then the media: a passing render is archived under
    previous/, anything else removed), writes
    `<work>/sources/github.jsonl` (the PR records quotes point at, one compact JSON object per line), then the new
    ledger, dry or not: a ledger on disk always matches its record file. A coverage gap is no failure: the reason
    names the harnesses. Without settings (ctx.settings None) it fails closed (`settings_missing`) before reading.
    LedgerError, GitHubReadError, SearchTruncated, TrackerReadError and ConfigError propagate, for run.py to record."""
    if ctx.settings is None:
        return StageResult(ok=False, reason="settings_missing")
    try:
        checks = textcheck.load(ctx.settings)
    except textcheck.TextcheckUnavailable:
        return StageResult(ok=False, reason="textcheck_unavailable")
    raw = gather(ctx.day, ctx.cfg, ctx.settings)
    work = Path(ctx.work).absolute()
    source = work / "sources" / "github.jsonl"
    led, records = build_ledger(ctx.day, raw, ctx.cfg, files.now_iso(), str(source), text_ok=text_ok(checks))
    for name in STALE_AFTER_COLLECT:
        (work / name).unlink(missing_ok=True)
    from daily import render  # lazy: render pulls in the whole media chain, which collect never needs
    render.clear_downstream(work, dry=ctx.dry)  # the old ledger's media leave the top level (a dry run: only the dry media)
    # ASCII escapes: a lone surrogate in a body survives the round trip, and the gate re-reads the same text
    files.write_text(source, "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))
    files.write_json(work / "ledger.json", led)
    gaps = [h for h, state in led["coverage"].items() if state != "ok"]
    return StageResult(ok=True, reason=f"coverage gaps: {', '.join(gaps)}" if gaps else "")
