"""The text gate: the script's shape, then fourteen checks that each record evidence, and the verdict.

Every check passes only on positive evidence, and one that raises records `error:<ExcClass>` and fails. No evidence
string holds script text, quote text, a blocked name or a secret: only codes, counts and kinds. `accepts` is the one way
a consumer reads a verdict, and `run_stage` is the pipeline stage that reads and writes the work directory.
"""
from __future__ import annotations

import errno
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from daily import paths, textcheck
from daily.config import repo_name
from daily.files import now_iso, sha256_bytes, write_json
from daily.settings import DEFAULT_PUBLIC_HOSTS
from daily.ledger import (blockable_private_names, exclude_fragments, name_pattern, non_ascii, path_or_url, quote_id, resolve_view_id,
                          writer_view)
from daily.sources.transcripts import SAFE_ID, contains_fragment
from daily.stage import StageResult

SCRIPT_SCHEMA = "daily-build/script/v1"
TOP = {"schema": str, "day": str, "quiet": bool, "quiet_reason": str, "story_item_id": str, "title": str,
       "description": str, "cold_open": dict, "scoreboard": dict, "story": dict, "steal_this": dict,
       "confessional": dict, "out": dict}
TOP_LIMITS = {"quiet_reason": 200, "story_item_id": 200, "title": 70, "description": 400}
NESTED = {
    "cold_open": {"host": 400, "on_screen": 42},
    "scoreboard": {"host": 400},
    "story": {"host": (400, 5), "on_screen": (42, 4)},
    "steal_this": {"host": 400, "on_screen": 42},
    "confessional": {"host_intro": 300, "quote_id": 40},
    "out": {"host": 400, "teaser": 42},
}
# Text a story (not quiet) script must fill in; story.host also needs at least one line, and story.on_screen is
# optional (0 to 4 items) but no item present may be blank.
REQUIRED_TEXT = ("story_item_id", "title", "description", "cold_open.host", "cold_open.on_screen",
                 "scoreboard.host", "steal_this.host", "steal_this.on_screen", "out.host", "out.teaser")


def _text(script: dict, name: str) -> str:
    """A top-level field, or a section field named like `out.teaser`."""
    head, _, tail = name.partition(".")
    return script[head][tail] if tail else script[head]


def validate_script(script, day: str) -> list:
    """Problems with the script's shape; an empty list means the shape is right.

    Shape problems mirror script_schema.json exactly (keys, types, lengths, counts, the schema tag). The day must
    match. A quiet script needs a non-blank quiet_reason. A story (not quiet) script needs every REQUIRED_TEXT
    field and every story line non-blank, story.host non-empty, and a confessional that is either absent
    (quote_id and host_intro both exactly "") or complete (both non-blank). Content rules (code, word budget)
    belong to the gate.
    """
    if not isinstance(script, dict):
        return ["not_object"]
    if set(script) != set(TOP):
        return ["keys:" + ",".join(sorted(map(str, set(script) ^ set(TOP))))]
    bad = [k for k, t in TOP.items() if not isinstance(script[k], t)]
    if bad:
        return ["type:" + ",".join(bad)]
    p = []
    if script["schema"] != SCRIPT_SCHEMA:
        p.append("schema_const")
    if script["day"] != day:
        p.append("day_mismatch")
    for key, lim in TOP_LIMITS.items():
        if len(script[key]) > lim:
            p.append(f"too_long:{key}")
    for section, fields in NESTED.items():
        obj = script[section]
        if set(obj) != set(fields):
            p.append(f"keys:{section}")
            continue
        for field, lim in fields.items():
            v = obj[field]
            if isinstance(lim, tuple):
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    p.append(f"type:{section}.{field}")
                    continue
                if len(v) > lim[1]:
                    p.append(f"too_many:{section}.{field}")
                if any(len(x) > lim[0] for x in v):
                    p.append(f"too_long:{section}.{field}")
            elif not isinstance(v, str):
                p.append(f"type:{section}.{field}")
            elif len(v) > lim:
                p.append(f"too_long:{section}.{field}")
    if p:
        return p
    if script["quiet"]:
        if not script["quiet_reason"].strip():
            p.append("empty_required_field")
        return p
    conf = script["confessional"]
    texts = [_text(script, name) for name in REQUIRED_TEXT]
    texts += script["story"]["host"] + script["story"]["on_screen"]
    conf_blank = any(v != "" and not v.strip() for v in (conf["quote_id"], conf["host_intro"]))
    if not all(t.strip() for t in texts) or conf_blank:
        p.append("empty_required_field")
    if not conf_blank and (conf["quote_id"] == "") != (conf["host_intro"] == ""):
        p.append("confessional_mismatch")
    if not script["story"]["host"]:
        p.append("empty:story.host")
    return p


