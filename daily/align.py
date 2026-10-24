"""Word timing for captions. Whisper's timed words are mapped onto the line's OWN words, so captions never show
recognizer spelling; words Whisper missed are interpolated between their neighbours; coverage is the share of the
line's words Whisper actually heard (a line under MIN_COVERAGE fails the voice stage).

The matching follows a word-level script-to-recognizer alignment (an ordered, spelling-insensitive match that maximizes
matched words) and a Whisper glossary prompt. Standard library only, so whisper_words.py can import it from the
faster-whisper environment (settings.toml `whisper.python`)."""
from __future__ import annotations

import functools
import json
import math
import os
import re
import subprocess
import unicodedata
from pathlib import Path

MIN_COVERAGE = 0.6
MAX_JOIN = 12  # "$1,200" is five spoken words; the longest figure we normalize ("$999,999") is ten
MIN_STEP = 0.05
MIN_WIDTH = 0.01
MAX_WORD = 24
WHISPER = Path(__file__).resolve().parent / "whisper_words.py"
WHISPER_TIMEOUT_S = 1800
MODEL_ENV = "ORION_CLOCK_WHISPER_MODEL"  # whisper_words.py reads the model name from here


ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen").split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
ORDINAL_STEMS = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
                 "nine": "ninth", "twelve": "twelfth"}
# A figure: optional $, whole part (digits, or digit groups with commas), optional decimals, optional % or ordinal
# suffix. It must stand alone, so "v2", "3x" and "R2D2" are not figures.
FIGURE = re.compile(r"(?<![\w.])(\$)?([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.([0-9]+))?(%|st|nd|rd|th)?(?!\w)")


def _plain(word: str) -> str:
    s = unicodedata.normalize("NFD", word)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def key(word: str) -> str:
    return "".join(c for c in _plain(word) if c.isalnum())


def _cardinal(n: int) -> str:
    """0 to 999999 in words."""
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + (f" {ONES[n % 10]}" if n % 10 else "")
    if n < 1000:
        return f"{ONES[n // 100]} hundred" + (f" {_cardinal(n % 100)}" if n % 100 else "")
    return f"{_cardinal(n // 1000)} thousand" + (f" {_cardinal(n % 1000)}" if n % 1000 else "")


def _ordinal(words: str) -> str:
    head, _, last = words.rpartition(" ")
    last = ORDINAL_STEMS.get(last) or (last[:-1] + "ieth" if last.endswith("y") else last + "th")
    return f"{head} {last}" if head else last


def _year(n: int) -> str | None:
    """A year read as two pairs: 2026 as "twenty twenty six", 1999 as "nineteen ninety nine", 1905 as "nineteen oh
    five", 1900 as "nineteen hundred". 1100 to 2099 only; 2000 to 2009 are always "two thousand ..."."""
    if not 1100 <= n <= 2099 or 2000 <= n <= 2009:
        return None
    hi, lo = divmod(n, 100)
    return f"{_cardinal(hi)} " + ("hundred" if lo == 0 else f"oh {ONES[lo]}" if lo < 10 else _cardinal(lo))


def _say(m: re.Match, units: bool, year: bool = False) -> str:
    """One figure as the words a voice says (`year`: a bare 4-digit figure as a year). Anything we cannot read with
    confidence (a million or more, zero padded, a dollar amount with cents, decimals with an ordinal) is left as
    written, digits and all."""
    dollar, whole, frac, suffix = m.groups()
    digits = whole.replace(",", "")
    if len(digits) > 6 or (len(digits) > 1 and digits[0] == "0"):
        return m.group(0)
    if (frac and (dollar or suffix in ("st", "nd", "rd", "th"))) or (dollar and suffix):
        return m.group(0)
    n = int(digits)
    if year and not (dollar or frac or suffix or "," in whole) and (reading := _year(n)):
        return reading
    words = _cardinal(n)
    if frac:
        words += " point " + " ".join(ONES[int(c)] for c in frac)
    if suffix in ("st", "nd", "rd", "th"):
        return _ordinal(words)
    if units and dollar:
        return words + (" dollar" if n == 1 else " dollars")
    if units and suffix == "%":
        return words + " percent"
    return words


