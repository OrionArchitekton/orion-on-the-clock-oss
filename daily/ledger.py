"""The ledger: the only thing the writer ever sees.

build_ledger is pure: no file, clock or network; daily.collect (gather, run_stage) does the reading and writing.
Private (headline) lanes reach items only as an operator-written label, reason and count; anything unlisted, hard-excluded or
embargoed is dropped and counted in `excluded`.

Quotes come from two sources, all of it untrusted text:
- transcript sentences, admitted under the rules (a) to (f) in `_transcript_bar`, from any approved lane or a
  console session (no remote), but never from a session that touched anything unapproved or excluded;
- sentences of the full body of a PR merged that day in a PUBLIC lane whose harness is known (codex or claude_code).
  Each such PR becomes one line of `pr_records`, which the collect stage writes to `pr_source_path`, so the gate can
  re-read the line and find the sentence in its body.
Selection is round-robin across sessions (a PR counts as one), interleaving harnesses, newest first within a
session, deduplicated by normalized text, at most MAX_QUOTES.

The writer only chooses text the gate can pass. Every quote, item title, PR body line, note title and teaser title is
read as the gate reads it, raw and in its normal form (textcheck.forms), and is dropped and counted when its normal
form holds a non-ASCII letter or digit (a homoglyph), or either form holds an exclude fragment or a blocked name
(cfg.names_to_block; fragments as substrings, and both through `name_pattern`, the gate's own matching: separators
ignored, a word start required), an embargo term, or fails the caller's text_ok (the collect stage passes the gate's
redactor, slop and no_code checks, on both text forms). A PR or arc whose title fails gives no item (and a PR no
quotes); a failing body line is cut from the item body. Item titles and PR body lines lose their backticks before the
checks (`_item_text`), so a marked name never costs the item. Titles and body lines are kept in their normal form
(textcheck.normal_form); quotes stay verbatim, because the gate re-reads them. A title or
body line holding a URL or a path (`path_or_url`, the one path rule) is dropped the same way, counted in
`path_or_url`; quotes never hold one, since quote_ok applies the same rule and refuses any "/" as well.

The ledger keeps absolute paths only in `quotes[].source.path`; session notes keep no repos or working directories.
`writer_view` is the only part of it the writer is sent.

Every harness the collect stage tried has a `coverage` entry. One that could not read its source is "unavailable" and
gives nothing (no count, quote or note): a coverage gap, never a privacy decision, since the checks above still govern
everything that is kept. `diagnostics` holds the readers' own counters and never reaches the writer.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import itertools
import re
from dataclasses import dataclass
from typing import Callable

from daily import textcheck
from daily.config import repo_name
from daily.day import to_utc, zone_name
from daily.sources.claude import UNRESOLVED, HarnessDay, quote_ok, sentences
from daily.sources.transcripts import contains_fragment, digest_fragments, fragments

MAX_QUOTES = 40
MAX_SESSIONS = 30
MAX_TEASERS = 5
BODY_CHARS = 1200
PR_QUOTE_HARNESSES = ("codex", "claude_code")
COUNTERS = ("unlisted_lane", "hard_exclude", "embargo", "unapproved", "tainted", "fragment_in_text", "unknown_scope",
            "blocked_name", "prefiltered", "non_ascii", "path_or_url")
BUILTIN_HARNESSES = ("claude_code", "codex")  # coverage order: these first, then the adapters in configured order
HARNESS_DISPLAY = {"claude_code": "Claude Code", "codex": "Codex"}  # the built-ins' display names (settings has all)
QUOTE_HARNESSES = ("claude_code", "codex")  # the transcript readers; adapters give counts only
VIEW_KEYS = ("schema", "day", "tz", "generated_at", "scoreboard", "coverage", "items", "quotes", "sessions", "teasers",
             "excluded")
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
# The one rule for a URL or a path, read by the ledger (titles and body lines), the gate's no_code check, quote_ok and
# run.py's reasons:
# - a scheme: http, https or javascript, with or without slashes ("https:evil.com"); ftp(s), ws(s), file, ssh, git,
#   mailto or data followed directly by more than a space ("mailto:x", never "the data: three runs");
# - www., github.com/, a git@host: remote;
# - a domain or an IPv4 address followed by a path, maybe through a port ("evil.com/x", "evil.com:8080/x",
#   "203.0.113.5/admin"), or a defanged dot ("evil[.]com");
# - a home, temp, mount or user folder, a Windows drive path, or a backslash before a word character (a Windows or
#   UNC path).
# path_or_url also refuses any whitespace-free token with two or more "/" (a relative path), while a single slash ("a/b
# testing", "24/7") stays. A bare host name ("example.com") and a bare file name ("config.py") are allowed: banning
# every dotted word would drop too much ordinary text (quote_ok refuses a bare host name on its own).
_PATHY = re.compile(r"\b(?:https?|javascript):|\b(?:ftps?|wss?|file|ssh|git|mailto|data):(?=\S)"
                    r"|\bwww\.|github\.com/|\bgit@[\w.-]+:"
                    r"|(?<![\w.@/-])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?::\d{1,5})?/"
                    r"|(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?/|[a-z0-9-]+\[\.\][a-z0-9-]+"
                    r"|/home/|~/|/mnt/|/tmp/|/users/|(?<![a-z0-9])[a-z]:[\\/]|\\\w", re.IGNORECASE)
_DOTS = {0x3002: ".", 0xFF0E: ".", 0xFF61: "."}  # ideographic, fullwidth and halfwidth full stops read as dots


class LedgerError(Exception):
    """The ledger cannot be built safely (`exclude_not_applied:<harness>`); never written as a partial ledger."""


@dataclass(frozen=True)
class RawSources:
    """What the collect stage read. A harness day is None when its reader was not run or could not read its source;
    `coverage` says which (harness id -> "ok" | "unavailable:<reason>"), and a harness it marks unavailable is ignored
    even when a day is passed. With no coverage entry, a harness is "ok" when its day is given and untried when not."""

    prs: tuple
    arcs: tuple  # ArcClose
    claude: HarnessDay | None  # a ClaudeDay, or a plain HarnessDay (no notes, no fragments_digest)
    codex: HarnessDay | None  # a CodexDay, or a plain HarnessDay
    active: tuple = ()  # ActiveArc, from the tracker adapter (daily.sources.tracker)
    others: dict | None = None  # adapter harness id -> its day (counts only), in the configured order
    coverage: dict | None = None
    diagnostics: dict | None = None  # reader counters, copied into the ledger as they are
    prs_read: bool = True  # False when GitHub is switched off: prs_merged is then unknown (None), not zero
    arcs_read: bool = True  # False when no tracker is configured: arcs_closed is then unknown (None), not zero


def quote_id(harness: str, text: str, path: str, line: int) -> str:
    return f"q:{harness}:" + hashlib.sha256(f"{text}{path}{line}".encode("utf-8")).hexdigest()[:8]


def arc_ref(arc_id: str) -> str:
    """The ledger's id for a tracked arc: "arc:" plus the first 8 hex of sha256(arc id). A tracker's arc id is free text
    (it can spell a private repo), so it never enters the ledger; the writer and the gate use this id as it is."""
    return "arc:" + hashlib.sha256(arc_id.encode("utf-8", "replace")).hexdigest()[:8]


def exclude_fragments(cfg) -> tuple:
    """Every hard_exclude entry and its repo name, each as written and in its normal form (textcheck.normal_form, the
    form text is read in), stripped and lowercased: what the transcript readers are given as exclude_fragments, and
    what no quote, note title or teaser title may contain."""
    return fragments(g for e in cfg.hard_exclude for f in (e, repo_name(e)) for g in (f, textcheck.normal_form(f)))


def _when(ts) -> dt.datetime:
    return to_utc(ts) or _EPOCH


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def non_ascii(text: str) -> bool:
    """A letter or digit outside ASCII in (normalized) text: a homoglyph could carry a blocked name past the checks."""
    return any(ch.isalnum() and ord(ch) > 127 for ch in text)


def path_or_url(text: str) -> bool:
    """A URL or a path-like token (_PATHY, or a token with two or more "/") in the raw text or its normal form
    (textcheck.normal_form, with ideographic and fullwidth full stops read as dots): the one path rule, which the gate,
    quote_ok and the orchestrator use too."""
    for form in dict.fromkeys((text, textcheck.normal_form(text).translate(_DOTS))):
        if _PATHY.search(form) or any(token.count("/") >= 2 for token in form.split()):
            return True
    return False


def name_pattern(name: str) -> re.Pattern | None:
    """A blocked name matched with every separator ignored: its letters and digits in order, any run of other
    characters (or none) between them, ignoring case ("garden-shed" hits "gardenshed", "GardenShed", "gar den shed" and
    "Garden.Shed"). It must start a word: after a character that is not a letter or digit, or at a change from lower to
    upper case ("OpenZephyr"), so a short name never hits inside a longer word ("openzephyr" passes). The name is
    read in its normal form (textcheck.normal_form), like the text, so an accented name hits both spellings once the
    text is normalized too. None for a name with no letter or digit."""
    chars = [c for c in textcheck.normal_form(name) if c.isalnum()]
    if not chars:
        return None
    return re.compile(r"(?:(?<![^\W_])|(?<=[a-z])(?=[A-Z]))(?i:" + r"[\W_]*".join(map(re.escape, chars)) + ")")


def _name_words(name: str) -> tuple:
    return tuple(re.findall(r"[a-z0-9]+", textcheck.normal_form(name).lower()))


# A harness display name, and its first word ("claude" for "Claude Code"): a private repo whose whole name is one of
# these (a private ".claude" settings repo, say) is not a blocked name, or every script that says "Claude Code" would
# fail.
HARNESS_WORDS = frozenset(" ".join(w) for d in HARNESS_DISPLAY.values() for w in (_name_words(d), _name_words(d)[:1]))


def is_harness_word(name: str) -> bool:
    """Whether a name, read in its normal form with separators ignored, is nothing but a harness display word."""
    return " ".join(_name_words(name)) in HARNESS_WORDS


def blockable_private_names(names) -> tuple:
    """The ledger's private_names that are blocked in shipped text: all but a bare harness word. Only private repo
    names go through this; blocked_names, headline lane names and hard_exclude entries are never relaxed. The one
    rule for the ledger's text bar and the gate's names check."""
    return tuple(n for n in names if not is_harness_word(n))


def _name_matchers(names) -> list:
    return [r for r in map(name_pattern, names) if r is not None]


def _available(raw: RawSources) -> tuple[dict, dict]:
    """(coverage, days): coverage for every harness tried, the built-ins first and then the adapters in their configured
    order (a harness named only in raw.coverage comes last), and the day of each harness that is "ok". A harness marked
    "ok" with no day is not ok: it has nothing to show."""
    given = raw.coverage or {}
    days = {"claude_code": raw.claude, "codex": raw.codex, **{h: d for h, d in (raw.others or {}).items()
                                                               if h not in BUILTIN_HARNESSES}}
    days.update({h: None for h in given if h not in days})
    coverage: dict = {}
    for h in days:
        state = given.get(h, "ok" if days[h] is not None else None)
        if state == "ok" and days[h] is None:
            state = "unavailable:no_data"
        if state is not None:
            coverage[h] = state
    return coverage, {h: days[h] for h in days if coverage.get(h) == "ok"}


def _check_excludes_applied(days: dict, frags: tuple) -> None:
    """A reader that handed over quotes or notes must have applied exactly these fragments (same digest), or its
    touches_excluded flags mean nothing: a missing, partial or different list is refused. A reader that saw nothing,
    or is unavailable (nothing of it is used), needs no check, and neither does a config with no hard_exclude."""
    if not frags:
        return
    want = digest_fragments(frags)
    for harness in QUOTE_HARNESSES:
        got = days.get(harness)
        if got is None:
            continue
        if (got.quotes or getattr(got, "notes", ())) and getattr(got, "fragments_digest", None) != want:
            raise LedgerError(f"exclude_not_applied:{harness}")


def _scope_bar(cfg, frags: tuple, session_repos, session_cwds, touched: bool) -> str | None:
    """Why a session may not be quoted or noted (rules b, c, d), or None. An empty session_cwds, or one holding a blank
    entry (a Claude record with no cwd, a Codex thread with an empty one), is an unknown scope."""
    if not session_cwds or any(not isinstance(c, str) or not c.strip() for c in session_cwds):
        return "unknown_scope"
    for repo in session_repos:
        if repo is not None and repo != UNRESOLVED and cfg.classify(repo)[0] != "lane":
            return "tainted"  # (b) a resolved repo that is unlisted or hard-excluded
    if any(contains_fragment(cwd, frags) for cwd in session_cwds):
        return "tainted"  # (c)
    if touched:
        return "tainted"  # (d)
    return None


def _private_names(raw: RawSources, days: dict, cfg) -> list:
    """The repo names of every resolved repo seen on the day that is not a public lane, sorted: PR and arc (closed or
    active) repos, and the own_repo and session_repos of each transcript quote and session note, when the lane is
    unlisted, headline or hard-excluded. The gate blocks them in shipped text (S2: a non-public repo name fails), and
    the ledger drops text naming them, like the gate. Host and machine names are not repo names: they stay covered by
    lanes.json blocked_names and settings.toml checks.redact_patterns. Never in writer_view."""
    seen = [pr.repo for pr in raw.prs] + [a.repo for a in (*raw.arcs, *raw.active)]
    for h in QUOTE_HARNESSES:
        if h in days:
            for c in days[h].quotes:
                seen += [c.own_repo, *c.session_repos]
            for n in getattr(days[h], "notes", ()):
                seen += list(n.session_repos)
    names = set()
    for repo in seen:
        if not isinstance(repo, str) or repo == UNRESOLVED or not repo_name(repo):
            continue
        lane = cfg.classify(repo)[1]
        if lane is None or lane.disclosure != "public":
            names.add(repo_name(repo))
    return sorted(names)


def _text_checks(cfg, frags: tuple, text_ok: Callable[[str], bool] | None,
                 private_names=()) -> Callable[[str], str | None]:
    """The check every text bound for the writer passes (rule e, then the prefilter): why it is dropped, or None.
    Like the gate, the checks read the raw text and its normal form (textcheck.forms) and a hit on either drops it;
    a letter or digit outside ASCII is looked for in the normal form, as the gate's names check does (accents are
    dropped there, a Cyrillic look-alike is not). text_ok gets the text as it will be kept."""
    frag_res, blocked = _name_matchers(frags), _name_matchers((*cfg.names_to_block(), *blockable_private_names(private_names)))

    def bar(text: str) -> str | None:
        texts = textcheck.forms(text)
        if non_ascii(texts[-1]):
            return "non_ascii"
        if any(contains_fragment(t, frags) or any(r.search(t) for r in frag_res) for t in texts):
            return "fragment_in_text"
        if any(r.search(t) for r in blocked for t in texts):
            return "blocked_name"
        if cfg.embargo_hit(text):
            return "embargo"
        if text_ok is not None and not text_ok(text):
            return "prefiltered"
        return None

    return bar


def _transcript_bar(cfg, frags: tuple, text_bar: Callable, cand) -> tuple[str | None, tuple | None]:
    """(counter, None) for a dropped candidate, (None, None) for one that is not a sentence (uncounted), or
    (None, (lane, disclosure)) for an admitted one; both are "console" for no remote or an unresolved directory."""
    own = cand.own_repo
    resolved = own is not None and own != UNRESOLVED
    kind, lane = cfg.classify(own if resolved else None, cand.cwd)
    if kind == "hard_exclude":
        return "hard_exclude", None  # (a) its own repo or directory is excluded
    if resolved and lane is None:
        return "unapproved", None  # (a) a resolved repo absent from lanes, client work say
    bar = _scope_bar(cfg, frags, cand.session_repos, cand.session_cwds, cand.touches_excluded)
    if bar:
        return bar, None
    bar = text_bar(cand.text)  # (e), before (f) so a homoglyph sentence is counted
    if bar:
        return bar, None
    if not quote_ok(cand.text):
        return None, None  # (f)
    return None, (lane.name, lane.disclosure) if resolved else ("console", "console")


def _select(quotes: list) -> list:
    """Round-robin across sessions (transcript path, or PR record), groups ordered by their newest quote with
    harnesses interleaved, newest first within a group; first pick of each normalized text wins; at most MAX_QUOTES."""
    groups: dict = {}
    for qd in quotes:
        key = (qd["kind"], qd["source"]["record"] if qd["kind"] == "pr" else qd["source"]["path"])
        groups.setdefault(key, []).append(qd)
    for g in groups.values():
        g.sort(key=lambda qd: _when(qd["ts"]), reverse=True)  # stable: ties keep reader (sentence) order
    ordered = sorted(groups.items(), key=lambda kv: kv[0])
    ordered.sort(key=lambda kv: _when(kv[1][0]["ts"]), reverse=True)
    by_harness: dict = {}  # insertion order: the harness with the newest group first
    for _, g in ordered:
        by_harness.setdefault(g[0]["harness"], []).append(g)
    rows = [g for row in itertools.zip_longest(*by_harness.values()) for g in row if g is not None]
    picked, seen = [], set()
    for r in range(max((len(g) for g in rows), default=0)):
        for g in rows:
            if r >= len(g) or _norm(g[r]["text"]) in seen:
                continue
            seen.add(_norm(g[r]["text"]))
            picked.append(g[r])
            if len(picked) == MAX_QUOTES:
                return picked
    return picked


def _title_bar(text_bar: Callable, excluded: dict, title: str) -> str | None:
    """Why a (normalized, non-blank) title or body line is dropped, counted; None to keep it. Blank text is not
    checked."""
    bar = ("path_or_url" if path_or_url(title) else text_bar(title)) if title.strip() else None
    if bar:
        excluded[bar] += 1
    return bar


def _item_text(text: str) -> str:
    """An item title or PR body line in its normal form (textcheck.normal_form) with backticks removed: dev titles
    mark names with them ("Fix the `foo` flag"), so the marks go, not the item. Every other check, code shapes
    included, still runs on the rest. Quotes and note and teaser titles are never changed this way: a backtick drops
    them."""
    return textcheck.normal_form(text).replace("`", "")


def _kept_body(text_bar: Callable, excluded: dict, body: str) -> str:
    """The body lines the text checks pass, in their normal form; blank lines stay. Dropped lines are counted."""
    lines = []
    for line in map(_item_text, body.splitlines()):
        if not _title_bar(text_bar, excluded, line):
            lines.append(line)
    return "\n".join(lines)


def _items(raw: RawSources, cfg, text_bar: Callable, excluded: dict) -> tuple[list, list]:
    """(items, quotable): public PR and arc items newest first (each with its lane's label and why, so the writer can
    name the project by its label), then one headline item per private lane; quotable holds (lane, pr) for each
    public, kept PR whose harness can be quoted. Every item carries its `source` (S1): a PR {"record":
    "pr:<lane>#<n>", "url"}, an arc {"record": "arc:<raw arc id>"} (the raw id stays in this private ledger; the item
    id and writer_view use arc_ref), a headline item {"records": [the PR and arc records it counts]}."""
    items: list = []
    headline: dict = {}
    quotable: list = []  # (lane, pr): public, not embargoed, known harness

    def lane_for(repo):
        kind, lane = cfg.classify(repo)
        if lane is None:
            excluded[kind] += 1
        return lane

    for pr in raw.prs:
        lane = lane_for(pr.repo)
        if lane is None:
            continue
        if lane.disclosure == "headline":
            headline.setdefault(lane.name, []).append(f"pr:{lane.name}#{pr.number}")
            continue
        if cfg.embargo_hit(f"{pr.title}\n{pr.body}"):
            excluded["embargo"] += 1
            continue  # an embargoed PR gives no quotes either, however clean a sentence of it looks
        title = _item_text(pr.title)
        if _title_bar(text_bar, excluded, title):
            continue  # nor does a PR whose title the gate would block
        body = _kept_body(text_bar, excluded, pr.body)[:BODY_CHARS]
        items.append({"id": f"pr:{lane.name}#{pr.number}", "kind": "pr", "lane": lane.name, "disclosure": "public",
                      "label": lane.label, "why": lane.why, "harness": pr.harness, "title": title, "body": body,
                      "url": pr.url, "ts": pr.closed_at,
                      "source": {"record": f"pr:{lane.name}#{pr.number}", "url": pr.url}})
        if pr.harness in PR_QUOTE_HARNESSES:
            quotable.append((lane, pr))
    for arc in raw.arcs:
        lane = lane_for(arc.repo)
        if lane is None:
            continue
        if lane.disclosure == "headline":
            headline.setdefault(lane.name, []).append(f"arc:{arc.arc_id}")
            continue
        title = _item_text(arc.title)
        if _title_bar(text_bar, excluded, title):
            continue
        items.append({"id": arc_ref(arc.arc_id), "kind": "arc", "lane": lane.name, "disclosure": "public",
                      "label": lane.label, "why": lane.why, "harness": arc.harness, "title": title, "ts": arc.ts,
                      "source": {"record": f"arc:{arc.arc_id}"}})
    items.sort(key=lambda i: i["id"])
    items.sort(key=lambda i: _when(i["ts"]), reverse=True)
    for name in sorted(headline):
        lane = cfg.lanes[name]
        if cfg.embargo_hit(f"{lane.label}\n{lane.why}"):
            excluded["embargo"] += 1
            continue
        items.append({"id": f"lane:{name}", "kind": "headline", "lane": name, "disclosure": "headline",
                      "harness": "unknown", "label": lane.label, "why": lane.why, "count": len(headline[name]),
                      "source": {"records": headline[name]}})
    return items, quotable


def _transcript_quotes(days: dict, cfg, frags: tuple, text_bar: Callable, excluded: dict) -> list:
    admitted: list = []
    for cand in [c for h in QUOTE_HARNESSES if h in days for c in days[h].quotes]:
        bar, where = _transcript_bar(cfg, frags, text_bar, cand)
        if bar:
            excluded[bar] += 1
        if where is None:
            continue
        admitted.append({"id": quote_id(cand.harness, cand.text, cand.path, cand.line), "kind": "transcript",
                         "harness": cand.harness, "lane": where[0], "disclosure": where[1], "text": cand.text,
                         "ts": cand.ts, "source": {"path": cand.path, "line": cand.line, "record": cand.record}})
    return admitted


def _pr_quotes(quotable: list, text_bar: Callable, excluded: dict, pr_source_path: str) -> tuple[list, list]:
    """(quotes, pr_records): the sentences of each quotable PR's full body that pass the text checks, and one record
    per PR that gave at least one, in line order."""
    admitted: list = []
    pr_records: list = []
    for lane, pr in sorted(quotable, key=lambda lp: (lp[0].name, lp[1].number)):
        kept = []
        for s in sentences(pr.body):  # each passes quote_ok and is an exact substring of the body
            bar = text_bar(s)
            if bar:
                excluded[bar] += 1
            else:
                kept.append(s)
        if not kept:
            continue
        pr_records.append({"repo": pr.repo, "number": pr.number, "harness": pr.harness, "closed_at": pr.closed_at,
                           "body": pr.body})
        line, record = len(pr_records), f"pr:{lane.name}#{pr.number}"
        for s in kept:
            admitted.append({"id": quote_id(pr.harness, s, pr_source_path, line), "kind": "pr",
                             "harness": pr.harness, "lane": lane.name, "disclosure": "public", "text": s,
                             "ts": pr.closed_at, "source": {"path": pr_source_path, "line": line, "record": record}})
    return admitted, pr_records


def _sessions(days: dict, cfg, frags: tuple, text_bar: Callable, excluded: dict) -> list:
    """Session notes that pass rules (b), (c), (d) with a kept title: four fields only, newest first, capped."""
    sessions: list = []
    for n in [n for h in QUOTE_HARNESSES if h in days for n in getattr(days[h], "notes", ())]:
        if _scope_bar(cfg, frags, n.session_repos, n.session_cwds, n.touches_excluded):
            continue
        title = textcheck.normal_form(n.title)
        if not title.strip() or _title_bar(text_bar, excluded, title):
            continue
        sessions.append({"harness": n.harness, "ref": n.ref, "title": title, "last_ts": n.last_ts})
    sessions.sort(key=lambda s: (s["harness"], s["ref"]))
    sessions.sort(key=lambda s: _when(s["last_ts"]), reverse=True)
    return sessions[:MAX_SESSIONS]


def _teasers(raw: RawSources, cfg, text_bar: Callable, excluded: dict) -> list:
    """Open arcs with activity on the day, in public lanes, with a kept title: newest first, capped."""
    teasers: list = []
    for a in raw.active:
        lane = cfg.resolve_lane(a.repo)
        if lane is None or lane.disclosure != "public":
            continue  # a headline lane's arc titles never reach the writer
        title = textcheck.normal_form(a.title)
        if not title.strip() or _title_bar(text_bar, excluded, title):
            continue
        teasers.append((a.last_ts, {"id": arc_ref(a.arc_id), "title": title, "harness": a.harness}))
    teasers.sort(key=lambda row: row[1]["id"])
    teasers.sort(key=lambda row: _when(row[0]), reverse=True)
    return [t for _, t in teasers[:MAX_TEASERS]]


def build_ledger(day: dt.date, raw: RawSources, cfg, generated_at: str, pr_source_path: str, *,
                 text_ok: Callable[[str], bool] | None = None) -> tuple[dict, list]:
    """(ledger, pr_records). pr_records are the PRs that gave at least one quote, in line order, each
    {"repo", "number", "harness", "closed_at", "body"} with the full body; a PR quote's source.line is its 1-based
    position there. text_ok, when given, must accept a quote, item title, PR body line, note title or teaser title for
    it to be kept. `excluded` counts dropped items, quotes and body lines, and note and teaser titles dropped by the
    text checks.
    `scoreboard.prs_merged` and `arcs_closed` count every PR and closed arc except those whose repo is hard-excluded
    (unlisted and headline repos count; they are numbers only); each is None (unknown) when its source was not read
    (raw.prs_read, raw.arcs_read), never a zero that was not counted. `coverage` holds every harness tried, built-ins
    first; a harness that is not "ok" is in no scoreboard map and gives nothing. `diagnostics` is raw.diagnostics,
    copied.
    Raises LedgerError when a reader's data was not checked against every exclude fragment."""
    frags = exclude_fragments(cfg)
    coverage, days = _available(raw)
    _check_excludes_applied(days, frags)
    private_names = _private_names(raw, days, cfg)
    text_bar = _text_checks(cfg, frags, text_ok, private_names)
    excluded = dict.fromkeys(COUNTERS, 0)
    items, quotable = _items(raw, cfg, text_bar, excluded)
    admitted = _transcript_quotes(days, cfg, frags, text_bar, excluded)
    pr_quotes, pr_records = _pr_quotes(quotable, text_bar, excluded, str(pr_source_path))
    sessions = _sessions(days, cfg, frags, text_bar, excluded)
    teasers = _teasers(raw, cfg, text_bar, excluded)
    ledger = {
        "schema": "daily-build/ledger/v1",
        "day": day.isoformat(),
        "tz": zone_name(),
        "generated_at": generated_at,
        "scoreboard": {
            # hard-excluded repos are never counted; unlisted repos are (numbers only)
            "prs_merged": sum(cfg.classify(pr.repo)[0] != "hard_exclude" for pr in raw.prs) if raw.prs_read else None,
            "arcs_closed": (sum(cfg.classify(arc.repo)[0] != "hard_exclude" for arc in raw.arcs)
                            if raw.arcs_read else None),
            "sessions": {h: d.sessions for h, d in days.items()},
            "subagents": {h: d.subagents for h, d in days.items()},
        },
        "coverage": coverage,
        "items": items,
        "quotes": _select(admitted + pr_quotes),
        "sessions": sessions,
        "teasers": teasers,
        "excluded": excluded,
        "private_names": private_names,
        "diagnostics": copy.deepcopy(raw.diagnostics or {}),
    }
    return ledger, pr_records


