"""What the transcript readers (Claude Code, Codex) share: the candidate, note and day types, the sentence filter,
the git lookup with its fail-closed bookkeeping, the note helpers, and the excluded-fragment taint helpers.

Nothing here reads a transcript; each reader keeps its own file layout and read errors. Transcripts are untrusted data.
"""
from __future__ import annotations

import datetime as dt
import functools
import hashlib
import json
import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from typing import Callable

from daily import textcheck
from daily.config import repo_name

NOTE_TITLE_CHARS = 120
TITLE_CHARS = 200  # a tracked task's title (daily.sources.tracker)
_SEPARATORS = ("/", "\\")
_FORBIDDEN = set("`/@*|[]<>")
_SPLIT = re.compile(r"(?<=[.!?])\s+")
# A bare host name on a common top-level domain ("example.com"). Shipped text may carry one (a channel's site), so the one
# path rule (ledger.path_or_url, which quote_ok applies too) allows it; a quote is an agent's own words and could name an
# internal host, so quotes refuse it as well.
_HOSTNAME = re.compile(r"\b[\w-]+\.(?:com|org|net|io|dev|ai|app|co|me|sh|xyz|edu|gov|us|uk|ly|so)\b", re.IGNORECASE)
# control, format (bidi marks and overrides, zero-width, tag characters), surrogate, private use, line and paragraph
# separators. U+FFFD is what errors="replace" leaves for a byte that was not UTF-8.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# What session_repos holds for a directory git could not answer for. It cannot be a lane name, so a rule that asks
# "is every repo public" fails on it, where a genuine "no remote" (None) is ignored.
UNRESOLVED = "!unresolved"
SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
USERINFO = re.compile(r"(?<=//)[^/@\s]*@")


class TranscriptReadError(Exception):
    """A transcript source cannot be read; never read as a quiet day. The message names the failure kind only (never a
    path, which spells a working directory). The Claude reader's ClaudeReadError is this class."""


class GitUnresolved(Exception):
    """`git config` gave neither a remote (exit 0) nor "no remote" (exit 1) for a directory: it failed to run, ran
    out of time, or exited some other way (a corrupt repository, say). Never to be read as "no remote"."""


class GitTimeout(GitUnresolved):
    """`git config` did not answer in time."""


@dataclass(frozen=True)
class QuoteCandidate:
    harness: str
    repo: str | None
    cwd: str
    text: str
    ts: str
    path: str
    line: int
    record: str
    # repo names (config.repo_name) of every directory the session's own records used on the day, sorted with None
    # first; None stands for a directory with no remote, UNRESOLVED for one git could not answer for. A candidate
    # built without it is unresolved.
    session_repos: tuple = (UNRESOLVED,)
    # the raw in-day working directories behind session_repos, sorted. Empty means unknown, not "none used".
    session_cwds: tuple = ()
    # the repo name of the directory this sentence was said in: a name, None for a directory that exists and has no
    # remote, UNRESOLVED for one git could not answer for (and for a candidate built without it)
    own_repo: str | None = UNRESOLVED
    # the session touched an excluded fragment, on any day (each reader's module docstring says how); only as good
    # as the exclude_fragments the reader was given, so False also means "not checked"
    touches_excluded: bool = False


@dataclass(frozen=True)
class SessionNote:
    """One main session (or Codex thread) active on the day: an opaque reference, a sanitized title, the lanes it
    touched, and whether it touched an excluded fragment."""

    harness: str
    ref: str  # first 8 hex of sha256(session or thread id): enough to tell notes apart, no way back to the session
    title: str  # control and format characters dropped, whitespace collapsed, at most NOTE_TITLE_CHARS; may be ""
    session_repos: tuple
    session_cwds: tuple
    touches_excluded: bool = False
    last_ts: str = ""  # the latest in-day record time, UTC ISO ("2026-10-16T21:30:00.000Z"); sorts newest first


@dataclass(frozen=True)
class HarnessDay:
    sessions: int
    subagents: int
    quotes: tuple


def quote_ok(s: str) -> bool:
    """The sentence filter: 6 to 30 words, no code, URL or path (ledger.path_or_url, the one path rule), bare host
    name or invisible characters, a complete sentence."""
    from daily.ledger import path_or_url  # imported here: daily.ledger imports this module
    if not isinstance(s, str) or not 6 <= len(s.split()) <= 30:
        return False
    if not s[0].isalnum() or s[-1] not in ".!?":
        return False
    if (any(ch in _FORBIDDEN for ch in s) or re.search(r"#\d", s) or path_or_url(s) or _HOSTNAME.search(s)
            or _IPV4.search(s)):
        return False
    return not any(_unsafe(ch) for ch in s)