@functools.lru_cache(maxsize=8192)
def spoken_forms(word: str) -> tuple[str, ...]:
    """The keys a word can be matched by, best first. Digits are turned into the words a voice says, on BOTH sides
    of a match (the script carries raw digits and the TTS speaks them, but Whisper may write "75%" or "seventy five
    percent"): "75%" is "seventyfivepercent", "$12" is "twelvedollars", "3rd" is "third", "1,200" is
    "onethousandtwohundred". A percent or dollar amount is also matched by its bare number ("75" heard for "75%"),
    and a year by its year reading ("2026" for "twenty twenty six"), after the cardinal one. Accents, case and
    punctuation do not matter. Anything left with a digit in it could not be normalized."""
    s = _plain(word)

    def render(units: bool, year: bool = False) -> str:
        t = FIGURE.sub(lambda m: _say(m, units, year), s)
        return "".join(c for c in (t.replace("%", " percent ") if units else t) if c.isalnum())

    forms = [render(True)]
    for alt in (render(False), render(True, True)):
        if alt and alt not in forms:
            forms.append(alt)
    return tuple(forms)


def spoken(word: str) -> str:
    return spoken_forms(word)[0]


def glossary(script: str, limit: int = 220) -> str | None:
    """Distinct words of 4 to 24 characters, sorted, kept while they fit in `limit` UTF-8 bytes. The script's first
    word is left out: Whisper reads the prompt as speech just before the audio and would skip that word. The line
    itself is never the prompt (Whisper skips audio that repeats a prompt)."""
    script = unicodedata.normalize("NFC", script)
    eligible, seen = [], set()
    for w in re.findall(r"[^\W\d_](?:[\w'-]*[^\W_])?", script):
        if 4 <= len(w) <= MAX_WORD and w.lower() not in seen:
            seen.add(w.lower())
            eligible.append(w)
    words = sorted(eligible[1:], key=str.lower)
    # Keep the "Glossary:" label: captions use the script's words, so a hallucinated label is simply skipped, while a
    # bare word list could be hallucinated as matching words and inflate coverage on a silent take.
    label = "Terms: " if eligible and eligible[0].lower() == "glossary" else "Glossary: "
    items, size = [], len(label.encode("utf-8"))
    for w in words:
        add = len(w.encode("utf-8")) + (2 if items else 0)
        if size + add > limit:
            continue
        items.append(w)
        size += add
    return label + ", ".join(items) if items else None


def match_tokens(text: str, timed: list[dict]) -> tuple[list[str], list[tuple[int, int, int, int]]]:
    """Ordered, spelling-insensitive match of the line's words against timed words, maximizing matched words. One
    line word may cover 1 to MAX_JOIN joined timed words ("75%" for seventy, five, percent), and one timed word may
    cover 2 to MAX_JOIN joined line words ("75%" heard for seventy, five, percent). Words are compared by their
    spoken keys (see spoken_forms). A contraction the voice reads as written but Whisper hears as two words ("it's"
    as "it is") is a known miss: it costs coverage, never caption text. Returns the line's tokens and links (token
    index, timed index, timed words used, line tokens merged)."""
    tokens = text.split()
    cand = [(i, spoken_forms(t)) for i, t in enumerate(tokens) if spoken(t)]
    tkeys = [spoken(t["word"]) for t in timed]
    n, m = len(cand), len(timed)

    def join_len(ci: int, j: int) -> int:
        for form in cand[ci][1]:
            acc = ""
            for ln in range(1, MAX_JOIN + 1):
                if j + ln > m:
                    break
                acc += tkeys[j + ln - 1]
                if len(acc) > len(form):
                    break
                if acc == form:
                    return ln
        return 0

    def merge_len(ci: int, j: int) -> int:
        target, acc = tkeys[j], cand[ci][1][0]
        for ln in range(2, MAX_JOIN + 1):
            if ci + ln > n:
                break
            acc += cand[ci + ln - 1][1][0]
            if len(acc) > len(target):
                return 0
            if acc == target:
                return ln
        return 0

    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for ci in range(n - 1, -1, -1):
        for j in range(m, -1, -1):
            best = dp[ci + 1][j]
            if j < m:
                best = max(best, dp[ci][j + 1])
                ln = join_len(ci, j)
                if ln:
                    best = max(best, 1 + dp[ci + 1][j + ln])
                mg = merge_len(ci, j)
                if mg:
                    best = max(best, mg + dp[ci + mg][j + 1])
            dp[ci][j] = best
    links = []
    ci = j = 0
    while ci < n and j < m:
        here = dp[ci][j]
        ln, mg = join_len(ci, j), merge_len(ci, j)
        if ln and 1 + dp[ci + 1][j + ln] == here:
            links.append((cand[ci][0], j, ln, 1))
            ci, j = ci + 1, j + ln
        elif mg and mg + dp[ci + mg][j + 1] == here:
            links.append((cand[ci][0], j, 1, mg))
            ci, j = ci + mg, j + 1
        elif dp[ci + 1][j] == here:
            ci += 1
        else:
            j += 1
    return tokens, links