VERDICT_SCHEMA = "daily-build/verdict/v1"
REQUIRED = ("schema", "quiet", "story_in_ledger", "quote_in_ledger", "quote_verbatim", "redactor", "slop",
            "secrets", "names", "embargo", "word_budget", "no_code", "scoreboard_numbers", "numbers_grounded")
# The render gate's required checks live here too, so every consumer (render, publish) reads one source.
RENDER_REQUIRED = ("duration", "format", "loudness", "audio_present", "captions_match")
WORDS_MIN, WORDS_MAX = 165, 210  # was 120-175; the clone speaks ~3.3 words/s, so 137 words ran 41 s of a 60 s minimum
NO_CONFESSIONAL = "no confessional"
_KNOWN_FIELDS = frozenset(TOP) | {f"{s}.{f}" for s, fields in NESTED.items() for f in fields}
_CALL = re.compile(r"\b[A-Za-z_]\w*\((?!s\))")  # a call shape, word( ; "agent(s)" is prose
_WORD = re.compile(r"[A-Za-z0-9]+")  # the one word-cap count
_CODE_MARKS = (("backtick", "`"), ("code_marks", "{"), ("code_marks", "}"), ("code_marks", "=>"),
               ("code_marks", "();"))
_GIT = ("add", "am", "apply", "bisect", "blame", "branch", "checkout", "cherry-pick", "clean", "clone", "commit",
        "config", "diff", "fetch", "grep", "init", "log", "merge", "mv", "pull", "push", "rebase", "remote", "reset",
        "restore", "revert", "rm", "show", "stash", "status", "switch", "tag", "worktree")
_NPM = ("install", "i", "ci", "run", "test", "start", "publish", "exec", "init", "update", "uninstall", "link",
        "pack", "audit")
# sudo, pip install, npm or git with a subcommand (whole words), cd followed by a path ("cd ~", "cd ..", "cd a/b"),
# rm with a recursive or force flag (short or long), or a pipe into a shell or an interpreter ("| bash",
# "| sudo sh", "| python3"). A word followed by a space and a parenthesis ("foo (x)") is prose, not a call.
_COMMAND = re.compile(r"(?<![\w-])(?:sudo|pip3?\s+install|npm\s+(?:%s)|git\s+(?:%s))(?![\w-])"
                      r"|(?<![\w/-])cd\s+(?:[~./]|[^\s/]+/)"
                      r"|(?<![\w-])rm\s+(?:-[A-Za-z]*[rRf]|--(?:recursive|force)\b)"
                      r"|\|\s*(?:sudo\s+)?(?:(?:ba|z|da|k|fi)?sh|python3?|node|perl|ruby)\b"
                      % ("|".join(_NPM), "|".join(map(re.escape, _GIT))), re.IGNORECASE)
_KIND = re.compile(r"[\d\s]*([A-Za-z][A-Za-z -]*)")  # a finding's leading words, before any value it quotes
_NUMBER = re.compile(r"\d+(?:\.\d+)?%?")  # a run of decimal digits (any script), maybe a decimal part, maybe a %


class SecretsUnavailable(Exception):
    """The secret-shape scanner (secret_scan.py) cannot be loaded from the checks folder."""


@dataclass(frozen=True)
class Deps:
    redactor_decision: Callable  # text -> ("PUBLISH", []) or ("ABSTAIN", [(class, masked snippet), ...])
    slop_scan: Callable  # text -> [] when clean, else [(rule, listed term or codepoint label), ...]
    secrets_scan: Callable  # text -> [] when clean, else findings that name a kind and withhold the value


def _unavailable(exc: type) -> Callable:
    def check(text):
        raise exc(exc.__name__)
    return check


def default_deps(settings=None) -> Deps:
    """The redactor and slop gate the collect stage's prefilter uses (daily.textcheck, with the operator's extra
    redact patterns), and the secret-shape scanner (`secret_scan.scan_output`, with settings.toml `checks.public_hosts`),
    each loaded by file location from the checks folder (textcheck.checks_dir) under a private module name: sys.path
    and sys.modules stay untouched, so no shadow module can stand in. Never raises: a scanner that cannot be loaded
    raises when it runs, so the gate records its check as failed."""
    try:
        checks = textcheck.load(settings)
        redactor, slop = checks.redactor_decision, checks.slop_scan
    except textcheck.TextcheckUnavailable:
        redactor = slop = _unavailable(textcheck.TextcheckUnavailable)
    hosts = getattr(settings, "public_hosts", None)
    hosts = DEFAULT_PUBLIC_HOSTS if hosts is None else tuple(hosts)  # an empty list means no host at all, not the default
    try:
        scan = textcheck.load_module(textcheck.checks_dir(settings) / "secret_scan.py", "secret_scan").scan_output
        if not callable(scan):
            raise SecretsUnavailable("secrets_unavailable")
    except Exception:  # missing, linked from outside the folder, broken on load, no scan_output
        secrets = _unavailable(SecretsUnavailable)
    else:
        def secrets(text):
            return scan(text, public_hosts=hosts)
    return Deps(redactor, slop, secrets)