def _unsafe(ch: str) -> bool:
    """Control, format, odd whitespace, U+FFFD, and any non-ASCII letter or digit (a lookalike of an ASCII one)."""
    return (unicodedata.category(ch) in _UNSAFE_CATEGORIES or ch == "\ufffd" or (ch.isspace() and ch != " ")
            or (ch.isalnum() and ord(ch) > 127))


def sentences(text: str) -> list:
    """Every kept sentence is an exact substring of text, so the gate can find it again verbatim."""
    out = []
    for para in text.splitlines():
        for s in _SPLIT.split(para.strip()):
            s = s.strip()
            if quote_ok(s):
                out.append(s)
    return out


@functools.lru_cache(maxsize=512)
def git_repo_for(cwd: str) -> str | None:
    """The origin remote of a working directory, with any credentials dropped; None only for a directory that exists
    and has no remote. A cwd that is empty, relative (it would resolve against this process, and a transcript's cwd is
    untrusted) or no longer a directory raises GitUnresolved, because a removed worktree may have held any repository.
    Git runs without any GIT_* variable (GIT_DIR would override -C and answer for one repository whatever the cwd).
    Exit 0 is a remote, exit 1 is "no remote" (None). A missing git binary raises TranscriptReadError; a git that cannot
    run, times out (GitTimeout) or exits any other way raises GitUnresolved, never "no remote"."""
    if not cwd or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        raise GitUnresolved("cwd_not_a_directory")  # a removed worktree may have been any repository: unknowable
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        p = subprocess.run(["git", "-C", cwd, "config", "--get", "remote.origin.url"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=10,
                           env=env)
    except FileNotFoundError as e:
        raise TranscriptReadError("git_missing") from e
    except subprocess.TimeoutExpired as e:
        raise GitTimeout("git_timeout") from e
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise GitUnresolved("git_error") from e
    if p.returncode == 0:
        return USERINFO.sub("", p.stdout.strip()) or None
    if p.returncode == 1:
        return None
    raise GitUnresolved(f"git_exit_{p.returncode}")


def _text(v) -> str:
    """A string field, or "" for any other type; a lone surrogate (from a JSON escape) becomes "?"."""
    return v.strip().encode("utf-8", "replace").decode("utf-8") if isinstance(v, str) else ""


def safe_title(v, cap: int = TITLE_CHARS) -> str:
    """A title reaches the writer as untrusted text: control and format (bidi, zero width) characters are dropped,
    whitespace of any kind becomes one space, and the result is capped at `cap`."""
    chars = []
    for ch in _text(v):
        if ch.isspace():
            chars.append(" ")
        elif unicodedata.category(ch) not in ("Cc", "Cf"):
            chars.append(ch)
    return " ".join("".join(chars).split())[:cap].rstrip()


def note_title(v) -> str:
    """A session title as untrusted text: sanitized like a task title, capped for a note."""
    return safe_title(v)[:NOTE_TITLE_CHARS].rstrip()


def note_ref(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest()[:8]


def iso_utc(t: dt.datetime | None) -> str:
    """A UTC time as one fixed ISO shape, so notes from both readers sort as strings; "" for none."""
    return t.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z") if t else ""


def fragments(exclude_fragments) -> tuple:
    """The excluded fragments to look for, stripped and lowercased. A bare string is refused: it would be read as one
    fragment per character. An entry that is empty after stripping matches everything (config forbids short ones)."""
    if isinstance(exclude_fragments, (str, bytes)):
        raise TypeError("exclude_fragments must be a collection of strings, not one string")
    items = tuple(exclude_fragments)
    if not all(isinstance(f, str) for f in items):
        raise TypeError("exclude_fragments must hold strings only")
    return tuple(dict.fromkeys(f.strip().lower() for f in items))


def digest_fragments(frags) -> str:
    """sha256 of the set of fragments (stripped, lowercased, sorted), "" for none: lets the ledger check that a reader
    applied exactly the list it expects, not just as many fragments."""
    items = sorted(set(fragments(frags)))
    return hashlib.sha256(json.dumps(items).encode("utf-8")).hexdigest() if items else ""


def contains_fragment(s: str, frags: tuple) -> bool:
    """Whether s holds one of the (lowercased) fragments, read like them: lowercased, and for text with any non-ASCII
    character also in its normal form (textcheck.normal_form), so a zero-width or fullwidth character cannot hide a
    fragment. Pure-ASCII text, nearly everything a scan reads, is only lowercased."""
    low = s.lower()
    if any(f in low for f in frags):
        return True
    if s.isascii():
        return False
    normal = textcheck.normal_form(s).lower()
    return any(f in normal for f in frags)


_NOT_JSON = object()


def _strings(value, whole: bool = True):
    """Every string inside a JSON value, keys included, with a JSON object or array held in a string (a Codex call's
    arguments) decoded and walked too, so an escaped slash or letter cannot hide a path. whole adds the value's
    serialization and the raw text of a string that holds JSON; a path test leaves both out and reads real text only,
    because a JSON escape after a bare name (the newline in a listing) would look like a Windows separator."""
    if whole:
        try:
            yield json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError, RecursionError):
            pass  # the walk below still sees every string
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            stack += [*v.keys(), *v.values()]
        elif isinstance(v, (list, tuple, set, frozenset)):
            stack += list(v)
        elif isinstance(v, str):
            decoded = _NOT_JSON
            if v.lstrip()[:1] in ("{", "["):
                try:
                    decoded = json.loads(v)
                except (ValueError, RecursionError):
                    pass
            if whole or decoded is _NOT_JSON:
                yield v
            if decoded is not _NOT_JSON:
                stack.append(decoded)


@functools.lru_cache(maxsize=64)
def _path_needles(frags: tuple) -> tuple:
    """Each fragment followed by a path separator (a fragment that already ends in one stands as it is)."""
    return tuple(n for f in frags for n in ((f,) if f.endswith(_SEPARATORS) else (f + sep for sep in _SEPARATORS)))


def touches(value, frags: tuple) -> bool:
    """Whether a JSON value holds any of the (lowercased) fragments anywhere, ignoring case. False when frags is
    empty."""
    return bool(frags) and any(contains_fragment(s, frags) for s in _strings(value))


def touches_path(value, frags: tuple) -> bool:
    """Whether a JSON value holds a path INTO a fragment (the fragment followed by a path separator), ignoring case:
    the test for a tool's output, where a bare folder name in a listing is not a touch but a path inside it is."""
    return bool(frags) and any(contains_fragment(s, _path_needles(frags)) for s in _strings(value, whole=False))


def sorted_entries(path, on_error: Callable) -> list:
    """Sorted directory entries. A missing or non-directory path propagates for the caller to judge; any other
    failure (permissions, I/O) raises on_error(the OSError), the caller's own read error."""
    try:
        with os.scandir(path) as it:
            return sorted(it, key=lambda e: e.name)
    except (FileNotFoundError, NotADirectoryError):
        raise
    except OSError as e:
        raise on_error(e) from e


def repo_names(cwds, lookup: Callable, unresolved: set) -> tuple:
    names = set()
    for cwd in cwds:
        repo = lookup(cwd)
        if cwd in unresolved:
            names.add(UNRESOLVED)
        else:
            names.add((repo_name(repo) or None) if repo else None)
    return tuple(sorted(names, key=lambda n: (n is not None, n or "")))


class Lanes:
    """Directory -> remote answers from the resolver, asked once per directory per scan, with fail-closed
    bookkeeping: a timeout or any other unanswered question is unresolved, never "no remote"; a remote that names no
    repository is unresolved too. A missing git binary raises the caller's own read error."""

    def __init__(self, resolver: Callable, error: type | None = None):
        self.resolver = resolver
        self.error = error or TranscriptReadError
        self.repos: dict = {}
        self.timed_out: set = set()
        self.unresolved: set = set()

    def lookup(self, cwd: str):
        if cwd not in self.repos:
            repo = None
            try:
                repo = self.resolver(cwd) or None
            except GitTimeout:
                self.timed_out.add(cwd)
                self.unresolved.add(cwd)
            except GitUnresolved:
                self.unresolved.add(cwd)
            except TranscriptReadError as e:  # git itself is missing
                if isinstance(e, self.error):
                    raise
                raise self.error(str(e)) from e
            if repo is not None and not repo_name(repo):
                repo = None  # a remote that names no repository can be no lane, and is not "no remote" either
                self.unresolved.add(cwd)
            self.repos[cwd] = repo
        return self.repos[cwd]
