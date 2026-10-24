"""The light voice pass's lint: the tells that make a narrated line sound written rather than said. Advisory.

The writer uses it to decide whether a story draft spends its one repair (daily.writer.review), and the voice stage
records what it left before any audio is made (write_record: hostlint.json in the day's folder). Rules:

- "heres": a "here's" opener or reveal;
- "nobody": "nobody" or "no one";
- "writerly": a style word people rarely say out loud (WRITERLY, or settings.toml `writer.avoid_words`);
- "impersonal": a sentence of IMPERSONAL_MIN words or more with no person in it (I, you, we).

The defaults come from measuring one narrator's unscripted speech against model-written scripts: the listed words
never appeared when he talked unscripted, and about 28% of his sentences of six words or more had no person in them
(model scripts: about 79%). Tune `writer.avoid_words` to your own voice.
"""
from __future__ import annotations

import re
from pathlib import Path

from daily import textcheck
from daily.files import write_json

PRONOUNS = frozenset("i i'm i've i'll i'd me my you you're you've you'll you'd your yourself we we're we've we'll "
                     "we'd us our let's guys".split())
WRITERLY = ("clever", "perfect", "granted", "manners", "polite", "rude", "catch", "takeaway", "properly",
            "especially", "disaster", "silly", "picture", "meet")
IMPERSONAL_MIN = 6  # words: a shorter sentence is too short to call impersonal
LINT_RULES = ("heres", "nobody", "writerly", "impersonal")
_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen " \
        "seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def num_words(n: int) -> str:
    """0 to 9999 spelled out the way numbers are said ("two hundred forty", no "and")."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        return _ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + num_words(n % 100))
    if n < 10000:
        return num_words(n // 1000) + " thousand" + ("" if n % 1000 == 0 else " " + num_words(n % 1000))
    return str(n)


def norm_tokens(text: str) -> list:
    """Comparable spoken tokens: lower case, curly apostrophes made straight, hyphens, slashes and dots split words,
    thousands commas dropped, apostrophes kept inside words, and every all-digit token spelled out."""
    text = text.replace(chr(0x2019), "'").replace(chr(0x2018), "'").replace(chr(0x2BC), "'").lower()
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    text = re.sub(r"[-/.]", " ", text)
    out: list = []
    for t in re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text):
        out.extend(num_words(int(t)).split() if t.isdigit() else [t])
    return out


def sentences_of_text(text: str) -> list:
    return [s for s in re.split(r"(?<=[.?!])\s+", text.strip()) if norm_tokens(s)]


class Linter:
    """lint_line with a writerly word list of the operator's choosing."""

    def __init__(self, writerly=None):
        self.writerly = frozenset(w.lower() for w in (WRITERLY if writerly is None else writerly))

    def lint_line(self, text: str) -> list:
        """The tells in one line as (rule, matched text): word tells in reading order, then each person-less
        sentence of IMPERSONAL_MIN words or more."""
        toks = norm_tokens(text)
        out = []
        for i, t in enumerate(toks):
            if t == "here's":
                out.append(("heres", t))
            elif t == "nobody" or (t == "no" and toks[i + 1:i + 2] == ["one"]):
                out.append(("nobody", "nobody" if t == "nobody" else "no one"))
            elif t in self.writerly:
                out.append(("writerly", t))
        for s in sentences_of_text(text):
            st = norm_tokens(s)
            if len(st) >= IMPERSONAL_MIN and not PRONOUNS & set(st):
                out.append(("impersonal", s))
        return out

    def lint(self, lines: list) -> dict:
        """Every tell in the (line id, text) lines, with counts per rule; all open (no tell is kept on purpose)."""
        flags = [{"line": line_id, "rule": rule, "match": match}
                 for line_id, text in lines for rule, match in self.lint_line(text)]
        counts = {rule: sum(f["rule"] == rule for f in flags) for rule in LINT_RULES}
        return {"flags": flags, "counts": counts, "open": len(flags), "kept": 0}


def lint_line(text: str) -> list:
    return Linter().lint_line(text)


# ---- the host-line record (hostlint.json) ----

RECORD_SCHEMA = "daily-build/hostlint/v1"
UNAVAILABLE = ("Host-line lint unavailable: the lint raised, so no line was checked.\n"
               "Advisory: nothing here blocks the build.")


def host_lines(script: dict) -> list:
    """(line id, text) of every host line, in episode order; the ids are the voice stage's. A confessional with no
    quote has an empty host_intro, and an empty line is no line."""
    out = [("cold_open", script["cold_open"]["host"]), ("scoreboard", script["scoreboard"]["host"])]
    out += [(f"story_{i}", t) for i, t in enumerate(script["story"]["host"])]
    out += [("steal_this", script["steal_this"]["host"]), ("confessional_intro", script["confessional"]["host_intro"]),
            ("out", script["out"]["host"])]
    return [(i, t) for i, t in out if t.strip()]


def open_count(rec: dict):
    """The record's open tells, or None when the lint was unavailable or the count is not a whole number. The voice
    stage reads the record through here and never indexes rec["result"], which is None in that case."""
    result = rec.get("result") if isinstance(rec, dict) else None
    n = result.get("open") if isinstance(result, dict) else None
    return n if isinstance(n, int) and not isinstance(n, bool) else None


def report(lines: list, res: dict) -> str:
    """A few plain lines for a person: the counts per rule, then each tell as (line id, rule, match)."""
    head = (f"Host-line lint: {res['open']} open tell(s) in {len(lines)} host line(s); "
            + ", ".join(f"{rule} {n}" for rule, n in res["counts"].items()) + ".")
    body = [f"  {f['line']}: {f['rule']}: {f['match']}" for f in res["flags"]]
    return "\n".join([head, *body, "Advisory: nothing here blocks the build."])


def write_record(script: dict, out_path, avoid=None) -> dict:
    """Lint the script's host lines (each read in its normal form, as the writer reads it, so an invisible character
    cannot hide a tell), write the record to out_path atomically and return it. avoid: the operator's writerly words
    (settings.toml `writer.avoid_words`; None for the built-in list). The record's lint is "ok" with the result
    (flags, counts, open, kept, lines) and a report, or "unavailable" with no result and the reason "run" when the
    lint raised. A reason is a code, never the exception's text."""
    lines = [(i, textcheck.normal_form(t)) for i, t in host_lines(script)]
    try:
        res = Linter(avoid).lint(lines)
        res["lines"] = len(lines)
        record = {"schema": RECORD_SCHEMA, "advisory": True, "lint": "ok", "reason": None, "result": res,
                  "report": report(lines, res)}
    except Exception:  # advisory: the record says so
        record = {"schema": RECORD_SCHEMA, "advisory": True, "lint": "unavailable", "reason": "run", "result": None,
                  "report": UNAVAILABLE}
    write_json(Path(out_path), record)
    return record