def shipped_fields(script: dict) -> list:
    """Every authored string that reaches a viewer, spoken or shown."""
    return [script["title"], script["description"], script["cold_open"]["host"], script["cold_open"]["on_screen"],
            script["scoreboard"]["host"], *script["story"]["host"], *script["story"]["on_screen"],
            script["steal_this"]["host"], script["steal_this"]["on_screen"], script["confessional"]["host_intro"],
            script["out"]["host"], script["out"]["teaser"]]


def shipped_text(script: dict, quote_text: str = "") -> str:
    """The shipped fields one per line, plus the chosen quote's text: it ships on screen and in audio too."""
    return "\n".join(shipped_fields(script) + ([quote_text] if quote_text else []))


def host_beats(script: dict) -> dict:
    """Each beat's host lines, in episode order (the writer reports word counts per beat)."""
    return {"cold_open": [script["cold_open"]["host"]], "scoreboard": [script["scoreboard"]["host"]],
            "story": list(script["story"]["host"]), "steal_this": [script["steal_this"]["host"]],
            "confessional": [script["confessional"]["host_intro"]], "out": [script["out"]["host"]]}


def host_lines(script: dict) -> list:
    return [line for lines in host_beats(script).values() for line in lines]


def line_words(line: str) -> int:
    """One line's words, counted the way every word cap here is enforced: len(re.findall(r"[A-Za-z0-9]+", ...)) on the
    line's normal form (textcheck.normal_form). A hyphen chain counts every word, punctuation counts none and an
    invisible character cannot split a word; a contraction counts two ("don't")."""
    return len(_WORD.findall(textcheck.normal_form(line)))


def host_words(script: dict) -> int:
    """The words the host speaks: line_words over every host line."""
    return sum(line_words(line) for line in host_lines(script))


def code_shapes(text: str) -> list:
    """The kinds of code in text, read in its normal form (a fullwidth backtick is a backtick), sorted: a backtick, code
    marks ({, }, =>, ();), a call shape (word followed by an opening parenthesis), a URL or a path (path_or_url, the
    ledger's one path rule), a line starting with $ or >, or a command."""
    clean = textcheck.normal_form(text)
    found = {kind for kind, mark in _CODE_MARKS if mark in clean}
    if _CALL.search(clean):
        found.add("call")
    if path_or_url(text):
        found.add("path_or_url")
    if any(line.lstrip()[:1] in ("$", ">") for line in clean.splitlines()):
        found.add("prompt_line")
    if _COMMAND.search(clean):
        found.add("command")
    return sorted(found)


class _NoMatch(Exception):
    """quote_verbatim fails; args[0] is a short code, never quote text."""


class _DuplicateKey(ValueError):
    """A JSON object repeats a key: parsers disagree on which copy wins, so the text proves nothing."""


def _no_duplicates(pairs: list) -> dict:
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise _DuplicateKey("duplicate_key")
        obj[key] = value
    return obj


def source_roots(work_dir=None, settings=None) -> tuple:
    """Where a quoted source may live: the Claude projects root, Codex's sessions and archived_sessions folders (the
    settings' paths, the readers' own defaults without settings), and the run's work directory (sources/github.jsonl)
    when the caller passes it."""
    claude_root = getattr(settings, "claude_projects", None) or paths.DEFAULT_CLAUDE_PROJECTS
    codex = Path(getattr(settings, "codex_db", None) or paths.DEFAULT_CODEX_DB).expanduser().parent
    roots = [Path(claude_root).expanduser(), codex / "sessions", codex / "archived_sessions"]
    return tuple(map(Path, roots + ([work_dir] if work_dir is not None else [])))


def _inside(path: str, roots) -> bool:
    """Whether path, every link in it resolved, sits under one of the roots (each resolved too)."""
    real = os.path.realpath(path)
    for root in roots:
        top = os.path.realpath(root)
        if os.path.commonpath([real, top]) == top:
            return True
    return False