def _private_aliases(ledger: dict) -> dict:
    """Each headline lane in the items or quotes -> its opaque alias, "private-1", "private-2", ... in sorted name
    order: how writer_view names private lanes, and what resolve_view_id reverses."""
    private = sorted({i["lane"] for i in ledger["items"] if i["kind"] == "headline"}
                     | {x["lane"] for x in ledger["quotes"] if x["disclosure"] == "headline"})
    return {name: f"private-{n}" for n, name in enumerate(private, 1)}


def resolve_view_id(ledger: dict, view_id) -> str | None:
    """The ledger id behind a story id the writer copied from writer_view(ledger): an item id as it is, a headline
    alias "lane:private-N" as its "lane:<name>" item, and "session:<ref>" (unchanged) when ledger.sessions holds that
    ref. A raw ledger item id resolves to itself. None for anything else, for an alias whose lane has no item (it
    shows only in a quote), and for an alias that is also a different item's raw id (ambiguous)."""
    if not isinstance(view_id, str):
        return None
    ids = {i["id"] for i in ledger["items"]}
    unalias = {f"lane:{alias}": f"lane:{name}" for name, alias in _private_aliases(ledger).items()}
    if view_id in unalias:
        target = unalias[view_id]
        return target if target in ids and (view_id not in ids or view_id == target) else None
    if view_id in ids:
        return view_id
    ref = view_id.removeprefix("session:")
    if view_id.startswith("session:") and ref and ref in {s["ref"] for s in ledger.get("sessions") or ()}:
        return view_id
    return None


def writer_view(ledger: dict) -> dict:
    """The only part of the ledger the writer is sent: the VIEW_KEYS only (never diagnostics), with coverage
    cut to "ok" or "unavailable" (no reason), and never private_names. No source or url field survives; quotes keep id, kind, harness, lane,
    text and ts; each headline lane is named by an opaque id ("private-1", "private-2", ... in sorted name order),
    consistently in item ids, item lanes and quote lanes. The ledger itself is not changed."""
    alias = _private_aliases(ledger)
    view = copy.deepcopy({k: ledger[k] for k in VIEW_KEYS if k in ledger})
    if "coverage" in view:
        view["coverage"] = {h: "ok" if state == "ok" else "unavailable" for h, state in view["coverage"].items()}
    for item in view["items"]:
        item.pop("url", None)
        item.pop("source", None)
        if item["kind"] == "headline":
            item["lane"] = alias[item["lane"]]
            item["id"] = f"lane:{item['lane']}"
    view["quotes"] = [{"id": x["id"], "kind": x["kind"], "harness": x["harness"],
                       "lane": alias[x["lane"]] if x["disclosure"] == "headline" else x["lane"],
                       "text": x["text"], "ts": x["ts"]} for x in view["quotes"]]
    return view
