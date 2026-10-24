"""Codex threads on the local day: counts, session notes, and one-sentence quote candidates from assistant prose.

Threads come from ~/.codex/state_5.sqlite (opened read-only; settings.toml `sources.codex_db`); their rollout files are
~/.codex/sessions/Y/M/D/*.jsonl (or wherever a thread row says). Rollout line timestamps are UTC and decide activity.
The rollout FOLDER dates are the machine's local time and decide nothing: the folders of the day and of its two neighbours only add candidate files, a
thread the db's own times missed, an older SEGMENT of a thread, or an "unindexed" rollout that no thread row names
(counted, never read as a session). A thread's log rotates into a new file; the row names only the newest, and the
older segments (same id in the file name) hold other work, so each segment found in the folders is read with the row's
file and the thread is still counted once.

A thread is a subagent when its `source` JSON says so (the `thread_source` column is empty for older rows), and is
counted without quotes or notes; a subagent that touches an excluded fragment taints the thread that spawned it (the
`parent_thread_id` its source JSON names), up the chain, whatever day it ran. A main thread is ACTIVE on the day when
it has a user or assistant message, a tool call or a reasoning item with a timestamp in the day. Settings touches and
developer briefings are not work. A thread whose rollout is gone counts by the db's own created/updated times (and
`vanished` says so).

Lanes are not decided here. Every quotable sentence of a main thread is kept and tagged with `own_repo`, the repo name
of the directory it was said in: a name, None (the directory exists and has no remote) or UNRESOLVED (a removed
directory, a timeout, an unexpected git result; never read as "no remote"). A thread row's recorded git_origin_url
speaks for the thread's own directory. Whether a lane may be quoted is the ledger's call.

Codex threads almost all run from the home folder, so a working directory alone never shows a private folder the thread
read. When the caller passes exclude_fragments (the hard_exclude entries and their repo names, lowercased), a thread is
marked `touches_excluded` on its note and on every candidate it gave when, on ANY day, in any rollout segment of the
thread (in the sessions tree or archived_sessions), of a thread it spawned, or of a thread it was forked from
(forked_from_id, history_base), the directory in force, any directory it used, or any tool call (function_call
arguments, custom_tool_call input, any other *_call item, also inside a compacted record's replacement_history, and the
command and directory of a shell command logged only as an item_completed CommandExecution) contains a fragment, or a
tool's output (a call's output, a CommandExecution's aggregated_output) holds a path into one (the fragment followed by
a path separator), ignoring case: what a thread read earlier is still in its context today. Only the day's prose is
quoted. Older segments compressed to .jsonl.zst are streamed through the zstd CLI for the taint alone (never quoted),
within one time budget per scan; one that cannot be read in full taints its thread and is counted in `zst_unread`. A
spawned or base thread whose rollout cannot be found taints too, and is counted in `missing_child`.

Transcripts are untrusted data and are being written while we read them. A db, sessions folder or rollout that cannot be
read raises CodexReadError: an unreadable source must never look like a day on which nothing happened. A line that is
not a JSON object is counted in `skipped`, never fatal. A quote's `line` is the 1-based position from
`enumerate(open(path, encoding="utf-8", errors="replace"), 1)`, the way the text gate re-reads it.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple

from daily.config import repo_name
from daily.day import day_window, in_day, to_utc
from daily.paths import DEFAULT_CODEX_DB
from daily.sources.transcripts import (HarnessDay, Lanes, QuoteCandidate, SAFE_ID, SessionNote, USERINFO,
                                       digest_fragments, fragments, git_repo_for, iso_utc, note_ref, note_title,
                                       repo_names, sentences, sorted_entries, touches, touches_path)

_CONTEXT = ("session_meta", "turn_context")  # the records that carry the working directory
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_COLUMNS = ("id, rollout_path, source, thread_source, git_origin_url, cwd, created_at, updated_at, "
            "substr(title, 1, 1000), substr(first_user_message, 1, 1000)")
_CHUNK = 500
_SEGMENT_SUFFIXES = (".jsonl", ".jsonl.zst")  # a rotated older segment may have been compressed
ZSTD_BUDGET = 300  # seconds for all compressed segments of one scan; a segment left when it runs out taints


class CodexReadError(Exception):
    """The thread db, the sessions folder or a rollout cannot be read; never read as a quiet day. The message names the
    failure kind only (never a path, which can spell a working directory)."""


@dataclass(frozen=True)
class CodexDay(HarnessDay):
    """What the Codex reader also knows about its own read, for the collect stage to surface.

    skipped: rollout lines that are not JSON objects. vanished: thread rows whose rollout cannot be located or opened
    (gone, compressed away, a relative path); each still counts by the row's own times. unresolved: directories git
    could not answer for; git_timeouts: the timed-out ones among them. unindexed: rollouts in the day's folders that
    no thread row names (by any id in the file name) and that hold work on the day; they are not counted as sessions.
    notes: one SessionNote per active main thread, ordered by thread id. fragments_checked: how many distinct exclude
    fragments were applied (0 means touches_excluded was never checked). zst_unread: compressed segments that could not
    be read in full for the taint (or were left when the time budget ran out); each one tainted its thread.
    missing_child: spawned or fork-base threads whose rollout could not be found (or whose row is gone); each one
    tainted the session it belongs to. fragments_digest: transcripts.digest_fragments of the applied fragments ("" for
    none). vanished_segment: plain historical segments (never a thread's own rollout) that were indexed but could not
    be read for the taint; like an unreadable compressed segment, each one tainted its thread."""

    skipped: int = 0
    vanished: int = 0
    git_timeouts: int = 0
    unresolved: int = 0
    unindexed: int = 0
    notes: tuple = ()
    fragments_checked: int = 0
    zst_unread: int = 0
    missing_child: int = 0
    fragments_digest: str = ""
    vanished_segment: int = 0


class _Thread(NamedTuple):
    id: str
    rollout: str
    source: str
    thread_source: str
    origin: str
    cwd: str
    created: object
    updated: object
    title: str
    first: str


def _thread(row) -> _Thread:
    text = lambda v: v if isinstance(v, str) else ""  # noqa: E731
    return _Thread(text(row[0]), text(row[1]), text(row[2]), text(row[3]), text(row[4]), text(row[5]), row[6], row[7],
                   text(row[8]), text(row[9]))


def _is_subagent(t: _Thread) -> bool:
    return "subagent" in t.source or t.thread_source == "subagent"


def _parent(t: _Thread) -> str:
    """The thread that spawned a subagent thread, as its source JSON names it; "" when it names none."""
    try:
        node = json.loads(t.source)
    except (ValueError, RecursionError):
        return ""
    for key in ("subagent", "thread_spawn", "parent_thread_id"):
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, str) else ""


def _is_call(p: dict) -> bool:
    """A tool call item (function_call, custom_tool_call, any other *_call), never its *_call_output."""
    return isinstance(p.get("type"), str) and p["type"].endswith("_call")


def _is_output(p: dict) -> bool:
    """A tool's output item (function_call_output, custom_tool_call_output, any other *_call_output)."""
    return isinstance(p.get("type"), str) and p["type"].endswith("_call_output")


def _item_touches(p: dict, frags: tuple) -> bool:
    """A tool call holding a fragment anywhere, or a tool output holding a path into one."""
    return (_is_call(p) and touches(p, frags)) or (_is_output(p) and touches_path(p, frags))


def _event_touches(p: dict, frags: tuple) -> bool:
    """A shell command Codex logged only as an event_msg item_completed CommandExecution (no tool call names it): its
    command, parsed command and directory count like a call's input, its aggregated output like a tool's output."""
    item = p.get("item") if p.get("type") == "item_completed" else None
    if not isinstance(item, dict) or item.get("type") != "CommandExecution":
        return False
    return (touches([item.get("command"), item.get("parsed_cmd"), item.get("cwd")], frags)
            or touches_path(item.get("aggregated_output"), frags))


def _bases(p: dict) -> set:
    """The threads a session_meta says this one was forked from: forked_from_id and history_base.thread_id."""
    out = {p["forked_from_id"]} if isinstance(p.get("forked_from_id"), str) else set()
    base = p.get("history_base")
    if isinstance(base, dict) and isinstance(base.get("thread_id"), str):
        out.add(base["thread_id"])
    return out - {""}


def _is_work(kind, p: dict) -> bool:
    """A conversation turn or a tool call or a reasoning item, not a settings touch or a developer briefing."""
    return kind == "response_item" and (p.get("type") != "message" or p.get("role") in ("user", "assistant"))


def _scan_rollout(path: str, day: dt.date, cwd0: str, want_quotes: bool, frags: tuple = ()) -> tuple:
    """_scan_lines over a plain rollout file, read the way the text gate re-reads it."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return _scan_lines(fh, day, cwd0, want_quotes, frags)


def _scan_lines(lines, day: dt.date, cwd0: str, want_quotes: bool, frags: tuple = ()) -> tuple:
    """(active, found, skipped, cwds, touched, last, bases): active when the file holds work on the day; found holds
    (cwd, sentence, ts, line, record) for every quotable sentence, cwd being the directory in force at that line (the
    thread row's, until a session_meta or turn_context says otherwise, on any day); skipped counts lines that are not
    JSON objects; cwds is every directory a session_meta or turn_context of the day named (kept for main threads only);
    touched: on some line of ANY day the directory in force, a tool call (also inside a compacted history, or a shell
    command logged as an item_completed CommandExecution) or a path in a tool output held a fragment; last: the latest
    in-day line time; bases: the threads a session_meta of any day says this one was forked from."""
    active, found, skipped, cwds, cur, touched, last, bases = False, [], 0, set(), cwd0, False, None, set()
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except (ValueError, RecursionError):
            r = None
        if not isinstance(r, dict):
            skipped += 1
            continue
        kind = r.get("type")
        p = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        named = p["cwd"] if kind in _CONTEXT and isinstance(p.get("cwd"), str) else None
        if named is not None:
            cur = named
        if kind == "session_meta":
            bases |= _bases(p)
        if frags and not touched:  # any day: what the thread read earlier is still in its context
            replaced = p.get("replacement_history") if kind == "compacted" else None
            touched = (touches(cur, frags) or (kind == "response_item" and _item_touches(p, frags))
                       or (kind == "event_msg" and _event_touches(p, frags))
                       or (isinstance(replaced, list)
                           and any(isinstance(i, dict) and _item_touches(i, frags) for i in replaced)))
        ts = r.get("timestamp")
        if not in_day(ts, day):
            continue
        t = to_utc(ts)
        last = t if last is None or t > last else last
        if want_quotes and named is not None:
            cwds.add(named)
        if not _is_work(kind, p):
            continue
        active = True
        if not want_quotes or p.get("type") != "message" or p.get("role") != "assistant" or not isinstance(ts, str):
            continue
        content = p.get("content")
        if not isinstance(content, list):
            continue
        pid = p.get("id")
        rid = pid if isinstance(pid, str) and SAFE_ID.match(pid) else ""
        for block in content:
            if isinstance(block, dict) and block.get("type") == "output_text" and isinstance(block.get("text"), str):
                for s in sentences(block["text"]):
                    found.append((cur, s, ts, n, rid))
    return active, found, skipped, cwds, touched, last, bases


def _read(path: str, day: dt.date, cwd0: str, want_quotes: bool, frags: tuple = ()):
    """_scan_rollout, or None when the rollout cannot be located (not an absolute path, gone, a path component that is
    a file). Any other failure to read it is a read failure."""
    if not os.path.isabs(path) or "\0" in path:
        return None
    try:
        return _scan_rollout(path, day, cwd0, want_quotes, frags)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        raise CodexReadError(f"rollout_unreadable:{type(e).__name__}") from e


def _zst_touched(path: str, day: dt.date, cwd0: str, frags: tuple, timeout: float) -> tuple | None:
    """(touched, bases) for a compressed segment streamed through `zstd -dc`; for the taint only, since the gate
    cannot re-read a quote from it. None when it could not be read in full: no time left, no zstd, a failure to start,
    a timeout, a non-zero exit, bytes that are not UTF-8, or a line that is not a JSON object. None never means
    clean."""
    exe = shutil.which("zstd")
    if exe is None or timeout <= 0:
        return None
    try:
        proc = subprocess.Popen([exe, "-dc", "--", path], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    undecodable = False

    def kill():  # the whole process group, so no child is left holding the pipe open
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def lines():
        nonlocal undecodable
        for raw in proc.stdout:
            try:
                yield raw.decode("utf-8")
            except UnicodeDecodeError:
                undecodable = True
                yield raw.decode("utf-8", errors="replace")

    timer = threading.Timer(timeout, kill)
    timer.start()
    try:
        with proc:
            got = _scan_lines(lines(), day, cwd0, False, frags)
            code = proc.wait()
    finally:
        timer.cancel()
    if code != 0 or undecodable or got[2]:
        return None
    return got[4], got[6]


def _listdir(path: str) -> list:
    """Sorted directory entries; a failure other than a missing path is a read failure."""
    return sorted_entries(path, lambda e: CodexReadError(f"sessions_unreadable:{type(e).__name__}"))


def _folder_rollouts(root: Path, day: dt.date) -> list:
    """(path, ids in the file name) for every rollout file in the folders of the day and its two neighbours."""
    out = []
    for offset in (-1, 0, 1):
        d = day + dt.timedelta(days=offset)
        try:
            entries = _listdir(os.path.join(root, f"{d:%Y}", f"{d:%m}", f"{d:%d}"))
        except (FileNotFoundError, NotADirectoryError):
            continue  # most days have a folder, but a day with no Codex work does not
        out += [(e.path, _UUID.findall(e.name)) for e in entries
                if e.name.startswith("rollout-") and e.name.endswith(".jsonl") and e.is_file()]
    return out


def _rollout_index(root: Path, depth: int = 3) -> dict:
    """Thread id -> every rollout file in the whole sessions tree (Y/M/D folders) whose name carries that id, plain
    or compressed. (The day's folders feed the day's scan with plain files only: a compressed segment is read for the
    taint alone, through this index.)"""
    out: dict = {}
    try:
        entries = _listdir(root)
    except (FileNotFoundError, NotADirectoryError):
        return out
    for e in entries:
        if e.name.startswith("rollout-") and e.name.endswith(_SEGMENT_SUFFIXES) and e.is_file():
            for u in _UUID.findall(e.name):
                out.setdefault(u, []).append(e.path)
        elif depth and e.is_dir():
            for u, paths in _rollout_index(Path(e.path), depth - 1).items():
                out.setdefault(u, []).extend(paths)
    return out


def _spawned(db: Path) -> dict:
    """Thread id -> the threads it spawned on any day, as each spawned thread's source JSON names its parent."""
    out: dict = {}
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as e:
        raise CodexReadError(f"db_unreadable:{type(e).__name__}") from e
    try:
        con.text_factory = lambda b: b.decode("utf-8", "replace")
        for t in map(_thread, con.execute(f"SELECT {_COLUMNS} FROM threads WHERE source LIKE '%parent_thread_id%'")):
            parent = _parent(t)
            if parent:
                out.setdefault(parent, []).append(t)
    except sqlite3.Error as e:
        raise CodexReadError(f"db_unreadable:{type(e).__name__}") from e
    finally:
        con.close()
    return out


def _threads_by_id(db: Path, ids) -> dict:
    """Thread rows by id, for fork bases that are neither in the day nor spawned threads."""
    ids = sorted(set(ids))
    out: dict = {}
    if not ids:
        return out
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as e:
        raise CodexReadError(f"db_unreadable:{type(e).__name__}") from e
    try:
        con.text_factory = lambda b: b.decode("utf-8", "replace")
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i:i + _CHUNK]
            for t in map(_thread, con.execute(
                    f"SELECT {_COLUMNS} FROM threads WHERE id IN ({','.join('?' * len(chunk))})", chunk)):
                out[t.id] = t
    except sqlite3.Error as e:
        raise CodexReadError(f"db_unreadable:{type(e).__name__}") from e
    finally:
        con.close()
    return out


def _candidates(db: Path, root: Path, day: dt.date) -> tuple:
    """(threads ordered by id, folder rollouts, ids the db knows). The threads are those whose db times overlap the
    day, plus those named by an id in the file name of a rollout in the day's folders."""
    start, end = day_window(day)
    files = _folder_rollouts(root, day)
    wanted = sorted({u for _, ids in files for u in ids})
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as e:
        raise CodexReadError(f"db_unreadable:{type(e).__name__}") from e
    try:
        con.text_factory = lambda b: b.decode("utf-8", "replace")
        threads = {t.id: t for t in map(_thread, con.execute(
            f"SELECT {_COLUMNS} FROM threads WHERE updated_at >= ? AND created_at < ?",
            (int(start.timestamp()), int(end.timestamp()))))}
        known: set = set()
        for i in range(0, len(wanted), _CHUNK):
            chunk = wanted[i:i + _CHUNK]
            for t in map(_thread, con.execute(
                    f"SELECT {_COLUMNS} FROM threads WHERE id IN ({','.join('?' * len(chunk))})", chunk)):
                known.add(t.id)
                threads.setdefault(t.id, t)
    except sqlite3.Error as e:
        raise CodexReadError(f"db_unreadable:{type(e).__name__}") from e
    finally:
        con.close()
    return [threads[k] for k in sorted(threads)], files, known


def _note_title(t: _Thread) -> str:
    return note_title(t.title) or next((s for s in map(note_title, t.first.splitlines()) if s), "")


def scan_codex(day: dt.date, db: Path = DEFAULT_CODEX_DB, resolver: Callable = git_repo_for, sessions: Path | None = None, *,
               exclude_fragments: tuple = ()) -> CodexDay:
    frags = fragments(exclude_fragments)
    db = Path(db).expanduser().absolute()
    root = Path(sessions).absolute() if sessions is not None else db.parent / "sessions"
    if not db.is_file():
        raise CodexReadError("db_missing")
    if not root.is_dir():
        raise CodexReadError("sessions_missing")
    threads, files, known = _candidates(db, root, day)
    owned = {t.rollout for t in threads}
    segments: dict = {}  # thread id -> older rollout files of that thread found in the day's folders
    for path, ids in files:
        if path not in owned:
            owner = next((i for i in ids if i in known), None)
            if owner is not None:
                segments.setdefault(owner, []).append(path)
    lanes = Lanes(resolver, CodexReadError)
    n_sessions = n_subagents = skipped = vanished = unindexed = 0
    quotes: list = []
    notes: list = []
    active_main: list = []  # (thread, found, cwds, last) for every active main thread, in id order
    day_read: dict = {}  # thread id -> (files the day's scan read, whether any touched a fragment, fork bases)
    for t in threads:
        sub = _is_subagent(t)
        active, found, cwds, touched, last, read, bases = False, [], set(), False, None, set(), set()
        for i, path in enumerate([t.rollout, *segments.get(t.id, ())]):
            got = _read(path, day, t.cwd, not sub, frags)
            if got is None:
                vanished += 1
                if i == 0:  # the row's own times are all we have
                    active = in_day(t.created, day) or in_day(t.updated, day)
                    last = max((to_utc(v) for v in (t.created, t.updated) if in_day(v, day)), default=None)
                continue
            seg_active, seg_found, bad, seg_cwds, seg_touched, seg_last, seg_bases = got
            skipped += bad
            active = active or seg_active
            found += [(path, *f) for f in seg_found]
            cwds |= seg_cwds
            touched = touched or seg_touched
            bases |= seg_bases
            if seg_last is not None and (last is None or seg_last > last):
                last = seg_last
            read.add(path)
        day_read[t.id] = (read, touched, bases)
        if not active:
            continue
        if sub:
            n_subagents += 1
            continue
        n_sessions += 1
        active_main.append((t, found, cwds, last))
    index: dict = {}
    if frags:
        for tree in (_rollout_index(root), _rollout_index(db.parent / "archived_sessions", 1)):
            for u, paths in tree.items():
                index.setdefault(u, []).extend(paths)
    spawned = _spawned(db) if frags and active_main else {}
    rows = {t.id: t for t in threads} | {c.id: c for kids in spawned.values() for c in kids}
    history: dict = {}  # thread id -> (touched on any day, its row's rollout is missing, fork bases)
    zst_unread = vanished_segment = 0
    missing: set = set()
    deadline = None  # one budget for all compressed segments of the scan, from the first one read

    def own(t: _Thread) -> tuple:
        """The thread's row directory, the files the day's scan read, then every other segment of it, plain ones
        first. A compressed segment that cannot be read in full taints the thread, and so does a plain historical
        segment that vanished after indexing (its history cannot be cleared). The thread's own rollout gone is `gone`,
        judged by tree_touched."""
        nonlocal zst_unread, vanished_segment, deadline
        if t.id not in history:
            read, hit, bases = day_read.get(t.id, (set(), False, set()))
            bases, gone = set(bases), False
            hit = hit or touches(t.cwd, frags)
            paths = list(dict.fromkeys([t.rollout, *index.get(t.id, ())]))
            for path in sorted(paths, key=lambda q: q.endswith(".zst")):
                if hit:
                    break
                if path in read:
                    continue
                if path.endswith(".zst"):
                    deadline = deadline if deadline is not None else time.monotonic() + ZSTD_BUDGET
                    got = _zst_touched(path, day, t.cwd, frags, deadline - time.monotonic())
                    zst_unread += got is None
                    hit = got is None or got[0]
                    bases |= got[1] if got else set()
                else:
                    got = _read(path, day, t.cwd, False, frags)
                    gone = gone or (got is None and path == t.rollout)
                    lost = got is None and path != t.rollout  # a historical segment gone since it was indexed
                    vanished_segment += lost
                    hit = lost or (got is not None and got[4])
                    bases |= got[6] if got else set()
            history[t.id] = (hit, gone, bases - {t.id})
        return history[t.id]

    def tree_touched(t: _Thread) -> bool:
        """The thread, any thread it spawned and any thread it was forked from, along every chain, whatever day they
        ran. A spawned or base thread whose rollout (or row) is gone taints: its history cannot be cleared."""
        stack, seen = [t], {t.id}
        while stack:
            cur = stack.pop()
            hit, gone, bases = own(cur)
            if hit or (gone and cur is not t):
                missing.update([cur.id] if gone and not hit else [])
                return True
            unknown = sorted(b for b in bases if b not in rows)
            rows.update(_threads_by_id(db, unknown))
            for nid in [*(c.id for c in spawned.get(cur.id, ())), *sorted(bases)]:
                if nid in seen:
                    continue
                seen.add(nid)
                if nid not in rows:  # a base the db does not know
                    missing.add(nid)
                    return True
                stack.append(rows[nid])
        return False

    for t, found, cwds, last in active_main:
        used = {t.cwd} | cwds | {f[1] for f in found}
        origin = USERINFO.sub("", t.origin.strip())
        if origin and not repo_name(origin):
            origin = ""  # junk recorded origin: the directory decides

        def lookup(cwd: str, origin=origin, own=t.cwd):
            return origin if origin and cwd == own else lanes.lookup(cwd)

        for cwd in used:
            lookup(cwd)
        unresolved = lanes.unresolved - ({t.cwd} if origin else set())  # the origin answers for the row's directory
        session_repos, session_cwds = repo_names(used, lookup, unresolved), tuple(sorted(used))
        touched = bool(frags) and (touches(session_cwds, frags) or tree_touched(t))
        notes.append(SessionNote("codex", note_ref(t.id), _note_title(t), session_repos, session_cwds, touched,
                                 iso_utc(last)))
        for path, cwd, s, ts, n, rid in found:
            quotes.append(QuoteCandidate("codex", lookup(cwd), cwd, s, ts, path, n, rid, session_repos, session_cwds,
                                         repo_names((cwd,), lookup, unresolved)[0], touched))
    for path, ids in files:  # a rollout no thread row names: counted so the gap shows, never read as a session
        if path not in owned and known.isdisjoint(ids):
            got = _read(path, day, "", False)
            if got is None:
                vanished += 1
                continue
            skipped += got[2]
            unindexed += got[0]
    return CodexDay(n_sessions, n_subagents, tuple(quotes), skipped, vanished, len(lanes.timed_out),
                    len(lanes.unresolved), unindexed, tuple(notes), len(frags), zst_unread, len(missing),
                    digest_fragments(frags), vanished_segment=vanished_segment)