def _open_source(path: str, roots):
    """The source as a text file, read like the readers read it. Only a regular file is opened: a link (it could
    point anywhere) or anything else (a pipe would block the open) is refused from lstat, before any open, and the
    open itself refuses a link (O_NOFOLLOW) and cannot block (O_NONBLOCK), so a swap after the lstat changes nothing."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        raise _NoMatch("source_missing") from None
    except OSError:
        raise _NoMatch("source_unreadable") from None
    if stat.S_ISLNK(mode):
        raise _NoMatch("source_link")
    if not stat.S_ISREG(mode):
        raise _NoMatch("source_not_file")
    if not _inside(path, roots):
        raise _NoMatch("outside_roots")  # a linked parent folder can lead anywhere
    try:
        fd = os.open(os.path.realpath(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise _NoMatch("source_missing") from None
    except OSError as e:
        raise _NoMatch("source_link" if e.errno == errno.ELOOP else "source_unreadable") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _NoMatch("source_not_file")
        return open(fd, encoding="utf-8", errors="replace")
    except BaseException:
        os.close(fd)
        raise


def _record_at(path: str, line_no: int, roots) -> dict:
    """The JSON object on line line_no, numbered as the readers number lines: by iterating the open file (default
    newline handling), never str.splitlines, which also breaks at U+2028, U+2029 and U+0085. A repeated key anywhere
    in the object fails."""
    try:
        with _open_source(path, roots) as fh:
            for n, line in enumerate(fh, 1):
                if n == line_no:
                    break
            else:
                raise _NoMatch("line_out_of_range")
    except OSError:
        raise _NoMatch("source_unreadable") from None
    try:
        record = json.loads(line, object_pairs_hook=_no_duplicates)
    except _DuplicateKey:
        raise _NoMatch("duplicate_key") from None
    except (ValueError, RecursionError):
        raise _NoMatch("bad_json") from None
    if not isinstance(record, dict):
        raise _NoMatch("not_object")
    return record


def _safe_id(v) -> str:
    """A record id as the readers keep it: the id when it is a safe token, else ""."""
    return v if isinstance(v, str) and SAFE_ID.match(v) else ""


def _claude_prose(r: dict, harness: str) -> tuple:
    """(text blocks, record id, harness) of a main-session assistant record, taken as the Claude reader takes it."""
    msg = r.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if r.get("type") != "assistant" or r.get("isSidechain") or not isinstance(content, list):
        raise _NoMatch("not_assistant_prose")
    texts = [b["text"] for b in content
             if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
    return texts, _safe_id(r.get("uuid")), harness


def _codex_prose(r: dict, harness: str) -> tuple:
    """(output_text blocks, payload id, harness) of an assistant message, taken as the Codex reader takes it."""
    p = r.get("payload") if isinstance(r.get("payload"), dict) else {}
    content = p.get("content")
    if (r.get("type") != "response_item" or p.get("type") != "message" or p.get("role") != "assistant"
            or not isinstance(content, list)):
        raise _NoMatch("not_assistant_prose")
    texts = [b["text"] for b in content
             if isinstance(b, dict) and b.get("type") == "output_text" and isinstance(b.get("text"), str)]
    return texts, _safe_id(p.get("id")), harness


def _pr_body(r: dict, harness: str) -> tuple:
    """([body], the record id built from the line, the line's harness) of a sources/github.jsonl PR record."""
    repo, number, body = r.get("repo"), r.get("number"), r.get("body")
    if not isinstance(repo, str) or type(number) is not int or not isinstance(body, str):
        raise _NoMatch("bad_pr_record")
    return [body], f"pr:{repo_name(repo)}#{number}", r.get("harness")


_READERS = {("transcript", "claude_code"): _claude_prose, ("transcript", "codex"): _codex_prose,
            ("pr", "claude_code"): _pr_body, ("pr", "codex"): _pr_body}


def _verbatim(quote: dict, roots) -> tuple:
    """Re-read the quote's source line and prove the quote is on it, in the right record, under the right id."""
    kind, harness, text, source = (quote.get(k) for k in ("kind", "harness", "text", "source"))
    read = _READERS.get((kind, harness)) if isinstance(kind, str) and isinstance(harness, str) else None
    if read is None:
        raise _NoMatch("unknown_kind")
    if not isinstance(text, str) or not text.strip():
        raise _NoMatch("empty_text")  # "" is a substring of every line
    if not isinstance(source, dict):
        raise _NoMatch("bad_source")
    path, line, record = source.get("path"), source.get("line"), source.get("record")
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path or ".." in Path(path).parts:
        raise _NoMatch("bad_path")
    if path.lower().endswith(".zst"):
        raise _NoMatch("zst_source")  # the readers never quote a compressed segment
    if type(line) is not int or line < 1:
        raise _NoMatch("bad_line")
    if not isinstance(record, str):
        raise _NoMatch("bad_record")
    texts, rid, line_harness = read(_record_at(path, line, roots), harness)
    if record != rid:
        raise _NoMatch("record_mismatch")
    if line_harness != harness:
        raise _NoMatch("harness_mismatch")
    if not any(text in t for t in texts):
        raise _NoMatch("text_not_found")
    if quote.get("id") != quote_id(harness, text, path, line):
        raise _NoMatch("id_mismatch")
    return True, f"matched {Path(path).name}:{line}"


def _redactor(deps: Deps, text: str) -> tuple:
    """The redactor on the raw text and on its normal form (textcheck.forms): a hit on either fails."""
    clean, classes = True, set()
    for form in textcheck.forms(text):
        verdict, hits = deps.redactor_decision(form)
        if not isinstance(hits, (list, tuple)):
            raise TypeError("redactor hits are not a list")
        if verdict != "PUBLISH" or hits:
            clean = False
            classes |= {str(hit[0]) for hit in hits}  # the class, never the snippet
    return (True, "PUBLISH") if clean else (False, "ABSTAIN: " + ",".join(sorted(classes)))


def _slop(deps: Deps, text: str) -> tuple:
    hits: list = []
    for form in textcheck.forms(text):
        got = deps.slop_scan(form)
        if not isinstance(got, (list, tuple)):
            raise TypeError("slop hits are not a list")
        hits += got
    return not hits, "clean" if not hits else ",".join(sorted({f"{rule}:{term}" for rule, term in hits}))


def _secrets(deps: Deps, text: str) -> tuple:
    found: list = []
    for form in textcheck.forms(text):
        got = deps.secrets_scan(form)
        if not isinstance(got, (list, tuple)):
            raise TypeError("secret findings are not a list")
        found += got
    if not found:
        return True, "clean"
    kinds = sorted({m.group(1).strip()[:60] if (m := _KIND.match(str(f))) else "finding" for f in found})
    return False, f"{len(found)} finding(s): " + "; ".join(kinds)


def _names(cfg, text: str, ledger: dict) -> tuple:
    """cfg.names_to_block() and the ledger's private_names (every non-public repo name seen that day; S2) matched as
    the ledger matches them (ledger.name_pattern: separators ignored, a word start required) on the normal form, and
    every exclude fragment as a substring. Host and node names are not repo names: the redactor's node list and config
    blocked_names cover them. A private_names that is not a list of strings raises, so the check fails closed. A letter or digit outside ASCII that the
    normal form keeps (accents are stripped, a Cyrillic look-alike is not) fails. Out of scope by decision: leetspeak
    and run-together capitals ("Z3phyr", "MYZEPHYR"), an open vocabulary covered by the writer rules, the ledger
    prefilter and the operator's review."""
    clean = textcheck.normal_form(text)
    if non_ascii(clean):
        return False, "non-ascii letter or digit"  # a homoglyph could carry a blocked name past the matching
    private = ledger.get("private_names", [])
    if not isinstance(private, list) or not all(isinstance(n, str) for n in private):
        raise TypeError("private_names is not a list of names")
    names = (*cfg.names_to_block(), *blockable_private_names(private))
    hits = {n.lower() for n in names if (r := name_pattern(n)) is not None and r.search(clean)}
    hits |= {f for f in exclude_fragments(cfg) if contains_fragment(clean, (f,))}
    return not hits, "clear" if not hits else f"{len(hits)} blocked name(s)"


def _embargo(cfg, text: str) -> tuple:
    hit = cfg.embargo_hit(text)  # the raw text and its normal form, against terms in their normal form
    return not hit, "embargo term present" if hit else "clear"


def _budget(script: dict) -> tuple:
    n = host_words(script)
    return WORDS_MIN <= n <= WORDS_MAX, f"{n} host words (allowed {WORDS_MIN} to {WORDS_MAX})"


def _no_code(text: str) -> tuple:
    shapes = code_shapes(text)
    return not shapes, ",".join(shapes) if shapes else "clear"


def _scoreboard_numbers(script: dict) -> tuple:
    """Any numeral (str.isnumeric: digits, fractions, roman and circled numbers) in the line or its normal form."""
    host = script["scoreboard"]["host"]
    digits = any(ch.isnumeric() for ch in host + textcheck.normal_form(host))
    return not digits, "digits present" if digits else "no digits"


def number_tokens(text: str) -> set:
    """The numbers in text's normal form (textcheck.normal_form: a fullwidth or superscript digit reads as its ASCII
    digit, and an invisible character cannot split a number): each run of decimal digits in any script, with an
    optional decimal part and an optional percent sign ("75%", "2.5", "2026")."""
    return set(_NUMBER.findall(textcheck.normal_form(text)))


def _counts(value):
    """Every integer in a scoreboard value, nested dicts and lists included (a boolean is not a count)."""
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        for v in value:
            yield from _counts(v)
    elif type(value) is int:
        yield value


def ledger_numbers(ledger: dict) -> set:
    """The numbers a script may show or say: those in the text of writer_view(ledger), which is all the writer sees
    (item titles, bodies, labels and whys, quote texts, session and teaser titles), and every scoreboard count. A
    percentage there also grounds its bare number ("75%" grounds "75"); a bare number never grounds a percentage."""
    view = writer_view(ledger)
    texts = [item[k] for item in view["items"] for k in ("title", "body", "label", "why") if k in item]
    texts += [q["text"] for q in view["quotes"]]
    texts += [x["title"] for x in view.get("sessions", []) + view.get("teasers", [])]
    found = set().union(*(number_tokens(t) for t in texts if isinstance(t, str)))
    return found | {n[:-1] for n in found if n.endswith("%")} | {str(n) for n in _counts(view.get("scoreboard"))}


def _numbers_grounded(text: str, ledger: dict) -> tuple:
    """Every number in the shipped text (the chosen quote's included) is one the ledger holds. The evidence counts
    the distinct numbers and never names them."""
    shown = number_tokens(text)
    if not shown:
        return True, "no numbers"
    missing = shown - ledger_numbers(ledger)
    return (False, f"ungrounded:{len(missing)}") if missing else (True, f"grounded:{len(shown)}")


def safe_problem(problem: str) -> str:
    """One validate_script problem with any field name the schema does not define replaced by "other": the validator
    echoes an unexpected key as it was written, and gate evidence, a stage reason or a repair prompt must not carry
    script text."""
    kind, sep, names = problem.partition(":")
    if not sep:
        return kind
    return kind + ":" + ",".join(dict.fromkeys(n if n in _KNOWN_FIELDS else "other" for n in names.split(",")))


def _dicts(v) -> list:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _verdict(day: str, script_bytes: bytes, checks: list) -> dict:
    ok = [c["name"] for c in checks] == list(REQUIRED) and all(c["pass"] for c in checks)
    return {"schema": VERDICT_SCHEMA, "day": day, "stage": "gate", "pass": ok,
            "subject_sha256": sha256_bytes(script_bytes), "checks": checks}


def gate(script_bytes: bytes, ledger: dict, cfg, deps: Deps, work_dir=None, settings=None) -> dict:
    """The verdict (verdict.json): every REQUIRED check, in order, each with short evidence. A check passes
    only when it returns True; one that raises fails with `error:<ExcClass>`. After a schema failure nothing else can
    be read safely, so every other check is recorded as failed and not run. A quoted source must resolve under
    source_roots(work_dir, settings): pass the run's work directory, or a PR quote cannot pass. Never raises."""
    checks: list = []
    ledger = ledger if isinstance(ledger, dict) else {}
    day = ledger.get("day") if isinstance(ledger.get("day"), str) else ""

    def add(name, ok, ev):
        checks.append({"name": name, "pass": ok is True, "evidence": str(ev)[:300]})

    def run(name, fn):
        try:
            ok, ev = fn()
        except Exception as e:  # fail closed: a crashing check never passes
            ok, ev = False, f"error:{type(e).__name__}"
        add(name, ok, ev)

    try:
        script = json.loads(script_bytes, object_pairs_hook=_no_duplicates)
        problems = validate_script(script, day)
        ev = "ok" if not problems else "; ".join(safe_problem(p) for p in problems[:5])
    except _DuplicateKey:
        problems, ev = ["duplicate_key"], "duplicate_key"
    except (ValueError, RecursionError):
        problems, ev = ["not_json"], "not_json"
    except Exception as e:
        problems, ev = ["error"], f"error:{type(e).__name__}"
    add("schema", not problems, ev)
    if problems:
        for name in REQUIRED[1:]:
            add(name, False, "not run: schema failed")
        return _verdict(day, script_bytes, checks)

    qid = script["confessional"]["quote_id"]
    matches = [q for q in _dicts(ledger.get("quotes")) if q.get("id") == qid] if qid else []
    quote = matches[0] if len(matches) == 1 else None
    text = shipped_text(script, quote["text"] if quote and isinstance(quote.get("text"), str) else "")

    def story():
        """The writer copies ids from writer_view, so a headline alias resolves too; the evidence is the ledger id."""
        resolved = resolve_view_id(ledger, script["story_item_id"])
        return (True, resolved) if resolved is not None else (False, "not in ledger")

    def quote_in_ledger():
        if qid == "":
            return True, NO_CONFESSIONAL
        if len(matches) == 1:
            return True, "found"
        return False, "duplicate id in ledger" if matches else "not in ledger"

    def quote_verbatim():
        if qid == "":
            return True, NO_CONFESSIONAL
        if quote is None:
            return False, "not run: quote unresolved"
        try:
            return _verbatim(quote, source_roots(work_dir, settings))
        except _NoMatch as e:
            return False, e.args[0]

    run("quiet", lambda: (script["quiet"] is False, "quiet script" if script["quiet"] else "not quiet"))
    run("story_in_ledger", story)
    run("quote_in_ledger", quote_in_ledger)
    run("quote_verbatim", quote_verbatim)
    run("redactor", lambda: _redactor(deps, text))
    run("slop", lambda: _slop(deps, text))
    run("secrets", lambda: _secrets(deps, text))
    run("names", lambda: _names(cfg, text, ledger))
    run("embargo", lambda: _embargo(cfg, text))
    run("word_budget", lambda: _budget(script))
    run("no_code", lambda: _no_code(text))
    run("scoreboard_numbers", lambda: _scoreboard_numbers(script))
    run("numbers_grounded", lambda: _numbers_grounded(text, ledger))
    return _verdict(day, script_bytes, checks)


def accepts(verdict_path, subject_path, required, ledger_path=None) -> tuple:
    """The only way a consumer reads a verdict: (True, "ok"), or (False, a short reason). Both files are re-read and the
    subject's sha256 recomputed; the verdict must carry the schema tag, the subject's hash, every name in `required`
    exactly once and passing (the boolean true, nothing else) and a top-level pass of true. With `ledger_path` (every
    consumer of a text verdict passes the day's ledger.json) that file is re-read too and the verdict's
    `ledger_sha256` must be its hash, so a verdict never vouches for a script beside a different ledger. A repeated
    JSON key anywhere in the verdict is refused, since parsers disagree on which copy wins. Never raises, and the
    reason holds only codes and names from `required`."""
    try:
        return _accepts(verdict_path, subject_path, tuple(required), ledger_path)
    except Exception as e:  # fail closed: a consumer is never handed an exception to mishandle
        return False, f"error:{type(e).__name__}"


def _accepts(verdict_path, subject_path, required: tuple, ledger_path) -> tuple:
    try:
        v = json.loads(Path(verdict_path).read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates)
        data = Path(subject_path).read_bytes()
    except _DuplicateKey:
        return False, "duplicate_key"
    except (OSError, ValueError, RecursionError) as e:
        return False, f"unreadable:{type(e).__name__}"
    ledger_bytes = None
    if ledger_path is not None:
        try:
            ledger_bytes = Path(ledger_path).read_bytes()
        except OSError as e:
            return False, f"ledger_unreadable:{type(e).__name__}"
    return _judge(v, data, ledger_bytes, required, ledger_path is not None)


def _judge(v, data: bytes, ledger_bytes, required: tuple, bind_ledger: bool) -> tuple:
    """The verdict checks, on bytes already read: (True, "ok") or (False, a short reason). The one implementation
    behind `accepts` and `read_verified`."""
    if not isinstance(v, dict) or v.get("schema") != VERDICT_SCHEMA:
        return False, "bad_schema"
    if v.get("subject_sha256") != sha256_bytes(data):
        return False, "sha_mismatch"
    if bind_ledger and v.get("ledger_sha256") != sha256_bytes(ledger_bytes):
        return False, "ledger_mismatch"
    listed = v.get("checks")
    by_name: dict = {}
    for c in listed if isinstance(listed, list) else []:
        if isinstance(c, dict) and isinstance(c.get("name"), str):
            by_name.setdefault(c["name"], []).append(c)
    missing = [n for n in required if n not in by_name]
    if missing:
        return False, "missing:" + ",".join(missing)
    repeated = [n for n in required if len(by_name[n]) > 1]
    if repeated:
        return False, "duplicate_check:" + ",".join(repeated)
    failing = [n for n in required if by_name[n][0].get("pass") is not True]
    if failing:
        return False, "failed:" + ",".join(failing)
    if v.get("pass") is not True:
        return False, "verdict_not_pass"
    return True, "ok"


def read_verified(verdict_path, subject_path, ledger_path, required) -> tuple:
    """The read-once consumer step: (subject bytes, ledger bytes, "ok"), or (None, None, a short reason). Each of the
    three files is read exactly once, and every check `accepts` makes (schema tag, subject hash, ledger hash binding,
    every name in `required` exactly once and passing as the boolean true, top-level pass true, no repeated JSON key)
    is made on those same bytes. A stage parses the returned bytes, never the files, so a file swapped at any moment
    can neither put unchecked text to use nor slip a verdict past the required set. The reason is a code from
    `accepts`. Never raises."""
    try:
        try:
            v = json.loads(Path(verdict_path).read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates)
            subject = Path(subject_path).read_bytes()
            ledger = Path(ledger_path).read_bytes()
        except _DuplicateKey:
            return None, None, "duplicate_key"
        except (OSError, ValueError, RecursionError) as e:
            return None, None, f"unreadable:{type(e).__name__}"
        ok, why = _judge(v, subject, ledger, tuple(required), True)
        return (subject, ledger, "ok") if ok else (None, None, why)
    except Exception as e:  # fail closed: a consumer is never handed an exception to mishandle
        return None, None, f"error:{type(e).__name__}"


def read_verified_full(verdict_path, subject_path, ledger_path, required) -> tuple:
    """The read-once consumer step that also hands back the parsed verdict: (verdict dict, subject bytes, ledger bytes,
    "ok"), or (None, None, None, a short reason). Each file is read exactly once and judged by the same `_judge` that
    `accepts` and `read_verified` use, on those bytes: ledger bound, unless `ledger_path` is None (then, as in
    `accepts`, the ledger is not read or bound and the returned ledger bytes are None). The caller makes any further
    check on the returned verdict dict and uses the returned bytes, so there is no second read of the verdict, the
    subject or the ledger. Never raises."""
    try:
        try:
            v = json.loads(Path(verdict_path).read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates)
            subject = Path(subject_path).read_bytes()
            ledger = Path(ledger_path).read_bytes() if ledger_path is not None else None
        except _DuplicateKey:
            return None, None, None, "duplicate_key"
        except (OSError, ValueError, RecursionError) as e:
            return None, None, None, f"unreadable:{type(e).__name__}"
        ok, why = _judge(v, subject, ledger, tuple(required), ledger_path is not None)
        return (v, subject, ledger, "ok") if ok else (None, None, None, why)
    except Exception as e:  # fail closed
        return None, None, None, f"error:{type(e).__name__}"


def read_verified_render(verdict_path, subject_path, ledger_path, required) -> tuple:
    """`read_verified_full` for a render verdict (episode.mp4 as the subject): (verdict dict, episode bytes, ledger
    bytes, "ok"), or (None, None, None, a short reason). The one judge for a render verdict: the same `_judge`, plus
    the verdict must be a render verdict (stage "render"). Callers: publish (the episode to upload; ledger bound) and
    the render stage's archive step (is the last run worth keeping; `ledger_path` None, because a recollect has
    already replaced the ledger the old render was made from). Never raises."""
    verdict, episode, ledger, why = read_verified_full(verdict_path, subject_path, ledger_path, required)
    if verdict is None:
        return None, None, None, why
    if verdict.get("stage") != "render":
        return None, None, None, "stage"
    return verdict, episode, ledger, "ok"


def _read_ledger(path: Path):
    """(the ledger as an object, the sha256 of the bytes it was parsed from), or None when it cannot be read as one."""
    try:
        data = path.read_bytes()
        ledger = json.loads(data.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return (ledger, sha256_bytes(data)) if isinstance(ledger, dict) else None


def run_stage(ctx, deps: Deps | None = None) -> StageResult:
    """The gate stage: read script.json and ledger.json from the work directory, run the gate with the work directory
    as an allowed source root (a PR quote lives in <work>/sources/github.jsonl), write verdict.json atomically and
    read it back through `accepts`. ok is what `accepts` says of the file on disk, so a verdict that does not read back
    as written, or a script edited meanwhile, fails the stage. The reason is the first failing check's name (a
    REQUIRED name, never script or quote text), a code from `accepts`, or a short code (missing/unreadable, or
    `day_mismatch` when the ledger is not for ctx.day, which nothing is gated against). The verdict also records
    `ledger_sha256`, the hash of the ledger bytes the gate read, which `accepts` binds through `ledger_path` (a ledger
    changed meanwhile fails the stage with `ledger_mismatch`), and `dry` (ctx.dry) and `at` (UTC ISO time).

    An older verdict.json is removed first, so a run that fails before it can write one leaves nothing for a consumer
    to accept. Without settings (ctx.settings None) the stage fails closed (`settings_missing`): the operator's own
    patterns, checks folder, public hosts and log roots would otherwise be silently replaced by the built-in ones."""
    script_path, ledger_path, verdict_path = (ctx.work / n for n in ("script.json", "ledger.json", "verdict.json"))
    verdict_path.unlink(missing_ok=True)
    settings = getattr(ctx, "settings", None)
    if settings is None:
        return StageResult(ok=False, reason="settings_missing")
    for p in (script_path, ledger_path):
        if not p.is_file():
            return StageResult(ok=False, reason=f"missing:{p.name}")
    try:
        script_bytes = script_path.read_bytes()
    except OSError:
        return StageResult(ok=False, reason=f"unreadable:{script_path.name}")
    read = _read_ledger(ledger_path)
    if read is None:
        return StageResult(ok=False, reason=f"unreadable:{ledger_path.name}")
    ledger, ledger_sha256 = read
    if ledger.get("day") != ctx.day.isoformat():
        return StageResult(ok=False, reason="day_mismatch")
    verdict = gate(script_bytes, ledger, ctx.cfg, deps or default_deps(settings), work_dir=ctx.work, settings=settings)
    verdict.update(ledger_sha256=ledger_sha256, dry=bool(ctx.dry), at=now_iso())
    write_json(verdict_path, verdict)
    accepted, why = accepts(verdict_path, script_path, REQUIRED, ledger_path=ledger_path)
    if accepted:
        return StageResult(ok=True)
    return StageResult(ok=False, reason=next((c["name"] for c in verdict["checks"] if not c["pass"]), why))