def align_words(text: str, timed: list[dict], duration: float) -> tuple[list[dict], float]:
    """The line's own words with times (seconds into the line's audio) and the coverage (0 to 1).

    Coverage is the share of the line's words that were heard. A word that still has a digit in its spoken key could
    not be normalized ("3x", "v2", a million or more), so we cannot know how the voice read it or how Whisper wrote
    it: when such a word is NOT heard it leaves the denominator instead of failing the line, but at least half of
    the line's words always stay in it, so a mostly unheard line cannot pass on its exemptions. Every other unheard
    word, numbers we can read included, counts against it. With no words to measure, coverage is 0.

    Times never go backwards, every word keeps MIN_WIDTH, and with a known duration (> 0) nothing runs past the end
    of the audio: words heard past it are pushed back from the end, each keeping its width."""
    tokens, links = match_tokens(text, timed)
    times: list[list[float] | None] = [None] * len(tokens)
    skeys = [spoken(t) for t in tokens]
    countable = [i for i, k in enumerate(skeys) if k]
    for tok, j, ln, merged in links:
        if merged == 1:
            times[tok] = [timed[j]["start"], timed[j + ln - 1]["end"]]
            continue
        start, end = timed[j]["start"], timed[j]["end"]
        total = len(spoken(timed[j]["word"])) or 1
        used = 0
        for q in [q for q in countable if q >= tok][:merged]:
            share = len(skeys[q])
            times[q] = [start + (end - start) * used / total, start + (end - start) * (used + share) / total]
            used += share
    heard = sum(1 for i in countable if times[i])
    exempt = sum(1 for i in countable if not times[i] and any(c.isdigit() for c in skeys[i]))
    kept = max(len(countable) - exempt, math.ceil(len(countable) / 2))
    coverage = heard / kept if countable else 0.0
    i = 0
    while i < len(tokens):
        if times[i]:
            i += 1
            continue
        k = i
        while k < len(tokens) and not times[k]:
            k += 1
        frm = times[i - 1][1] if i > 0 else 0.0
        to = times[k][0] if k < len(tokens) else duration
        step = max((to - frm) / (k - i), MIN_STEP)
        for q in range(i, k):
            times[q] = [frm + step * (q - i), frm + step * (q - i + 1)]
        i = k
    prev = 0.0
    for t in times:
        t[0] = max(t[0], prev)
        t[1] = max(t[1], t[0] + MIN_WIDTH)
        prev = t[1]
    if duration > 0:
        limit = duration
        for t in reversed(times):
            t[1] = min(t[1], limit)
            t[0] = min(t[0], t[1] - MIN_WIDTH)
            limit = t[0]
        if times and times[0][0] < 0:  # the audio is shorter than the minimum widths: share it out evenly
            step = duration / len(times)
            times = [[step * q, step * (q + 1)] for q in range(len(times))]
    words = [{"word": tok, "start": round(t[0], 3), "end": round(t[1], 3)} for tok, t in zip(tokens, times)]
    return words, round(coverage, 3)


def run_whisper(jobs: list[dict], manifest: Path, python, model: str | None = None, log: Path | None = None) -> None:
    """jobs: [{"audio", "text", "out"}]. One faster-whisper run (`python`, the interpreter of the environment
    faster-whisper is installed in; `model`, the Whisper model name, default small.en) writes every out. Each old out
    is deleted first and each new one must exist afterwards, so a failed run never leaves a stale take to be read.
    The run has no stdin, and its stdout and stderr stream into `log` (default whisper.log beside the manifest) as it
    runs, so a failure or a timeout leaves what it printed. The error raised on a failure names no path or log text."""
    for job in jobs:
        Path(job["out"]).unlink(missing_ok=True)
    manifest = Path(manifest)
    log = Path(log) if log is not None else manifest.with_name("whisper.log")
    manifest.write_text(json.dumps(jobs, indent=1), encoding="utf-8")
    env = dict(os.environ)
    if model:
        env[MODEL_ENV] = model
    with open(log, "w", encoding="utf-8", errors="replace") as fh:
        r = subprocess.run([str(python), str(WHISPER), str(manifest)], stdout=fh, stderr=subprocess.STDOUT,
                           stdin=subprocess.DEVNULL, env=env, timeout=WHISPER_TIMEOUT_S)
    text = log.read_text(encoding="utf-8", errors="replace")
    if r.returncode != 0 or "WHISPER_OK" not in text:
        raise RuntimeError(f"whisper failed (exit {r.returncode}); see whisper.log")
    missing = [j for j in jobs if not Path(j["out"]).is_file()]
    if missing:
        raise RuntimeError(f"whisper wrote no word file for {len(missing)} line(s)")
