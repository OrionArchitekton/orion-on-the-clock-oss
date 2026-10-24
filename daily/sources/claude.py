"""Claude Code sessions on the local day: counts, session notes, and one-sentence quote candidates from assistant
text.

Sessions are ~/.claude/projects/<dir>/<uuid>.jsonl and subagents <dir>/<uuid>/subagents/agent-*.jsonl (the root is
settings.toml `sources.claude_projects`). A transcript-summarizer plugin's own runs (top-level agent-*.jsonl, or any
file holding a version 2.0.77 record with no entrypoint) are excluded. A file last written before the day starts cannot hold the day's records, so it is not opened.

Transcripts are untrusted data and are being written while we read them. A projects root or file that cannot be read
raises ClaudeReadError: an unreadable source must never look like a day on which nothing happened. A line that is not
a JSON object (a live file's truncated last line, say) is counted in `skipped`, never fatal. A quote's `line` is the
1-based position from `enumerate(open(path, encoding="utf-8", errors="replace"), 1)`, the way the text gate re-reads it.

Lanes are not decided here. Every quotable sentence of a main session is kept and tagged
with `own_repo`: the repo name of the directory it was said in, None for a directory that exists with no remote, or
UNRESOLVED. Each main session active on the day also gets a SessionNote. When the caller passes exclude_fragments (the
hard_exclude entries and their repo names, lowercased), a session is marked `touches_excluded` on its note and on every
candidate it gave when any record's working directory, any tool call's input or any attached file's path, on ANY day,
in its file or in one of its subagent files (workflow agents included) contains a fragment, or a tool's output holds a
path into one (the fragment followed by a path separator), ignoring case: what a session read earlier is still in its
context today. Only the day's prose is quoted. Most sessions run from the home folder, so a working directory alone
would never show a private folder the session read.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple

from daily.day import day_window, in_day, to_utc
from daily.paths import DEFAULT_CLAUDE_PROJECTS
# Re-exported for the modules and tests that import them from here: HarnessDay, QuoteCandidate, SessionNote, UNRESOLVED,
# GitUnresolved, GitTimeout, git_repo_for, sentences, quote_ok.
from daily.sources.transcripts import (UNRESOLVED, GitTimeout, GitUnresolved, HarnessDay, Lanes,  # noqa: F401
                                       QuoteCandidate, SAFE_ID, SessionNote, TranscriptReadError, contains_fragment,
                                       digest_fragments, fragments, git_repo_for, iso_utc, note_ref, note_title,
                                       quote_ok, repo_names, sentences, sorted_entries, touches, touches_path)

SUMMARIZER_VERSION = "2.0.77"
_TITLE_FIELDS = {"ai-title": "aiTitle", "custom-title": "customTitle"}  # record type -> field; neither has a timestamp
_ATTACHMENT_PATHS = ("filename", "path", "displayPath")  # where an attachment names the file it brought in
ClaudeReadError = TranscriptReadError  # the projects root, a transcript or git itself cannot be read


@dataclass(frozen=True)
class ClaudeDay(HarnessDay):
    """What the Claude reader also knows about its own read, for the collect stage to surface.

    skipped: transcript lines that are not JSON objects. vanished: files that disappeared between listing and
    reading. no_lane: candidates whose own_repo is None or UNRESOLVED (kept, and counted for visibility).
    unresolved: directories git could not answer for (a directory that no longer exists, such as a removed worktree,
    a timeout, failure to run, an exit code other than 0 or 1); each is UNRESOLVED in session_repos and own_repo.
    git_timeouts: the timed-out ones among them. notes: one SessionNote per active main session, in transcript
    order. fragments_checked: how many distinct exclude fragments were applied (0 means touches_excluded was never
    checked). fragments_digest: transcripts.digest_fragments of them ("" for none)."""

    skipped: int = 0
    vanished: int = 0
    no_lane: int = 0
    git_timeouts: int = 0
    unresolved: int = 0
    notes: tuple = ()
    fragments_checked: int = 0
    fragments_digest: str = ""


def is_summarizer(r: dict) -> bool:
    return r.get("version") == SUMMARIZER_VERSION and not r.get("entrypoint")


def _attachment_paths(att: dict) -> list:
    """Every path an attachment names: its own file, the instruction files it loaded, the images it inlined."""
    paths = [att[k] for k in _ATTACHMENT_PATHS if isinstance(att.get(k), str)]
    if isinstance(att.get("files"), list):
        paths += [f["path"] for f in att["files"] if isinstance(f, dict) and isinstance(f.get("path"), str)]
    if isinstance(att.get("paths"), list):
        paths += [x for x in att["paths"] if isinstance(x, str)]
    return paths


def _record_touches(r: dict, frags: tuple) -> bool:
    """A record touches a fragment through its working directory, a tool call's input (any *tool_use block, sidechain
    or not), a path into it in a tool's output (a tool_result block or the structured toolUseResult), or a path an
    attachment names (an @-mentioned file, a loaded instruction file, an inlined image: none needs a tool call). What
    a file or instruction SAYS is not a touch: a rules file that names a private folder would taint every session."""
    if isinstance(r.get("cwd"), str) and contains_fragment(r["cwd"], frags):
        return True
    kind = r.get("type")
    msg = r.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    blocks = [b for b in content if isinstance(b, dict) and isinstance(b.get("type"), str)] \
        if isinstance(content, list) else []
    if kind == "assistant":
        return any(b["type"].endswith("tool_use") and touches(b, frags) for b in blocks)
    if kind == "user":
        return (any(b["type"] == "tool_result" and touches_path(b, frags) for b in blocks)
                or ("toolUseResult" in r and touches_path(r["toolUseResult"], frags)))
    if kind == "attachment" and isinstance(r.get("attachment"), dict):
        return touches(_attachment_paths(r["attachment"]), frags)
    return False


class _FileScan(NamedTuple):
    active: bool  # a user or assistant record on the day
    found: list  # (cwd, sentence, ts, line, record) for every quotable sentence (session files only)
    skipped: int  # lines that are not JSON objects
    cwds: set  # every working directory a record of the day used (session files only)
    title: str  # the latest sanitized non-empty ai-title or custom-title, whatever its day (session files only)
    session_id: str  # the first sessionId a record names, "" if none
    touched: bool  # a record of ANY day touched an excluded fragment (see _record_touches)
    last: dt.datetime | None  # the latest in-day record time


def _scan_file(path: str, day: dt.date, want_quotes: bool, frags: tuple = ()) -> _FileScan:
    active, found, skipped, cwds, title, sid, touched, last = False, [], 0, set(), "", "", False, None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except (ValueError, RecursionError):
                r = None
            if not isinstance(r, dict):
                skipped += 1
                continue
            if is_summarizer(r):
                return _FileScan(False, [], 0, set(), "", "", False, None)
            if not sid and isinstance(r.get("sessionId"), str):
                sid = r["sessionId"]
            if frags and not touched:  # any day: what the session read earlier is still in its context
                touched = _record_touches(r, frags)
            if want_quotes and r.get("type") in _TITLE_FIELDS:
                title = note_title(r.get(_TITLE_FIELDS[r["type"]])) or title
                continue
            ts = r.get("timestamp")
            if not in_day(ts, day):
                continue
            t = to_utc(ts)
            last = t if last is None or t > last else last
            if want_quotes and isinstance(r.get("cwd"), str):
                cwds.add(r["cwd"])
            if r.get("type") not in ("user", "assistant"):
                continue
            active = True
            if not want_quotes or r["type"] != "assistant" or r.get("isSidechain") or not isinstance(ts, str):
                continue
            msg = r.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            cwd = r.get("cwd") if isinstance(r.get("cwd"), str) else ""
            uuid = r.get("uuid")
            rid = uuid if isinstance(uuid, str) and SAFE_ID.match(uuid) else ""
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    for s in sentences(block["text"]):
                        found.append((cwd, s, ts, n, rid))
    return _FileScan(active, found, skipped, cwds, title, sid, touched, last)


def _listdir(path: str, what: str) -> list:
    """Sorted directory entries; a failure other than a missing path is a read failure named after what was listed."""
    return sorted_entries(path, lambda e: ClaudeReadError(f"{what}_unreadable:{type(e).__name__}"))


def _agent_files(path: str, depth: int = 4) -> list:
    """Every agent-*.jsonl under a session's subagents folder, at any depth (workflow agents sit in
    subagents/workflows/wf_*/), sorted within each folder. Other files (a workflow's journal.jsonl) are not
    transcripts."""
    out: list = []
    for e in _listdir(path, "subagents"):
        if e.name.startswith("agent-") and e.name.endswith(".jsonl") and e.is_file():
            out.append(e.path)
        elif depth and e.is_dir():
            try:
                out += _agent_files(e.path, depth - 1)
            except (FileNotFoundError, NotADirectoryError):
                continue  # removed while we were listing
    return out


def _transcripts(root: str) -> list:
    """(path, owner) for every transcript under the projects root: sessions first within each project (owner None),
    then the subagent files of its session directories, workflow agents included (owner: the path of the session file
    they belong to, which may not exist). Top-level agent-*.jsonl are the summarizer's own runs and are left out."""
    try:
        projects = [e for e in _listdir(root, "root") if e.is_dir()]
    except (FileNotFoundError, NotADirectoryError) as e:
        raise ClaudeReadError("root_missing") from e
    if not projects:
        raise ClaudeReadError("empty_root")  # a projects root with no project in it is not a quiet day
    jobs: list = []
    for proj in projects:
        try:
            entries = _listdir(proj.path, "project")
        except (FileNotFoundError, NotADirectoryError):
            continue  # removed while we were listing
        subs: list = []
        for e in entries:
            if e.name.endswith(".jsonl") and not e.name.startswith("agent-") and e.is_file():
                jobs.append((e.path, None))
            elif e.is_dir():
                try:
                    agents = _agent_files(os.path.join(e.path, "subagents"))
                except (FileNotFoundError, NotADirectoryError):
                    continue  # most session directories have no subagents
                owner = os.path.join(proj.path, e.name + ".jsonl")
                subs += [(a, owner) for a in agents]
        jobs += subs
    return jobs


def scan_claude(day: dt.date, root: Path = DEFAULT_CLAUDE_PROJECTS, resolver: Callable = git_repo_for, *,
                exclude_fragments: tuple = ()) -> ClaudeDay:
    frags = fragments(exclude_fragments)
    root = str(Path(root).expanduser().absolute())
    floor = day_window(day)[0].timestamp()
    sessions = subagents = skipped = vanished = no_lane = 0
    quotes: list = []
    notes: list = []
    lanes = Lanes(resolver, ClaudeReadError)  # each directory is asked once per scan
    lookup, unresolved = lanes.lookup, lanes.unresolved

    active_sessions: dict = {}  # session path -> _FileScan, in transcript order
    touched_by_subagent: set = set()  # session paths one of whose subagent files touched a fragment on any day

    def taint_only(owner) -> bool:
        """An old subagent file is read only to taint an active session that is not tainted yet."""
        return bool(frags) and owner in active_sessions and not (active_sessions[owner].touched
                                                                 or owner in touched_by_subagent)

    for path, owner in _transcripts(root):  # a project's sessions come before its subagent files
        try:
            recent = os.stat(path).st_mtime >= floor
            if not recent and not taint_only(owner):
                continue
            got = _scan_file(path, day, owner is None, frags)
        except FileNotFoundError:
            vanished += 1
            continue
        except OSError as e:
            raise ClaudeReadError(f"transcript_unreadable:{type(e).__name__}") from e
        if not recent:  # read for the taint alone; it holds none of the day's records
            touched_by_subagent.update([owner] if got.touched else [])
            continue
        skipped += got.skipped
        if owner is not None:
            if got.touched:
                touched_by_subagent.add(owner)
            subagents += got.active
            continue
        if got.active:
            sessions += 1
            active_sessions[path] = got
    for path, got in active_sessions.items():
        used = got.cwds | {f[0] for f in got.found}
        session_repos, session_cwds = repo_names(used, lookup, unresolved), tuple(sorted(used))
        touched = got.touched or path in touched_by_subagent or touches(sorted(used), frags)
        session_id = got.session_id or os.path.basename(path).removesuffix(".jsonl")
        notes.append(SessionNote("claude_code", note_ref(session_id), got.title, session_repos, session_cwds,
                                 touched, iso_utc(got.last)))
        for cwd, s, ts, n, rid in got.found:
            own = repo_names((cwd,), lookup, unresolved)[0]
            no_lane += own is None or own == UNRESOLVED
            quotes.append(QuoteCandidate("claude_code", lookup(cwd), cwd, s, ts, path, n, rid, session_repos,
                                         session_cwds, own, touched))
    return ClaudeDay(sessions, subagents, tuple(quotes), skipped, vanished, no_lane, len(lanes.timed_out),
                     len(unresolved), tuple(notes), len(frags), digest_fragments(frags))
