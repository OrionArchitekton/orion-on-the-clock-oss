"""The text gate: fourteen required checks, each with a failing case and a passing control. Quotes are proven by
re-reading real fixture files (a Claude transcript, a Codex rollout, the collect stage's github.jsonl) the way the
readers read them. A scanner that crashes, cannot be loaded or answers with anything but a list fails closed, and no
evidence string ever holds quote text, a blocked name or a secret."""
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from daily import files, paths
from daily import gate as G
from daily.config import Lane
from daily.ledger import RawSources, build_ledger, quote_id
from daily.sources import claude, codex
from daily.sources.github import PR
from daily_helpers import (DISCLOSURE, QUOTE, good_script, make_world, no_confessional_script, quiet_script, words,
                           write_jsonl)

DAY = dt.date(2026, 10, 16)
OK = G.Deps(lambda t: ("PUBLISH", []), lambda t: [], lambda t: [])
# Fakes that flag one plain trigger word each, so one script edit fails exactly one scanner check.
TRIGGERS = G.Deps(lambda t: ("ABSTAIN", [("server", "ro***ud")]) if "rosebud" in t else ("PUBLISH", []),
                  lambda t: [("ai_slop_lexeme", "seamless")] if "seamless" in t else [],
                  lambda t: ["GitHub token (value withheld)"] if "hunter" in t else [])


@pytest.fixture(autouse=True)
def fixtures_are_a_source_root(tmp_path, monkeypatch):
    """Quoted sources must sit under an allowed root; every fixture file here is written under tmp_path."""
    monkeypatch.setattr(paths, "DEFAULT_CLAUDE_PROJECTS", tmp_path)


def run_gate(script, ledger, cfg, deps=OK):
    return G.gate(json.dumps(script).encode(), ledger, cfg, deps)


def failed(v):
    return [c["name"] for c in v["checks"] if not c["pass"]]


def evidence(v, name):
    return next(c["evidence"] for c in v["checks"] if c["name"] == name)


def put(script, dotted, value):
    head, _, tail = dotted.partition(".")
    if tail:
        script[head][tail] = value
    else:
        script[head] = value


def grounded(ledger, *texts):
    """The ledger with texts as its item's body, which the writer sees, so numbers in them are grounded
    (numbers_grounded) and a test aimed at another check still isolates that check."""
    ledger["items"][0]["body"] = "\n".join(texts)
    return ledger


def get(script, dotted):
    head, _, tail = dotted.partition(".")
    return script[head][tail] if tail else script[head]


# ---- the whole verdict ----

def test_clean_script_passes_every_required_check(tmp_path, cfg):  # positive control
    _, qid, ledger = make_world(tmp_path)
    script = good_script(qid)
    v = run_gate(script, ledger, cfg)
    assert v["pass"] is True and failed(v) == []
    assert [c["name"] for c in v["checks"]] == list(G.REQUIRED)
    assert v["schema"] == "daily-build/verdict/v1" and v["stage"] == "gate" and v["day"] == "2026-10-16"
    assert v["subject_sha256"] == hashlib.sha256(json.dumps(script).encode()).hexdigest()
    assert evidence(v, "quote_verbatim") == "matched s.jsonl:2"
    assert evidence(v, "word_budget") == "195 host words (allowed 165 to 210)"


def test_required_is_the_fourteen_checks_in_order():
    assert G.REQUIRED == ("schema", "quiet", "story_in_ledger", "quote_in_ledger", "quote_verbatim", "redactor",
                          "slop", "secrets", "names", "embargo", "word_budget", "no_code", "scoreboard_numbers",
                          "numbers_grounded")
    assert G.RENDER_REQUIRED == ("duration", "format", "loudness", "audio_present", "captions_match")


def _paraphrase(w):
    w["src"].write_text(w["src"].read_text().replace("every single test", "every test"))


# (check, one edit to a world that passes all 14, the checks that edit fails). A bad shape stops every other check,
# and a quote id the ledger lacks leaves nothing to re-read, so those two edits fail more than their own check.
ALONE = [
    ("schema", lambda w: put(w["script"], "title", "x" * 80), G.REQUIRED),
    ("quiet", lambda w: (put(w["script"], "quiet", True), put(w["script"], "quiet_reason", "x")), ("quiet",)),
    ("story_in_ledger", lambda w: put(w["script"], "story_item_id", "pr:demo-tool#99"), ("story_in_ledger",)),
    ("quote_in_ledger", lambda w: put(w["script"], "confessional.quote_id", "q:claude_code:ffffffff"),
     ("quote_in_ledger", "quote_verbatim")),
    ("quote_verbatim", _paraphrase, ("quote_verbatim",)),
    ("redactor", lambda w: put(w["script"], "description", "A rosebud day in the build. " + DISCLOSURE),
     ("redactor",)),
    ("slop", lambda w: put(w["script"], "story.host", [words(44) + " seamless", words(45), words(45)]), ("slop",)),
    ("secrets", lambda w: put(w["script"], "out.host", words(14) + " hunter"), ("secrets",)),
    ("names", lambda w: put(w["script"], "cold_open.on_screen", "Zephyr rules"), ("names",)),
    ("embargo", lambda w: put(w["script"], "story.host", ["we rehearsed the tree talk " + words(40), words(45),
                                                          words(45)]), ("embargo",)),
    ("word_budget", lambda w: put(w["script"], "story.host", [words(5)]), ("word_budget",)),
    ("no_code", lambda w: put(w["script"], "steal_this.on_screen", "Steal this: one `log` per run"), ("no_code",)),
    ("scoreboard_numbers", lambda w: (put(w["script"], "scoreboard.host", words(11) + " 3"),
                                      w["ledger"].update(scoreboard={"prs_merged": 3})), ("scoreboard_numbers",)),
    ("numbers_grounded", lambda w: put(w["script"], "story.on_screen", ["90% faster checks"]), ("numbers_grounded",)),
]


def test_the_alone_table_covers_every_required_check():
    assert [name for name, _, _ in ALONE] == list(G.REQUIRED)


@pytest.mark.parametrize("name,mutate,expected", ALONE, ids=[a[0] for a in ALONE])
def test_each_check_fails_alone(tmp_path, cfg, name, mutate, expected):
    src, qid, ledger = make_world(tmp_path)
    w = {"script": good_script(qid), "ledger": ledger, "src": src}
    assert failed(run_gate(w["script"], ledger, cfg, TRIGGERS)) == []  # positive control: the same world passes
    mutate(w)
    v = run_gate(w["script"], w["ledger"], cfg, TRIGGERS)
    assert v["pass"] is False and failed(v) == list(expected)
    assert QUOTE not in json.dumps(v)


# ---- schema ----

def test_a_schema_failure_still_lists_every_check(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, title="x" * 80), ledger, cfg)
    assert v["pass"] is False and failed(v) == list(G.REQUIRED)
    assert evidence(v, "schema") == "too_long:title"
    assert {c["evidence"] for c in v["checks"][1:]} == {"not run: schema failed"}


def test_day_mismatch_fails_the_schema_check(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, day="2026-10-15"), ledger, cfg)
    assert "schema" in failed(v) and evidence(v, "schema") == "day_mismatch"


def test_schema_evidence_never_echoes_an_unknown_key(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    token = "ghp_" + "a" * 36
    script = good_script(qid)
    script[token] = "x"
    v = run_gate(script, ledger, cfg)
    assert failed(v) == list(G.REQUIRED) and evidence(v, "schema") == "keys:other" and token not in json.dumps(v)


@pytest.mark.parametrize("raw", [b"\xff\xfe not json", b"[" * 100000, b"[]", b"null"],
                         ids=["bad-utf8", "deep", "array", "null"])
def test_bytes_that_are_not_a_script_fail_the_schema_check_without_raising(tmp_path, cfg, raw):
    _, _, ledger = make_world(tmp_path)
    v = G.gate(raw, ledger, cfg, OK)
    assert v["pass"] is False and failed(v) == list(G.REQUIRED)


@pytest.mark.parametrize("ledger", [None, [], {"day": "2026-10-16", "items": "x", "quotes": [1, None], "sessions": 7},
                                    {"day": 20261016}],
                         ids=["none", "list", "bad-fields", "day-not-text"])
def test_a_malformed_ledger_fails_without_raising(cfg, ledger):
    v = G.gate(json.dumps(good_script()).encode(), ledger, cfg, OK)
    assert v["pass"] is False and [c["name"] for c in v["checks"]] == list(G.REQUIRED)


# ---- quiet ----

def test_a_quiet_script_never_passes(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, quiet=True, quiet_reason="x"), ledger, cfg)
    assert failed(v) == ["quiet"] and v["pass"] is False


def test_an_empty_quiet_script_fails_too(tmp_path, cfg):
    _, _, ledger = make_world(tmp_path)
    v = run_gate(quiet_script(), ledger, cfg)
    assert v["pass"] is False and "quiet" in failed(v)


# ---- story_in_ledger ----

def test_a_session_note_can_carry_the_story(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, story_item_id="session:0a1b2c3d"), ledger, cfg)
    assert failed(v) == [] and evidence(v, "story_in_ledger") == "session:0a1b2c3d"
    assert evidence(run_gate(good_script(qid), ledger, cfg), "story_in_ledger") == "pr:demo-tool#7"


def headline_world(tmp_path):
    src, qid, ledger = make_world(tmp_path)
    ledger["items"].append({"id": "lane:backup-box", "kind": "headline", "lane": "backup-box", "disclosure": "headline",
                            "harness": "unknown", "label": "the offsite backup system", "why": "so one dead disk",
                            "count": 2})
    return qid, ledger


def test_a_headline_story_written_with_its_view_alias_passes(tmp_path, cfg):
    """The writer sees a private lane only as lane:private-N (writer_view) and copies that id."""
    qid, ledger = headline_world(tmp_path)
    v = run_gate(good_script(qid, story_item_id="lane:private-1"), ledger, cfg)
    assert failed(v) == [] and evidence(v, "story_in_ledger") == "lane:backup-box"


def test_a_raw_ledger_id_still_passes(tmp_path, cfg):  # positive control
    qid, ledger = headline_world(tmp_path)
    v = run_gate(good_script(qid, story_item_id="lane:backup-box"), ledger, cfg)
    assert failed(v) == [] and evidence(v, "story_in_ledger") == "lane:backup-box"


def test_an_unknown_view_alias_fails(tmp_path, cfg):
    qid, ledger = headline_world(tmp_path)
    v = run_gate(good_script(qid, story_item_id="lane:private-2"), ledger, cfg)
    assert failed(v) == ["story_in_ledger"] and evidence(v, "story_in_ledger") == "not in ledger"


@pytest.mark.parametrize("story", ["pr:demo-tool#99", "session:ffffffff", "session:", "session:pr:demo-tool#7",
                                   "0a1b2c3d", "lane:backup-box"])
def test_a_story_the_ledger_lacks_fails_without_echoing_it(tmp_path, cfg, story):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, story_item_id=story), ledger, cfg)
    assert failed(v) == ["story_in_ledger"] and evidence(v, "story_in_ledger") == "not in ledger"


# ---- quote_in_ledger ----

def test_no_confessional_passes_both_quote_checks(tmp_path, cfg):
    _, _, ledger = make_world(tmp_path)
    for led in (ledger, {**ledger, "quotes": []}):
        v = run_gate(no_confessional_script(), led, cfg)
        assert failed(v) == []
        assert evidence(v, "quote_in_ledger") == evidence(v, "quote_verbatim") == "no confessional"


def test_quote_must_be_in_the_ledger(tmp_path, cfg):
    _, _, ledger = make_world(tmp_path)
    v = run_gate(good_script("q:claude_code:ffffffff"), ledger, cfg)
    assert failed(v) == ["quote_in_ledger", "quote_verbatim"]
    assert evidence(v, "quote_in_ledger") == "not in ledger"
    assert evidence(v, "quote_verbatim") == "not run: quote unresolved"


def test_a_quote_id_held_twice_is_ambiguous(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    ledger["quotes"].append({**ledger["quotes"][0], "text": "Another sentence of six words here."})
    v = run_gate(good_script(qid), ledger, cfg)
    assert failed(v) == ["quote_in_ledger", "quote_verbatim"]
    assert evidence(v, "quote_in_ledger") == "duplicate id in ledger"


def test_the_quote_text_is_scanned_with_the_script(tmp_path, cfg):
    """The quote ships on screen and in audio, so every scanner sees it."""
    seen = []

    def spy(name, answer):
        return lambda t: (seen.append((name, t)), answer)[1]

    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid), ledger, cfg, G.Deps(spy("r", ("PUBLISH", [])), spy("s", []), spy("x", [])))
    assert failed(v) == [] and [n for n, _ in seen] == ["r", "s", "x"] and all(QUOTE in t for _, t in seen)


@pytest.mark.parametrize("quote,check", [
    ("Zephyr shipped today and every single test passed.", "names"),
    ("The `pool` shipped today and every single test passed.", "no_code"),
    ("We rehearsed the tree talk today and it went well.", "embargo"),
])
def test_a_verbatim_quote_can_still_fail_a_text_check(tmp_path, cfg, quote, check):
    _, qid, ledger = make_world(tmp_path, quote)
    v = run_gate(good_script(qid), ledger, cfg)
    assert failed(v) == [check] and evidence(v, "quote_verbatim") == "matched s.jsonl:2"


# ---- quote_verbatim, from real files ----

class World:
    """One quote of one kind, written to a real source file the way its producer writes it."""

    def __init__(self, src: Path, ledger: dict, line: int):
        self.src, self.ledger, self.line = src, ledger, line
        self.script = good_script(self.quote["id"])

    @property
    def quote(self) -> dict:
        return self.ledger["quotes"][0]

    @property
    def source(self) -> dict:
        return self.quote["source"]

    def rehash(self):
        """Give the quote the id its (edited) fields hash to, so only the edit under test is wrong."""
        q = self.quote
        q["id"] = quote_id(q["harness"], q["text"], q["source"]["path"], q["source"]["line"])
        self.script["confessional"]["quote_id"] = q["id"]

    def gate(self, cfg):
        return run_gate(self.script, self.ledger, cfg)


def _ledger(quote: dict) -> dict:
    return {"schema": "daily-build/ledger/v1", "day": "2026-10-16", "items": [{"id": "pr:demo-tool#7", "kind": "pr"}],
            "sessions": [], "quotes": [quote]}


def claude_world(tmp_path, quote=QUOTE) -> World:
    src, _, ledger = make_world(tmp_path, quote)
    return World(src, ledger, 2)


def codex_world(tmp_path, quote=QUOTE) -> World:
    src = write_jsonl(tmp_path / "rollout-2026-10-16.jsonl", [
        {"timestamp": "2026-10-16T19:59:00Z", "type": "session_meta", "payload": {"id": "th-1", "cwd": "/x/demo"}},
        {"timestamp": "2026-10-16T20:00:00Z", "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Done. " + quote}]}},
        {"timestamp": "2026-10-16T20:00:05Z", "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "id": "msg_1",
            "content": [{"type": "output_text", "text": "Done. " + quote + " Next up."}]}},
    ])
    q = {"id": quote_id("codex", quote, str(src), 3), "kind": "transcript", "harness": "codex", "lane": "console",
         "disclosure": "console", "text": quote, "ts": "2026-10-16T20:00:05Z",
         "source": {"path": str(src), "line": 3, "record": "msg_1"}}
    return World(src, _ledger(q), 3)


def write_pr_records(path: Path, records: list) -> Path:
    """github.jsonl exactly as the collect stage writes it."""
    files.write_text(path, "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))
    return path


def pr_world(tmp_path, quote=QUOTE) -> World:
    src = write_pr_records(tmp_path / "work" / "sources" / "github.jsonl", [
        {"repo": "owner/demo-tool", "number": 6, "harness": "claude_code",
         "closed_at": "2026-10-16T18:00:00Z", "body": "Done. " + quote},
        {"repo": "owner/demo-tool", "number": 7, "harness": "codex", "closed_at": "2026-10-16T21:00:00Z",
         "body": "## Summary\n" + quote + "\nThe rest of it."},
    ])
    q = {"id": quote_id("codex", quote, str(src), 2), "kind": "pr", "harness": "codex", "lane": "demo-tool",
         "disclosure": "public", "text": quote, "ts": "2026-10-16T21:00:00Z",
         "source": {"path": str(src), "line": 2, "record": "pr:demo-tool#7"}}
    return World(src, _ledger(q), 2)


KINDS = {"claude": claude_world, "codex": codex_world, "pr": pr_world}


def assert_verbatim_fails(v, code):
    assert failed(v) == ["quote_verbatim"] and evidence(v, "quote_verbatim") == code
    assert QUOTE not in json.dumps(v)


@pytest.mark.parametrize("kind", KINDS)
def test_each_kind_of_quote_matches_its_source_line(tmp_path, cfg, kind):  # positive control
    w = KINDS[kind](tmp_path)
    v = w.gate(cfg)
    assert failed(v) == [] and evidence(v, "quote_verbatim") == f"matched {w.src.name}:{w.line}"


@pytest.mark.parametrize("kind", KINDS)
def test_a_paraphrased_source_fails(tmp_path, cfg, kind):
    w = KINDS[kind](tmp_path)
    w.src.write_text(w.src.read_text().replace("every single test", "every test"))
    assert_verbatim_fails(w.gate(cfg), "text_not_found")


@pytest.mark.parametrize("kind,line,code", [
    ("claude", 1, "not_assistant_prose"),  # the user record before it
    ("codex", 2, "not_assistant_prose"),  # a user message holding the same text
    ("codex", 1, "not_assistant_prose"),
    ("pr", 1, "record_mismatch"),  # another PR whose body holds the same text
])
def test_a_wrong_line_number_fails(tmp_path, cfg, kind, line, code):
    w = KINDS[kind](tmp_path)
    w.source["line"] = line
    w.rehash()
    assert_verbatim_fails(w.gate(cfg), code)


@pytest.mark.parametrize("kind", KINDS)
def test_a_line_past_the_end_fails(tmp_path, cfg, kind):
    w = KINDS[kind](tmp_path)
    w.source["line"] = 99
    w.rehash()
    assert_verbatim_fails(w.gate(cfg), "line_out_of_range")


@pytest.mark.parametrize("line", [0, -1, "2", True, 2.0, None])
def test_a_line_that_is_not_a_positive_integer_fails(tmp_path, cfg, line):
    w = claude_world(tmp_path)
    w.source["line"] = line
    assert_verbatim_fails(w.gate(cfg), "bad_line")


@pytest.mark.parametrize("kind", KINDS)
def test_a_compressed_source_fails_even_when_its_text_would_match(tmp_path, cfg, kind):
    w = KINDS[kind](tmp_path)
    zst = w.src.with_name(w.src.name + ".zst")
    shutil.copy(w.src, zst)  # the same bytes: only the name says compressed, and the readers never quote from one
    w.source["path"] = str(zst)
    w.rehash()
    assert_verbatim_fails(w.gate(cfg), "zst_source")


@pytest.mark.parametrize("kind", KINDS)
def test_a_missing_source_fails(tmp_path, cfg, kind):
    w = KINDS[kind](tmp_path)
    w.src.unlink()
    assert_verbatim_fails(w.gate(cfg), "source_missing")


def test_a_folder_in_place_of_the_source_fails(tmp_path, cfg):
    w = claude_world(tmp_path)
    w.src.unlink()
    w.src.mkdir()
    assert_verbatim_fails(w.gate(cfg), "source_not_file")


@pytest.mark.parametrize("kind,record", [("claude", "u9"), ("claude", ""), ("codex", "msg_2"), ("codex", ""),
                                         ("pr", "pr:demo-tool#8"), ("pr", "pr:owner/demo-tool#7")])
def test_a_different_record_fails(tmp_path, cfg, kind, record):
    w = KINDS[kind](tmp_path)
    w.source["record"] = record
    assert_verbatim_fails(w.gate(cfg), "record_mismatch")


def test_a_pr_record_from_another_harness_fails(tmp_path, cfg):
    w = pr_world(tmp_path)
    w.quote["harness"] = "claude_code"
    w.rehash()
    assert_verbatim_fails(w.gate(cfg), "harness_mismatch")


@pytest.mark.parametrize("kind", KINDS)
def test_an_edited_quote_fails_on_its_id(tmp_path, cfg, kind):
    """A shorter text that is still on the line, under the id of the full sentence: the id binds text and source."""
    w = KINDS[kind](tmp_path)
    w.quote["text"] = "every single test passed."
    assert_verbatim_fails(w.gate(cfg), "id_mismatch")


@pytest.mark.parametrize("text", ["", "   ", None, 7])
def test_an_empty_quote_text_never_matches(tmp_path, cfg, text):
    w = claude_world(tmp_path)
    w.quote["text"] = text
    assert_verbatim_fails(w.gate(cfg), "empty_text")


@pytest.mark.parametrize("path", ["s.jsonl", "", None, "/tmp/x\0y.jsonl"])
def test_a_source_path_that_is_not_absolute_fails(tmp_path, cfg, path):
    w = claude_world(tmp_path)
    w.source["path"] = path
    assert_verbatim_fails(w.gate(cfg), "bad_path")


@pytest.mark.parametrize("bad", ["{truncated", "[1, 2]", ""], ids=["not-json", "not-object", "blank"])
def test_a_quoted_line_that_is_not_a_record_fails(tmp_path, cfg, bad):
    w = claude_world(tmp_path)
    lines = w.src.read_text().splitlines(keepends=True)
    lines[1] = bad + "\n"
    w.src.write_text("".join(lines))
    assert_verbatim_fails(w.gate(cfg), "not_object" if bad.startswith("[") else "bad_json")


@pytest.mark.parametrize("kind,harness", [("transcript", "alpha_agent"), ("transcript", "beta_agent"), ("arc", "codex"),
                                          (None, "claude_code")])
def test_an_unknown_kind_of_quote_fails(tmp_path, cfg, kind, harness):
    w = claude_world(tmp_path)
    w.quote["kind"], w.quote["harness"] = kind, harness
    w.rehash()
    assert_verbatim_fails(w.gate(cfg), "unknown_kind")


def test_a_sidechain_record_is_not_quotable(tmp_path, cfg):
    w = claude_world(tmp_path)
    lines = w.src.read_text().splitlines(keepends=True)
    rec = json.loads(lines[1])
    rec["isSidechain"] = True
    lines[1] = json.dumps(rec) + "\n"
    w.src.write_text("".join(lines))
    assert_verbatim_fails(w.gate(cfg), "not_assistant_prose")


# ---- quote_verbatim agrees with the readers that produced the quote ----

def _transcript_line(path: Path, records: list):
    # ensure_ascii=False keeps U+2028, U+2029 and U+0085 raw on the line: str.splitlines would end a line at each
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    assert len(path.read_text(encoding="utf-8").splitlines()) > len(records)  # the trap is really there
    return path


BREAKS = "one\u2028two\u2029three\x85four"


def test_a_claude_quote_found_by_the_reader_passes_despite_unicode_line_breaks(tmp_path, cfg):
    path = _transcript_line(tmp_path / "t.jsonl", [
        {"type": "user", "timestamp": "2026-10-16T19:00:00Z", "uuid": "u0",
         "message": {"content": [{"type": "text", "text": BREAKS}]}},
        {"type": "assistant", "timestamp": "2026-10-16T20:00:05Z", "uuid": "u1",
         "message": {"content": [{"type": "text", "text": BREAKS + " Done. " + QUOTE + " Next up."}]}},
    ])
    [(_, text, ts, line, record)] = [f for f in claude._scan_file(str(path), DAY, True).found if f[1] == QUOTE]
    assert (line, record) == (2, "u1")
    q = {"id": quote_id("claude_code", text, str(path), line), "kind": "transcript", "harness": "claude_code",
         "lane": "console", "disclosure": "console", "text": text, "ts": ts,
         "source": {"path": str(path), "line": line, "record": record}}
    v = run_gate(good_script(q["id"]), _ledger(q), cfg)
    assert failed(v) == [] and evidence(v, "quote_verbatim") == "matched t.jsonl:2"


def test_a_codex_quote_found_by_the_reader_passes_despite_unicode_line_breaks(tmp_path, cfg):
    path = _transcript_line(tmp_path / "r.jsonl", [
        {"timestamp": "2026-10-16T19:59:00Z", "type": "session_meta", "payload": {"id": "th-1", "cwd": "/x/demo"}},
        {"timestamp": "2026-10-16T20:00:00Z", "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": BREAKS}]}},
        {"timestamp": "2026-10-16T20:00:05Z", "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "id": "msg_1",
            "content": [{"type": "output_text", "text": BREAKS + " Done. " + QUOTE}]}},
    ])
    found = codex._scan_rollout(str(path), DAY, "/x/demo", True)[1]
    [(_, text, ts, line, record)] = [f for f in found if f[1] == QUOTE]
    assert (line, record) == (3, "msg_1")
    q = {"id": quote_id("codex", text, str(path), line), "kind": "transcript", "harness": "codex", "lane": "console",
         "disclosure": "console", "text": text, "ts": ts, "source": {"path": str(path), "line": line, "record": record}}
    assert failed(run_gate(good_script(q["id"]), _ledger(q), cfg)) == []


def test_a_pr_quote_from_the_ledger_passes_against_the_written_records(tmp_path, cfg):
    src = tmp_path / "work" / "sources" / "github.jsonl"
    prs = (PR(repo="owner/demo-tool", number=7, title="Add the browser pool",
              body="## Summary\nIt landed.\n" + QUOTE, url="https://github.invalid/7",
              closed_at="2026-10-16T21:00:00Z", harness="codex"),)
    led, records = build_ledger(DAY, RawSources(prs=prs, arcs=(), claude=None, codex=None), cfg,
                                "2026-10-17T05:00:00Z", str(src))
    write_pr_records(src, records)
    [q] = [x for x in led["quotes"] if x["text"] == QUOTE]
    v = run_gate(good_script(q["id"]), led, cfg)
    assert failed(v) == [] and evidence(v, "quote_verbatim") == "matched github.jsonl:1"


# ---- redactor, slop, secrets ----

def test_redactor_abstain_fails_and_names_only_the_class(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    deps = G.Deps(lambda t: ("ABSTAIN", [("secret", "gh**xx"), ("host", "he**es")]), lambda t: [], lambda t: [])
    v = run_gate(good_script(qid), ledger, cfg, deps)
    assert failed(v) == ["redactor"] and evidence(v, "redactor") == "ABSTAIN: host,secret"
    assert "gh**xx" not in json.dumps(v)


@pytest.mark.parametrize("answer", [("PUBLISH", [("host", "he**es")]), ("MAYBE", []), ("PUBLISH", None),
                                    ("PUBLISH", "")], ids=["publish-with-hits", "other-verdict", "none", "str"])
def test_a_redactor_answer_that_is_not_a_clean_publish_fails(tmp_path, cfg, answer):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid), ledger, cfg, G.Deps(lambda t: answer, lambda t: [], lambda t: []))
    assert failed(v) == ["redactor"]


def test_slop_hit_fails(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    deps = G.Deps(lambda t: ("PUBLISH", []), lambda t: [("ai_slop_lexeme", "seamless")], lambda t: [])
    v = run_gate(good_script(qid), ledger, cfg, deps)
    assert failed(v) == ["slop"] and evidence(v, "slop") == "ai_slop_lexeme:seamless"


def test_secret_finding_fails_naming_kinds_only(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    found = ["GitHub token (value withheld)", "email-like text 'sam@example.invalid'",
             "home folder path /home/someone/.config/private-app",
             "host not on the public allowlist: https://private.invalid"]
    v = run_gate(good_script(qid), ledger, cfg, G.Deps(lambda t: ("PUBLISH", []), lambda t: [], lambda t: found))
    assert failed(v) == ["secrets"]
    ev = evidence(v, "secrets")
    assert ev.startswith("4 finding(s): ") and "GitHub token" in ev and "email-like text" in ev
    assert not any(s in json.dumps(v) for s in ("sam@example", "/home/someone", "private.invalid"))


@pytest.mark.parametrize("dep", ["redactor_decision", "slop_scan", "secrets_scan"])
def test_a_scanner_answering_none_fails_closed(tmp_path, cfg, dep):
    _, qid, ledger = make_world(tmp_path)
    fns = {"redactor_decision": lambda t: ("PUBLISH", []), "slop_scan": lambda t: [], "secrets_scan": lambda t: []}
    fns[dep] = lambda t: None
    v = run_gate(good_script(qid), ledger, cfg, G.Deps(**fns))
    assert failed(v) == [{"redactor_decision": "redactor", "slop_scan": "slop", "secrets_scan": "secrets"}[dep]]


@pytest.mark.parametrize("dep,check", [("redactor_decision", "redactor"), ("slop_scan", "slop"),
                                       ("secrets_scan", "secrets")])
def test_a_crashing_scanner_fails_closed(tmp_path, cfg, dep, check):
    _, qid, ledger = make_world(tmp_path)

    def boom(text):
        raise RuntimeError("scanner down " + text)

    fns = {"redactor_decision": lambda t: ("PUBLISH", []), "slop_scan": lambda t: [], "secrets_scan": lambda t: []}
    fns[dep] = boom
    v = run_gate(good_script(qid), ledger, cfg, G.Deps(**fns))
    assert failed(v) == [check] and evidence(v, check) == "error:RuntimeError" and QUOTE not in json.dumps(v)


# ---- the scanners, loaded by file location from the checks folder ----

SHADOWED = ("redactor", "slop_gate", "secret_scan")


@pytest.fixture
def clean_modules(monkeypatch):
    """sys.path and the plain module names come back exactly as they were."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    saved = {name: sys.modules.get(name) for name in SHADOWED}
    yield
    for name, mod in saved.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


def fake_checks(folder: Path, secrets: bool = True) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "redactor.py").write_text(
        "def decision(t):\n    return ('ABSTAIN', [('host', 'xx')]) if 'rosebud' in t else ('PUBLISH', [])\n")
    (folder / "slop_gate.py").write_text(
        "def scan(t):\n    return [('ai_slop_lexeme', 'delve')] if 'delve' in t else []\n")
    if secrets:
        (folder / "secret_scan.py").write_text(
            "def scan_output(text, public_hosts=()):\n"
            "    if 'youtube.com' not in public_hosts:\n        return ['wrong public hosts']\n"
            "    return ['GitHub token (value withheld)'] if 'hunter' in text else []\n")
    return folder


def test_default_deps_load_the_folder_modules(tmp_path, cfg, monkeypatch, clean_modules):  # positive control
    monkeypatch.setattr(paths, "CHECKS_DIR", fake_checks(tmp_path / "checks"))
    for name in SHADOWED:
        sys.modules.pop(name, None)
    before = list(sys.path)
    deps = G.default_deps()
    assert sys.path == before and not set(SHADOWED) & set(sys.modules)
    _, qid, ledger = make_world(tmp_path)
    assert failed(run_gate(good_script(qid), ledger, cfg, deps)) == []
    v = run_gate(good_script(qid, description="We delve into hunter rosebud. " + DISCLOSURE), ledger, cfg, deps)
    assert failed(v) == ["redactor", "slop", "secrets"]


def test_default_deps_take_the_operators_folder_and_public_hosts(tmp_path, cfg, clean_modules):
    folder = fake_checks(tmp_path / "mine")
    _, qid, ledger = make_world(tmp_path)
    no_youtube = SimpleNamespace(checks_dir=folder, redact_patterns=(), public_hosts=("example.com",))
    v = run_gate(good_script(qid), ledger, cfg, G.default_deps(no_youtube))
    assert failed(v) == ["secrets"]  # the fake scanner proves it was handed the settings' hosts, not the defaults
    with_youtube = SimpleNamespace(checks_dir=folder, redact_patterns=(), public_hosts=("youtube.com",))
    assert failed(run_gate(good_script(qid), ledger, cfg, G.default_deps(with_youtube))) == []  # positive control


def test_textcheck_unavailable_fails_redactor_and_slop(tmp_path, cfg, monkeypatch, clean_modules):
    folder = fake_checks(tmp_path / "checks")
    (folder / "redactor.py").unlink()
    (folder / "slop_gate.py").unlink()
    monkeypatch.setattr(paths, "CHECKS_DIR", folder)
    deps = G.default_deps()  # never raises: an unavailable check is a failed check
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid), ledger, cfg, deps)
    assert failed(v) == ["redactor", "slop"]
    assert evidence(v, "redactor") == evidence(v, "slop") == "error:TextcheckUnavailable"


def test_a_missing_or_outside_secret_scanner_fails_secrets(tmp_path, cfg, monkeypatch, clean_modules):
    monkeypatch.setattr(paths, "CHECKS_DIR", fake_checks(tmp_path / "checks", secrets=False))
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid), ledger, cfg, G.default_deps())
    assert failed(v) == ["secrets"] and evidence(v, "secrets") == "error:SecretsUnavailable"
    fake_checks(tmp_path / "outside")
    linked = fake_checks(tmp_path / "linked", secrets=False)
    (linked / "secret_scan.py").symlink_to(tmp_path / "outside" / "secret_scan.py")
    monkeypatch.setattr(paths, "CHECKS_DIR", linked)
    v = run_gate(good_script(qid), ledger, cfg, G.default_deps())
    assert failed(v) == ["secrets"] and evidence(v, "secrets") == "error:SecretsUnavailable"


def test_a_shadow_secret_scanner_on_sys_path_is_never_used(tmp_path, cfg, monkeypatch, clean_modules):
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "secret_scan.py").write_text("def scan_output(text, public_hosts=()):\n    return []\n")
    monkeypatch.syspath_prepend(str(shadow))
    sys.modules.pop("secret_scan", None)
    import secret_scan  # noqa: F401  (the plain name now points at the shadow)
    monkeypatch.setattr(paths, "CHECKS_DIR", fake_checks(tmp_path / "checks"))
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, **{"out.host": words(14) + " hunter"}), ledger, cfg, G.default_deps())
    assert failed(v) == ["secrets"]


# ---- names ----

def test_a_blocked_name_fails_without_echoing_it(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, **{"cold_open.on_screen": "Zephyr rules"}), ledger, cfg)
    assert failed(v) == ["names"] and "zephyr" not in json.dumps(v).lower()


@pytest.mark.parametrize("text", ["zephyr rules", "Zephyrs everywhere", "the backup box", "the backup_box",
                                  "BACKUP-BOX",
                                  "our mydiary-apps folder", "the secret garden", "Zep\u200bhyr rules",
                                  "\uff3a\uff45\uff50\uff48\uff59\uff52 rules", "Z\u0435phyr rules"],
                         ids=["lower", "inflected", "headline-spaced", "headline-underscore", "headline-caps",
                              "fragment-inside", "excluded-spaced", "zero-width", "fullwidth", "homoglyph"])
def test_names_are_matched_like_the_ledger_matches_them(tmp_path, cfg, text):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, **{"cold_open.on_screen": text}), ledger, cfg)
    assert failed(v) == ["names"]


@pytest.mark.parametrize("text", ["the offsite backup system", "a backup plan, boxed in", "the demo tool",
                                  "a diary, an app", "a secret, a garden"])
def test_text_near_a_name_passes(tmp_path, cfg, text):  # positive controls
    _, qid, ledger = make_world(tmp_path)
    assert failed(run_gate(good_script(qid, **{"cold_open.on_screen": text}), ledger, cfg)) == []


# ---- embargo ----

@pytest.mark.parametrize("line", ["we rehearsed the tree talk ", "we rehearsed the tree-talk ",
                                  "we rehearsed the Tree Talks ", "we rehearsed the \uff54\uff52\uff45\uff45 talk "],
                         ids=["plain", "hyphen", "inflected", "fullwidth"])
def test_an_embargo_term_fails(tmp_path, cfg, line):
    _, qid, ledger = make_world(tmp_path)
    story = [line + words(40), words(45), words(45)]
    v = run_gate(good_script(qid, **{"story.host": story}), ledger, cfg)
    assert failed(v) == ["embargo"] and "tree" not in evidence(v, "embargo").lower()


def test_text_near_an_embargo_term_passes(tmp_path, cfg):  # positive control
    _, qid, ledger = make_world(tmp_path)
    story = ["we heard some street talk " + words(40), words(45), words(45)]
    assert failed(run_gate(good_script(qid, **{"story.host": story}), ledger, cfg)) == []


# ---- word_budget ----

@pytest.mark.parametrize("story_words,ok", [(104, False), (105, True), (150, True), (151, False)],
                         ids=["164", "165", "210", "211"])
def test_the_word_budget_is_165_to_210_inclusive(tmp_path, cfg, story_words, ok):
    """The other host lines hold 60 words (15 + 12 + 10 + 8 + 15); the story fills the rest."""
    _, qid, ledger = make_world(tmp_path)
    third = story_words // 3
    story = [words(third), words(third), words(story_words - 2 * third)]
    v = run_gate(good_script(qid, **{"story.host": story}), ledger, cfg)
    assert failed(v) == ([] if ok else ["word_budget"])
    assert evidence(v, "word_budget") == f"{60 + story_words} host words (allowed 165 to 210)"


@pytest.mark.parametrize("field,n", [("steal_this.host", 26), ("confessional.host_intro", 24), ("out.host", 31),
                                     ("cold_open.host", 31), ("scoreboard.host", 28)])
def test_every_host_line_counts(tmp_path, cfg, field, n):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, **{field: words(n)}), ledger, cfg)
    assert failed(v) == ["word_budget"] and evidence(v, "word_budget").startswith("211 ")


def test_host_words_count_runs_of_letters_and_digits():
    """The tokenizer is len(re.findall(r"[A-Za-z0-9]+", normal form)), the one word-cap count: hyphens, slashes
    and apostrophes split words, punctuation alone is no word."""
    script = good_script(**{"out.host": "a\tb\nc  state-of-the-art 24/7 don't . , !"})
    assert G.host_words(script) == 195 - 15 + 11


def test_screen_text_is_not_counted(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, title=words(14), description=words(70)), ledger, cfg)
    assert failed(v) == [] and evidence(v, "word_budget").startswith("195 ")


# ---- no_code ----

CODE = ["Run `make` first", "See https://example.invalid now", "See http://example.invalid now",
        "Go to www.example.invalid today", "It lives in /home/someone/notes", "It lives in ~/notes",
        "Open C:\\Users\\someone now", "Open src/app/main now", "$ make all", "> quoted shell output", "  $ indented",
        "sudo reboot the box", "pip install the tool", "then npm install it", "then git push it", "Git commit it",
        "cd ~/src first", "cd .. and retry", "then cd src/app", "a {block} here", "x => y", "call run(); then",
        "Run \uff40make\uff40 first", "go to \uff48\uff54\uff54\uff50\uff53://x.invalid"]


@pytest.mark.parametrize("text", CODE)
def test_code_commands_paths_and_urls_fail(tmp_path, cfg, text):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, description="A short day in the build.\n" + text + "\n" + DISCLOSURE), ledger, cfg)
    assert failed(v) == ["no_code"]


PROSE = ["A log-first habit keeps every run honest", "It ran 24/7 and/or overnight", "Steal this: one log per run",
         "We published an npm package", "The git history tells the story", "Our CI/CD pipeline stayed green",
         "Plain prose, with commas; and a colon: fine", "It cost five dollars (roughly)"]


@pytest.mark.parametrize("text", PROSE)
def test_plain_prose_passes(tmp_path, cfg, text):  # positive controls: a hyphen, a single slash, a colon
    _, qid, ledger = make_world(tmp_path)
    grounded(ledger, text)
    v = run_gate(good_script(qid, description="A short day in the build.\n" + text + "\n" + DISCLOSURE), ledger, cfg)
    assert failed(v) == []


SHIPPED = ["title", "description", "cold_open.host", "cold_open.on_screen", "scoreboard.host", "story.host",
           "story.on_screen", "steal_this.host", "steal_this.on_screen", "confessional.host_intro", "out.host",
           "out.teaser"]


@pytest.mark.parametrize("field", SHIPPED)
def test_every_shipped_field_is_scanned(tmp_path, cfg, field):
    _, qid, ledger = make_world(tmp_path)
    script = good_script(qid)
    value = get(script, field)
    put(script, field, value[:-1] + [value[-1] + " `x`"] if isinstance(value, list) else value + " `x`")
    v = run_gate(script, ledger, cfg)
    assert failed(v) == ["no_code"] and evidence(v, "no_code") == "backtick"


def test_no_code_evidence_names_shapes_never_text(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    text = "Run `make` at https://private.invalid/x/y or sudo cd ~/secret-place"
    v = run_gate(good_script(qid, description=text), ledger, cfg)
    assert failed(v) == ["no_code"] and evidence(v, "no_code") == "backtick,command,path_or_url"
    assert "private.invalid" not in json.dumps(v) and "secret-place" not in json.dumps(v)


# ---- scoreboard_numbers ----

@pytest.mark.parametrize("digit", ["3", "\uff13", "12"], ids=["ascii", "fullwidth", "two"])
def test_a_digit_in_the_scoreboard_line_fails(tmp_path, cfg, digit):
    _, qid, ledger = make_world(tmp_path)
    grounded(ledger, digit)
    v = run_gate(good_script(qid, **{"scoreboard.host": words(11) + " " + digit}), ledger, cfg)
    assert failed(v) == ["scoreboard_numbers"] and evidence(v, "scoreboard_numbers") == "digits present"


def test_spelled_numbers_and_digits_elsewhere_pass(tmp_path, cfg):  # positive control
    _, qid, ledger = make_world(tmp_path)
    grounded(ledger, "3")
    script = good_script(qid, **{"scoreboard.host": words(11) + " three",
                                 "story.host": [words(44) + " 3", words(45), words(45)]})
    v = run_gate(script, ledger, cfg)
    assert failed(v) == [] and evidence(v, "scoreboard_numbers") == "no digits"


# ---- the built-in redactor, slop gate and secret scanner (daily/checks/) ----

# The operator's settings as default_deps reads them: the built-in folder, one private word as a redact pattern
# ("Nimbus", a machine name in this test), and YouTube as the only public host.
OPERATOR = SimpleNamespace(checks_dir=None, redact_patterns=(("machine", re.compile(r"\bnimbus\b", re.I)),),
                           public_hosts=("youtube.com", "www.youtube.com"))


def test_real_scanners_pass_clean_text_and_catch_a_planted_token(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    deps = G.default_deps()
    assert failed(run_gate(good_script(qid), ledger, cfg, deps)) == []  # positive control
    token = "ghp_" + "a" * 36
    v = run_gate(good_script(qid, description="A short day. " + token), ledger, cfg, deps)
    assert {"redactor", "secrets"} <= set(failed(v))
    assert token not in json.dumps(v)


def test_real_slop_gate_catches_a_banned_dash_and_lexeme(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, title="A seamless day \u2014 really"), ledger, cfg, G.default_deps())
    assert failed(v) == ["slop"]


def test_an_operator_pattern_keeps_a_machine_name_out_of_prose(tmp_path, cfg):
    """A machine name the operator lists in settings.toml checks.redact_patterns fails the redactor in any shipped
    line, so prose refers to that machine by its role."""
    _, qid, ledger = make_world(tmp_path)
    deps = G.default_deps(OPERATOR)
    by_role = good_script(qid, **{"out.host": words(11) + " thanks to the scheduler agent"})
    assert failed(run_gate(by_role, ledger, cfg, deps)) == []  # positive control: the role passes
    by_name = good_script(qid, **{"out.host": words(11) + " thanks to Nimbus overnight"})
    v = run_gate(by_name, ledger, cfg, deps)
    assert failed(v) == ["redactor"] and evidence(v, "redactor") == "ABSTAIN: operator_pattern"
    assert failed(run_gate(by_name, ledger, cfg, G.default_deps())) == []  # without the pattern it is just a word


# ---- review round: scanners read the raw text AND its normal form (NFKC, invisibles dropped, dash look-alikes) ----

TOKEN = "ghp_" + "a" * 36
DASH_LOOKALIKES = [chr(c) for c in (*range(0x2010, 0x2016), 0xFE58, 0xFE31, 0xFE32, 0x2E3A, 0x2E3B, 0x2212,
                                     0x2500, 0x2501, 0x2504, 0x2505, 0x2508, 0x2509, 0x23AF)]
# Fakes that see only plain spellings: a hit through an invisible split or a look-alike must come from the normal form.
PLAIN = G.Deps(
    lambda t: ("ABSTAIN", [("server", "xx")]) if "Nimbus" in t or TOKEN in t else ("PUBLISH", []),
    lambda t: [("banned_dash", "U+2014")] * ("\u2014" in t) + [("ai_slop_lexeme", "seamless")] * ("seamless" in t),
    lambda t: ["GitHub token (value withheld)"] if TOKEN in t else [])
HIDDEN = [
    ("ghp_\u200b" + "a" * 36, ["redactor", "secrets"]),
    ("ghp_" + "a" * 18 + "\u2060" + "a" * 18, ["redactor", "secrets"]),
    ("\uff2e\uff49\uff4d\uff42\uff55\uff53 overnight", ["redactor"]),
    ("Nim\u200bbus overnight", ["redactor"]),
    ("a seam\u200bless day", ["slop"]),
    ("Nim" + chr(0xFE0F) + "bus overnight", ["redactor"]),
    ("Nim" + chr(0x034F) + "bus overnight", ["redactor"]),
    ("Nim" + chr(0x0332) + "bus overnight", ["redactor"]),
    ("ghp_" + chr(0xFE0F) + "a" * 36, ["redactor", "secrets"]),
    ("ghp_" + chr(0xE0101) + "a" * 36, ["redactor", "secrets"]),
    ("a seam" + chr(0xFE0F) + "less day", ["slop"]),
] + [(f"a short day {d} a long night", ["slop"]) for d in DASH_LOOKALIKES]
HIDDEN_IDS = ["token-zwsp", "token-word-joiner", "fullwidth-name", "zwsp-name", "zwsp-slop", "vs16-name",
              "cgj-name", "low-line-name", "token-vs16", "token-vs17", "vs16-slop"] + [
    f"dash-U+{ord(d):04X}" for d in DASH_LOOKALIKES]


@pytest.mark.parametrize("hidden,checks", HIDDEN, ids=HIDDEN_IDS)
def test_a_hit_hidden_by_invisible_or_lookalike_characters_still_fails(tmp_path, cfg, hidden, checks):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, description="A day with " + hidden + ". " + DISCLOSURE), ledger, cfg, PLAIN)
    assert failed(v) == checks and TOKEN not in json.dumps(v)


def test_plain_text_and_ascii_hyphens_stay_clean_under_both_forms(tmp_path, cfg):  # positive control
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, description="A log-first day, well-run. " + DISCLOSURE), ledger, cfg, PLAIN)
    assert failed(v) == []


def test_each_scanner_sees_the_normal_form_only_when_it_differs(tmp_path, cfg):
    seen = []
    deps = G.Deps(lambda t: (seen.append(t), ("PUBLISH", []))[1], lambda t: [], lambda t: [])
    _, qid, ledger = make_world(tmp_path)
    run_gate(good_script(qid), ledger, cfg, deps)
    assert len(seen) == 1  # plain ASCII: one form
    seen.clear()
    run_gate(good_script(qid, description="A day with Nim\u200bbus. " + DISCLOSURE), ledger, cfg, deps)
    assert len(seen) == 2 and "Nim\u200bbus" in seen[0] and "Nimbus" in seen[1]


@pytest.mark.parametrize("hidden,checks", HIDDEN, ids=HIDDEN_IDS)
def test_real_scanners_catch_hidden_hits(tmp_path, cfg, hidden, checks):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, description="A day with " + hidden + ". " + DISCLOSURE), ledger, cfg,
                 G.default_deps(OPERATOR))
    assert set(checks) <= set(failed(v)) and TOKEN not in json.dumps(v)


# ---- review round: names ignore separators and need a word start (a camelCase change counts as one) ----

@pytest.mark.parametrize("text", ["Zeph-yr rules", "Zeph*yr rules", "Zeph.yr rules", "OpenZephyr rules",
                                  "the backupbox",
                                  "the BackupBox", "the backup\nbox", "back up box", "Zeph\u00adyr rules",
                                  "Zeph\u200cyr rules", "the Backup-Box"])
def test_a_blocked_name_with_its_separators_moved_or_removed_fails(tmp_path, cfg, text):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, **{"cold_open.on_screen": text}), ledger, cfg)
    assert failed(v) == ["names"]


@pytest.mark.parametrize("text", ["openzephyr rules", "a backup plan, boxed in", "the backups box set"])
def test_a_name_run_into_a_lowercase_word_passes(tmp_path, cfg, text):
    """Decision: a name must start a word (after a non-letter-or-digit, or at a lower-to-upper case change), so a
    short private name cannot block every word that contains it; "openzephyr" is the accepted cost."""
    _, qid, ledger = make_world(tmp_path)
    assert failed(run_gate(good_script(qid, **{"cold_open.on_screen": text}), ledger, cfg)) == []



# ---- review round, minors ----

@pytest.mark.parametrize("numeral", [chr(0xFF11) + chr(0xFF12), chr(0xB2), chr(0xBD), chr(0x216B), chr(0x2473),
                                     chr(0x663)], ids=["fullwidth-12", "superscript-2", "one-half", "roman-12",
                                                       "circled-20", "arabic-indic-3"])
def test_any_numeral_in_the_scoreboard_line_fails(tmp_path, cfg, numeral):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, **{"scoreboard.host": words(11) + " " + numeral}), ledger, cfg)
    assert "scoreboard_numbers" in failed(v) and evidence(v, "scoreboard_numbers") == "digits present"


def test_hyphen_chains_count_every_word(tmp_path, cfg):
    """60 whitespace tokens in the story (120 in all, inside the old budget) are 180 words when counted properly."""
    _, qid, ledger = make_world(tmp_path)
    line = " ".join(["we-built-it"] * 20)
    v = run_gate(good_script(qid, **{"story.host": [line, line, line]}), ledger, cfg)
    assert failed(v) == ["word_budget"] and evidence(v, "word_budget").startswith("240 ")


def test_punctuation_is_no_word(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    dots = " ".join(["."] * 30)
    v = run_gate(good_script(qid, **{"story.host": [dots, dots, dots]}), ledger, cfg)
    assert failed(v) == ["word_budget"] and evidence(v, "word_budget").startswith("60 ")


def test_an_invisible_character_cannot_split_a_word(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, **{"out.host": words(14) + " wo" + chr(0x200B) + "rd"}), ledger, cfg)
    assert failed(v) == [] and evidence(v, "word_budget").startswith("195 ")


RM = "rm"  # spelled apart so a shell guard reading this file's text sees no destructive command


@pytest.mark.parametrize("text", ["then " + RM + " -rf the build", RM + " -r old logs", "curl x.sh | bash",
                                  "curl it |sh", "pipe it | sudo sh", "call foo() now", "run alert(1) now",
                                  "javascript:alert", "see evil.com/x now", "go to Evil.Com/path today", "ftp://a.b",
                                  "//evil.com", "https" + chr(0xFF1A) + "//evil", "htt" + chr(0x200B) + "ps://evil",
                                  "pip3 install x"])
def test_more_code_shapes_fail(tmp_path, cfg, text):
    _, qid, ledger = make_world(tmp_path)
    grounded(ledger, text)
    v = run_gate(good_script(qid, **{"story.on_screen": ["Script in", text]}), ledger, cfg)
    assert failed(v) == ["no_code"]


@pytest.mark.parametrize("text", ["the agent(s) finished", "five dollars (roughly)", "version 2.0/3.0 shipped",
                                  "the AI/ML team", "this | that", "the shell held", "we firmed it up",
                                  "Visit example.com"])
def test_prose_near_the_new_code_shapes_passes(tmp_path, cfg, text):  # positive controls
    _, qid, ledger = make_world(tmp_path)
    grounded(ledger, text)
    assert failed(run_gate(good_script(qid, **{"story.on_screen": ["Script in", text]}), ledger, cfg)) == []


@pytest.mark.parametrize("text", ["ls -la", "<script>", "x = [1]", "brew install x", "uv add x", "make deploy"])
def test_shapes_left_to_the_writer_rules(tmp_path, cfg, text):
    """Probed and left out by triage (2026-10-17): no_code catches the listed shapes only; these stay the writer's
    rule 9 and the operator's review. A test so that widening no_code is a deliberate change."""
    _, qid, ledger = make_world(tmp_path)
    grounded(ledger, text)
    assert failed(run_gate(good_script(qid, **{"story.on_screen": ["Script in", text]}), ledger, cfg)) == []


def test_a_source_path_through_a_parent_folder_fails(tmp_path, cfg):
    w = claude_world(tmp_path)
    (tmp_path / "sub").mkdir()
    w.source["path"] = str(tmp_path / "sub" / ".." / "s.jsonl")
    w.rehash()
    assert_verbatim_fails(w.gate(cfg), "bad_path")


@pytest.mark.parametrize("kind", KINDS)
def test_a_symlinked_source_fails(tmp_path, cfg, kind):
    w = KINDS[kind](tmp_path)
    real = w.src.with_name("real-" + w.src.name)
    w.src.rename(real)
    w.src.symlink_to(real)  # the same bytes behind a link: a link can point anywhere
    assert_verbatim_fails(w.gate(cfg), "source_link")


def test_a_pipe_in_place_of_the_source_fails_without_hanging(tmp_path, cfg):
    import signal
    w = claude_world(tmp_path)
    w.src.unlink()
    os.mkfifo(w.src)

    def hung(signum, frame):
        raise TimeoutError("the gate opened a pipe and waited")

    old = signal.signal(signal.SIGALRM, hung)
    signal.alarm(5)
    try:
        v = w.gate(cfg)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert_verbatim_fails(v, "source_not_file")


@pytest.mark.parametrize("kind,line", [
    ("claude", '{"type": "user", "type": "assistant", "uuid": "u1", "message": {"content": [{"type": "text", '
               '"text": "Done. ' + QUOTE + '"}]}}'),
    ("claude", '{"type": "assistant", "uuid": "u1", "message": {"content": [{"type": "text", "text": "x", '
               '"text": "Done. ' + QUOTE + '"}]}}'),
    ("pr", '{"repo": "owner/demo-tool", "number": 7, "harness": "codex", "body": "' + QUOTE + '", '
           '"body": "other"}'),
], ids=["top-level", "nested", "pr-body"])
def test_a_source_line_with_a_repeated_key_fails(tmp_path, cfg, kind, line):
    """Parsers disagree on which copy of a repeated key wins, so a line that holds one proves nothing."""
    w = KINDS[kind](tmp_path)
    lines = w.src.read_text().splitlines(keepends=True)
    lines[w.line - 1] = line + "\n"
    w.src.write_text("".join(lines))
    assert_verbatim_fails(w.gate(cfg), "duplicate_key")


def test_a_script_with_a_repeated_key_fails_the_schema_check(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    raw = json.dumps(good_script(qid)).replace('"title": ', '"title": "Zephyr rules", "title": ', 1)
    v = G.gate(raw.encode(), ledger, cfg, OK)
    assert failed(v) == list(G.REQUIRED) and evidence(v, "schema") == "duplicate_key"
    assert "zephyr" not in json.dumps(v).lower()


def test_a_quote_text_field_in_the_script_fails_the_schema_check(tmp_path, cfg):
    """The writer picks a quote by id only; a text of its own would never be checked against the source."""
    _, qid, ledger = make_world(tmp_path)
    script = good_script(qid)
    script["confessional"]["text"] = "Something the agent never said."
    v = run_gate(script, ledger, cfg)
    assert failed(v) == list(G.REQUIRED) and evidence(v, "schema") == "keys:confessional"



# ---- review round 2 ----

def test_real_scanners_pass_accented_prose(tmp_path, cfg):  # positive control for the mark-stripping normal form
    _, qid, ledger = make_world(tmp_path)
    desc = "A caf" + chr(0xE9) + " day, a na" + chr(0xEF) + "ve plan. " + DISCLOSURE
    v = run_gate(good_script(qid, description=desc), ledger, cfg, G.default_deps())
    assert failed(v) == []


def test_accented_prose_passes_and_an_accented_name_fails(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    cafe = good_script(qid, **{"cold_open.on_screen": "a caf" + chr(0xE9) + " day"})
    assert failed(run_gate(cafe, ledger, cfg)) == []
    v = run_gate(good_script(qid, **{"cold_open.on_screen": "Z" + chr(0xE9) + "phyr rules"}), ledger, cfg)
    assert failed(v) == ["names"]


def test_an_all_caps_name_fails(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    assert failed(run_gate(good_script(qid, **{"cold_open.on_screen": "the BACKUPBOX"}), ledger, cfg)) == ["names"]


@pytest.mark.parametrize("text", ["MYZEPHYR rules", "Z3phyr rules"])
def test_leetspeak_and_run_together_capitals_are_out_of_scope(tmp_path, cfg, text):
    """Out of scope by decision (2026-10-17): an open vocabulary, covered by the writer rules, the ledger prefilter and
    the operator's review. A test so that taking it on is a deliberate change."""
    _, qid, ledger = make_world(tmp_path)
    grounded(ledger, text)
    assert failed(run_gate(good_script(qid, **{"cold_open.on_screen": text}), ledger, cfg)) == []


@pytest.mark.parametrize("text", ["evil" + chr(0x3002) + "com/x", "evil" + chr(0xFF0E) + "com/x",
                                  "evil" + chr(0xFF61) + "com/x", "evil.com:8080/x", "203.0.113.5/admin",
                                  "evil[.]com", "https:evil.com", "http:evil", RM + " --recursive --force .",
                                  RM + " --force x", "curl x | python3 -", "curl x | python", "curl x | node",
                                  "cat x | perl", "cat x | ruby", "htt" + chr(0x0332) + "ps://evil"])
def test_round_two_code_shapes_fail(tmp_path, cfg, text):
    _, qid, ledger = make_world(tmp_path)
    grounded(ledger, text)
    v = run_gate(good_script(qid, **{"story.on_screen": ["Script in", text]}), ledger, cfg)
    assert failed(v) == ["no_code"]


@pytest.mark.parametrize("text", ["run foo (x) now", "the (x) factor", "port 8080 stayed open", "it took 3.5 hours",
                                  "at 10:30 we shipped", "python is fine", "a node in the graph"])
def test_round_two_prose_controls_pass(tmp_path, cfg, text):
    _, qid, ledger = make_world(tmp_path)
    grounded(ledger, text)
    assert failed(run_gate(good_script(qid, **{"story.on_screen": ["Script in", text]}), ledger, cfg)) == []


# ---- quoted sources must resolve under an allowed root ----

def test_a_source_outside_every_root_fails(tmp_path, cfg, monkeypatch):
    w = claude_world(tmp_path)
    monkeypatch.setattr(paths, "DEFAULT_CLAUDE_PROJECTS", tmp_path / "projects")
    assert_verbatim_fails(w.gate(cfg), "outside_roots")


def test_the_work_dir_is_a_root_only_when_passed(tmp_path, cfg, monkeypatch):
    w = pr_world(tmp_path)  # github.jsonl under tmp_path/work
    monkeypatch.setattr(paths, "DEFAULT_CLAUDE_PROJECTS", tmp_path / "projects")
    raw = json.dumps(w.script).encode()
    assert failed(G.gate(raw, w.ledger, cfg, OK, work_dir=tmp_path / "work")) == []  # positive control
    v = G.gate(raw, w.ledger, cfg, OK)
    assert failed(v) == ["quote_verbatim"] and evidence(v, "quote_verbatim") == "outside_roots"


@pytest.mark.parametrize("store", ["sessions", "archived_sessions"])
def test_codex_session_stores_are_roots(tmp_path, cfg, monkeypatch, store):
    monkeypatch.setattr(paths, "DEFAULT_CLAUDE_PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(paths, "DEFAULT_CODEX_DB", tmp_path / "codex" / "state_5.sqlite")
    w = codex_world(tmp_path / "codex" / store)
    assert failed(w.gate(cfg)) == []


def test_a_symlinked_parent_folder_leading_out_of_the_root_fails(tmp_path, cfg, monkeypatch):
    projects = tmp_path / "projects"
    w = claude_world(tmp_path / "elsewhere")
    projects.mkdir()
    (projects / "linkdir").symlink_to(tmp_path / "elsewhere")
    monkeypatch.setattr(paths, "DEFAULT_CLAUDE_PROJECTS", projects)
    w.source["path"] = str(projects / "linkdir" / w.src.name)
    w.rehash()
    assert_verbatim_fails(w.gate(cfg), "outside_roots")


def test_a_symlinked_root_still_holds_its_files(tmp_path, cfg, monkeypatch):  # positive control
    w = claude_world(tmp_path / "real-projects")
    (tmp_path / "projects").symlink_to(tmp_path / "real-projects")
    monkeypatch.setattr(paths, "DEFAULT_CLAUDE_PROJECTS", tmp_path / "projects")
    assert failed(w.gate(cfg)) == []


# ---- the word counter the writer shares ----

def test_host_beats_and_line_words_add_up_to_host_words():
    s = good_script(**{"story.host": ["don't stop-the bot " + words(20), words(7)], "out.host": "So, we're done."})
    beats = G.host_beats(s)
    assert list(beats) == ["cold_open", "scoreboard", "story", "steal_this", "confessional", "out"]
    assert beats["story"] == s["story"]["host"] and beats["confessional"] == [s["confessional"]["host_intro"]]
    assert [line for lines in beats.values() for line in lines] == G.host_lines(s)
    assert G.line_words("don't stop-the bot") == 5  # a contraction and a hyphen chain count every part
    assert sum(G.line_words(line) for lines in beats.values() for line in lines) == G.host_words(s)


# ---- numbers_grounded: every number shown or spoken is in the ledger the writer saw ----

def grounded_world(tmp_path, quote=QUOTE):
    """make_world's ledger with text the writer saw: a PR item title and body, its lane label and why, a teaser and a
    scoreboard."""
    _, qid, ledger = make_world(tmp_path, quote)
    ledger["items"] = [{"id": "pr:demo-tool#7", "kind": "pr", "lane": "demo-tool", "disclosure": "public",
                        "label": "the demo tool", "why": "so 3 videos make themselves", "harness": "codex",
                        "title": "Cut the check time", "body": "Checks now run 75% faster, in 2.5 minutes.",
                        "url": "https://github.com/owner/demo-tool/pull/41", "ts": "2026-10-16T21:00:00Z"}]
    ledger["teasers"] = [{"id": "arc:f55ff16f", "title": "Ship the 12 guide videos", "harness": "codex"}]
    ledger["scoreboard"] = {"prs_merged": 16, "arcs_closed": 6, "sessions": {"claude_code": 17, "codex": 9},
                            "subagents": {"alpha_agent": 0}}
    return qid, ledger


def numbers(v):
    return next(c for c in v["checks"] if c["name"] == "numbers_grounded")


@pytest.mark.parametrize("field,value", [
    ("story.on_screen", ["75% faster checks"]),
    ("story.on_screen", ["Checks in 2.5 minutes"]),
    ("story.on_screen", ["75 percent faster"]),  # a percentage in the ledger grounds its bare number
    ("out.teaser", "Next: 12 guide videos"),  # a teaser title
    ("steal_this.on_screen", "Steal this: 3 videos a day"),  # the lane's why
    ("cold_open.on_screen", "16 changes shipped"),  # a scoreboard count
    ("cold_open.on_screen", "9 Codex sessions"),  # a nested scoreboard count
    ("cold_open.on_screen", "0 Alpha Agent helpers"),
    ("story.on_screen", [chr(0xFF17) + chr(0xFF15) + chr(0xFF05) + " faster"]),  # fullwidth 75% reads as 75%
    ("story.on_screen", ["7" + chr(0x200B) + "5% faster"]),  # an invisible split cannot make a new number
], ids=["percent", "decimal", "bare_of_percent", "teaser", "why", "scoreboard", "nested", "zero", "fullwidth",
        "zero_width"])
def test_a_number_the_ledger_holds_passes(tmp_path, cfg, field, value):
    qid, ledger = grounded_world(tmp_path)
    script = good_script(qid)
    put(script, field, value)
    v = run_gate(script, ledger, cfg)
    assert numbers(v)["pass"] is True and numbers(v)["evidence"] == "grounded:1"


@pytest.mark.parametrize("field,value", [
    ("story.on_screen", ["90% faster checks"]),  # invented
    ("story.on_screen", ["5% faster"]),  # inside 75%: a number is a whole token, never a substring
    ("story.on_screen", ["2 minutes"]),  # part of 2.5
    ("story.on_screen", ["16% more"]),  # a bare count never grounds a percentage
    ("story.on_screen", ["Change 7 shipped"]),  # only in the item id, which the writer does not read as text
    ("story.on_screen", ["41 today"]),  # only in the url, which writer_view drops
    ("title", "What my agents built in 2026"),  # a year: the ledger's day field is not text the writer saw
    ("story.on_screen", [chr(0x0667) + chr(0x0665) + "% faster"]),  # Arabic-Indic digits are digits too
], ids=["invented", "substring", "decimal_part", "percent_of_count", "item_id", "url", "year", "arabic_indic"])
def test_a_number_the_ledger_lacks_fails_without_echoing_it(tmp_path, cfg, field, value):
    qid, ledger = grounded_world(tmp_path)
    script = good_script(qid)
    assert numbers(run_gate(script, ledger, cfg))["evidence"] == "no numbers"  # positive control
    put(script, field, value)
    v = run_gate(script, ledger, cfg)
    assert numbers(v) == {"name": "numbers_grounded", "pass": False, "evidence": "ungrounded:1"}
    assert v["pass"] is False


def test_a_year_in_the_title_passes_once_the_ledger_text_holds_it(tmp_path, cfg):
    qid, ledger = grounded_world(tmp_path)
    ledger["sessions"][0]["title"] = "Plan the 2026 recap"
    v = run_gate(good_script(qid, title="What my agents built in 2026"), ledger, cfg)
    assert numbers(v)["pass"] is True


def test_the_count_is_of_distinct_ungrounded_numbers(tmp_path, cfg):
    qid, ledger = grounded_world(tmp_path)
    script = good_script(qid, **{"story.on_screen": ["90% faster", "44 runs, 75% fewer", "90% again"]})
    assert numbers(run_gate(script, ledger, cfg))["evidence"] == "ungrounded:2"


def test_numbers_in_the_chosen_quote_are_grounded_by_the_quote(tmp_path, cfg):
    quote = "The new browser pool shipped today and all 12 tests passed."
    _, qid, ledger = make_world(tmp_path, quote)
    v = run_gate(good_script(qid), ledger, cfg)
    assert numbers(v) == {"name": "numbers_grounded", "pass": True, "evidence": "grounded:1"} and v["pass"] is True


def test_a_headline_items_label_and_why_ground_numbers_but_its_count_does_not(tmp_path, cfg):
    qid, ledger = grounded_world(tmp_path)
    ledger["items"].append({"id": "lane:backup-box", "kind": "headline", "lane": "backup-box",
                            "disclosure": "headline", "label": "the 2 backup boxes", "why": "so 1 dead disk is fine",
                            "count": 5})
    for value, ok in (["2 backup boxes"], True), (["1 dead disk"], True), (["5 quiet fixes"], False):
        script = good_script(qid, **{"story.on_screen": value})
        assert numbers(run_gate(script, ledger, cfg))["pass"] is ok


def test_a_malformed_ledger_fails_the_number_check_closed(tmp_path, cfg):
    qid, ledger = grounded_world(tmp_path)
    ledger["items"] = "not a list"
    script = good_script(qid, **{"story.on_screen": ["75% faster"]})
    check = numbers(run_gate(script, ledger, cfg))
    assert check["pass"] is False and check["evidence"].startswith("error:")


# ---- accented config terms: matched through the normal form, in both spellings ----

JOSE = "Jos" + chr(0xE9) + " Ram" + chr(0xED) + "rez"
ZOE = "Zo" + chr(0xEB)
CAFE = "caf" + chr(0xE9)


def accented(cfg):
    """An accented embargo term, blocked name, hard_exclude entry and headline lane name."""
    lanes = {**cfg.lanes, CAFE + "-sync": Lane(CAFE + "-sync", "headline", "the sync tool", "so files stay in sync")}
    return dataclasses.replace(cfg, lanes=lanes, blocked_names=("Zephyr", ZOE), embargo_terms=("tree talk", JOSE),
                               hard_exclude=cfg.hard_exclude + (CAFE + "-notes",))


@pytest.mark.parametrize("text,check", [("we met " + JOSE, "embargo"), ("we met Jose Ramirez", "embargo"),
                                        (ZOE + " rules", "names"), ("Zoe rules", "names"),
                                        ("the " + CAFE + "-notes pile", "names"), ("the cafe-notes pile", "names"),
                                        ("the " + CAFE + " sync", "names"), ("the cafe sync", "names")],
                         ids=["embargo-accented", "embargo-ascii", "blocked-accented", "blocked-ascii",
                              "exclude-accented", "exclude-ascii", "lane-accented", "lane-ascii"])
def test_accented_config_terms_block_both_spellings_at_the_gate(tmp_path, cfg, text, check):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, **{"cold_open.on_screen": text}), ledger, accented(cfg))
    assert failed(v) == [check]


@pytest.mark.parametrize("text", ["we met Jose Ramos", "we met Joseph", "the cafe opened", "a zone rules"])
def test_text_near_accented_config_terms_passes(tmp_path, cfg, text):  # positive controls
    _, qid, ledger = make_world(tmp_path)
    assert failed(run_gate(good_script(qid, **{"cold_open.on_screen": text}), ledger, accented(cfg))) == []



# ---- one path rule: the gate's no_code reads ledger.path_or_url ----

@pytest.mark.parametrize("text", ["mail mailto:sam@example.org", "data:text sample", "ssh:example-host now",
                                  "wss:example.org feed", "file:notes here"])
def test_the_shared_path_rule_adds_schemes_to_no_code(tmp_path, cfg, text):
    _, qid, ledger = make_world(tmp_path)
    v = run_gate(good_script(qid, **{"story.on_screen": ["Script in", text]}), ledger, cfg)
    assert failed(v) == ["no_code"] and evidence(v, "no_code") == "path_or_url"


@pytest.mark.parametrize("text", ["the data: three runs", "one file: the config", "Visit example.com"])
def test_a_colon_in_prose_is_no_scheme(tmp_path, cfg, text):  # positive controls
    _, qid, ledger = make_world(tmp_path)
    assert failed(run_gate(good_script(qid, **{"story.on_screen": ["Script in", text]}), ledger, cfg)) == []


@pytest.mark.parametrize("problem,code", [("keys:title,ghp_secret", "keys:title,other"), ("too_long:title", "too_long:title"),
                                          ("day_mismatch", "day_mismatch"), ("keys:x,y", "keys:other")])
def test_safe_problem_keeps_only_script_field_names(problem, code):
    assert G.safe_problem(problem) == code



# ---- private repo names seen that day fail the names check (pipeline finding #2) ----

@pytest.mark.parametrize("text", ["the mystery repo", "Mystery-Repo rules", "the mysteryrepo"])
def test_a_private_repo_name_from_the_ledger_fails_names(tmp_path, cfg, text):
    _, qid, ledger = make_world(tmp_path)
    ledger["private_names"] = ["mystery-repo"]
    v = run_gate(good_script(qid, **{"cold_open.on_screen": text}), ledger, cfg)
    assert failed(v) == ["names"] and "mystery" not in json.dumps(v).lower()


def test_a_private_repo_name_seen_in_a_pr_fails_the_gate_end_to_end(tmp_path, cfg):
    _, qid, world = make_world(tmp_path)
    built, _ = build_ledger(DAY, RawSources(prs=(PR("owner/mystery-repo", 3, "t", "b", "u", "2026-10-16T21:00:00Z"),),
                                            arcs=(), claude=None, codex=None), cfg, "now", "/x")
    assert built["private_names"] == ["mystery-repo"] and "mystery" not in json.dumps(G.writer_view(built))
    world["private_names"] = built["private_names"]
    assert failed(run_gate(good_script(qid, **{"cold_open.on_screen": "the mystery repo"}), world, cfg)) == ["names"]
    assert failed(run_gate(good_script(qid), world, cfg)) == []  # positive control


def test_malformed_private_names_fail_closed(tmp_path, cfg):
    _, qid, ledger = make_world(tmp_path)
    ledger["private_names"] = "mystery-repo"
    check = next(c for c in run_gate(good_script(qid), ledger, cfg)["checks"] if c["name"] == "names")
    assert check["pass"] is False and check["evidence"].startswith("error:")


# ---- read_verified: the read-once consumer step ----

def _trio(tmp_path, checks=None, **over):
    script, ledger = b'{"title": "t"}', b'{"quotes": []}'
    v = {"schema": G.VERDICT_SCHEMA, "pass": True, "subject_sha256": hashlib.sha256(script).hexdigest(),
         "ledger_sha256": hashlib.sha256(ledger).hexdigest(),
         "checks": [{"name": n, "pass": True} for n in G.REQUIRED] if checks is None else checks}
    v.update(over)
    for name, data in (("script.json", script), ("ledger.json", ledger)):
        (tmp_path / name).write_bytes(data)
    (tmp_path / "verdict.json").write_text(json.dumps(v))
    return tmp_path / "verdict.json", tmp_path / "script.json", tmp_path / "ledger.json", script, ledger


def test_read_verified_returns_the_bytes_the_verdict_names(tmp_path):
    v, s, l, script, ledger = _trio(tmp_path)
    assert G.read_verified(v, s, l, G.REQUIRED) == (script, ledger, "ok")


@pytest.mark.parametrize("over,why", [
    ({"pass": False}, "verdict_not_pass"), ({"pass": "true"}, "verdict_not_pass"), ({"pass": 1}, "verdict_not_pass"),
    ({"pass": None}, "verdict_not_pass"), ({"subject_sha256": "0" * 64}, "sha_mismatch"),
    ({"ledger_sha256": "0" * 64}, "ledger_mismatch"), ({"ledger_sha256": None}, "ledger_mismatch"),
    ({"schema": "other"}, "bad_schema"), ({"checks": []}, "missing:" + ",".join(G.REQUIRED)),
    ({"checks": "all"}, "missing:" + ",".join(G.REQUIRED))])
def test_read_verified_refuses_a_verdict_that_does_not_pass_or_name_these_bytes(tmp_path, over, why):
    v, s, l, *_ = _trio(tmp_path, **over)
    assert G.read_verified(v, s, l, G.REQUIRED) == (None, None, why)


def test_read_verified_judges_the_check_list_like_accepts(tmp_path):
    good = [{"name": n, "pass": True} for n in G.REQUIRED]
    cases = {
        "missing": (good[1:], "missing:" + G.REQUIRED[0]),
        "failing": ([dict(good[0], **{"pass": False})] + good[1:], "failed:" + G.REQUIRED[0]),
        "string_pass": ([dict(good[0], **{"pass": "true"})] + good[1:], "failed:" + G.REQUIRED[0]),
        "duplicate": (good + [good[0]], "duplicate_check:" + G.REQUIRED[0]),
    }
    for checks, why in cases.values():
        v, s, l, *_ = _trio(tmp_path, checks=checks)
        assert G.read_verified(v, s, l, G.REQUIRED) == (None, None, why)
        assert G.accepts(v, s, G.REQUIRED, ledger_path=l) == (False, why)


def test_read_verified_takes_the_required_set_as_given(tmp_path):
    v, s, l, script, ledger = _trio(tmp_path, checks=[{"name": "a", "pass": True}])
    assert G.read_verified(v, s, l, ("a",)) == (script, ledger, "ok")
    assert G.read_verified(v, s, l, ("a", "b")) == (None, None, "missing:b")


def test_read_verified_refuses_unreadable_or_odd_files(tmp_path):
    v, s, l, *_ = _trio(tmp_path)
    for bad, why in (("{broken", "unreadable:JSONDecodeError"), ("[]", "bad_schema"),
                     ('{"pass": true, "pass": true}', "duplicate_key")):
        v.write_text(bad)
        assert G.read_verified(v, s, l, G.REQUIRED) == (None, None, why)
    v.unlink()
    assert G.read_verified(v, s, l, G.REQUIRED) == (None, None, "unreadable:FileNotFoundError")
    v, s, l, *_ = _trio(tmp_path)
    l.unlink()
    assert G.read_verified(v, s, l, G.REQUIRED) == (None, None, "unreadable:FileNotFoundError")


def test_read_verified_never_raises_on_odd_required(tmp_path):
    v, s, l, *_ = _trio(tmp_path)
    assert G.read_verified(v, s, l, None)[2].startswith("error:")


# ---- read_verified_render: the read-once render verdict step ----

def _render_files(tmp_path, **over):
    ledger, video = b'{"day": "2026-10-16"}', b"video bytes"
    v = {"schema": G.VERDICT_SCHEMA, "subject_sha256": files.sha256_bytes(video),
         "ledger_sha256": files.sha256_bytes(ledger), "pass": True,
         "checks": [{"name": n, "pass": True} for n in G.RENDER_REQUIRED]}
    v.update(over)
    (tmp_path / "render.json").write_text(json.dumps(v))
    (tmp_path / "episode.mp4").write_bytes(video)
    (tmp_path / "ledger.json").write_bytes(ledger)
    return tmp_path / "render.json", tmp_path / "episode.mp4", tmp_path / "ledger.json", video, ledger


def test_read_verified_render_returns_verdict_and_the_judged_bytes(tmp_path):
    r, e, l, video, ledger = _render_files(tmp_path, stage="render")
    v, subject, led, why = G.read_verified_render(r, e, l, G.RENDER_REQUIRED)
    assert why == "ok" and v["stage"] == "render" and subject == video and led == ledger


@pytest.mark.parametrize("over,why", [({"pass": False}, "verdict_not_pass"), ({"schema": "x"}, "bad_schema"),
                                      ({"subject_sha256": "0" * 64}, "sha_mismatch"),
                                      ({"ledger_sha256": "0" * 64}, "ledger_mismatch")])
def test_read_verified_render_refuses_like_accepts(tmp_path, over, why):
    r, e, l, *_ = _render_files(tmp_path, **over)
    assert G.read_verified_render(r, e, l, G.RENDER_REQUIRED) == (None, None, None, why)
    assert G.accepts(r, e, G.RENDER_REQUIRED, ledger_path=l) == (False, why)


def test_read_verified_render_never_raises_on_missing_or_duplicate(tmp_path):
    r, e, l, *_ = _render_files(tmp_path)
    r.write_text('{"schema": "a", "schema": "b"}')
    assert G.read_verified_render(r, e, l, G.RENDER_REQUIRED)[3] == "duplicate_key"
    r.unlink()
    assert G.read_verified_render(r, e, l, G.RENDER_REQUIRED)[3].startswith("unreadable:")


def test_read_verified_full_returns_the_parsed_verdict_for_any_subject(tmp_path):
    r, e, l, video, ledger = _render_files(tmp_path, dry=False)
    v, subject, led, why = G.read_verified_full(r, e, l, G.RENDER_REQUIRED)
    assert why == "ok" and v["dry"] is False and subject == video and led == ledger
    assert G.read_verified_full(r, e, l, G.REQUIRED)[3].startswith("missing:")


# ---- the merge review's two direct tests ----

def test_read_verified_render_without_a_ledger_path_skips_only_the_ledger_binding(tmp_path):
    r, e, l, video, ledger = _render_files(tmp_path, stage="render", ledger_sha256="0" * 64)  # binding would fail
    v, subject, led, why = G.read_verified_render(r, e, None, G.RENDER_REQUIRED)
    assert why == "ok" and subject == video and led is None and v["stage"] == "render"
    assert G.read_verified_render(r, e, l, G.RENDER_REQUIRED)[3] == "ledger_mismatch"  # positive control: it is bound
    # every other check still runs without a ledger
    for over, why in (({"schema": "x"}, "bad_schema"), ({"subject_sha256": "0" * 64}, "sha_mismatch"),
                      ({"pass": False}, "verdict_not_pass"),
                      ({"checks": [{"name": n, "pass": True} for n in G.RENDER_REQUIRED[1:]]},
                       "missing:" + G.RENDER_REQUIRED[0]),
                      ({"checks": [{"name": n, "pass": n != G.RENDER_REQUIRED[0]} for n in G.RENDER_REQUIRED]},
                       "failed:" + G.RENDER_REQUIRED[0]),
                      ({"stage": "voice"}, "stage")):
        d = tmp_path / why.replace(":", "_")
        d.mkdir()
        r2, e2, _l, *_ = _render_files(d, **{"stage": "render", **over})
        assert G.read_verified_render(r2, e2, None, G.RENDER_REQUIRED) == (None, None, None, why)


@pytest.mark.parametrize("stage", ["voice", "text", "", None, 3])
def test_read_verified_render_refuses_a_verdict_whose_stage_is_not_render(tmp_path, stage):
    r, e, l, *_ = _render_files(tmp_path, stage=stage)
    assert G.accepts(r, e, G.RENDER_REQUIRED, ledger_path=l) == (True, "ok")  # everything else passes
    assert G.read_verified_render(r, e, l, G.RENDER_REQUIRED) == (None, None, None, "stage")
    assert G.read_verified_render(r, e, None, G.RENDER_REQUIRED) == (None, None, None, "stage")


# ---- a private name that is only a harness's display word is not a blocked name ----

def _names_verdict(tmp_path, cfg, private, text):
    _, qid, ledger = make_world(tmp_path)
    ledger["private_names"] = private
    return run_gate(good_script(qid, **{"cold_open.on_screen": text}), ledger, cfg)


def test_dot_claude_private_name_does_not_block_claude_code(tmp_path, cfg):
    v = _names_verdict(tmp_path, cfg, [".claude"], "Claude Code shipped it")
    assert failed(v) == []


@pytest.mark.parametrize("private,text", [("claude-notebook", "the claude notebook repo"), ("codex-playbook", "the codex playbook"),
                                          (".claude-notebook", "Claude-Notebook notes")])
def test_a_private_name_with_anything_more_than_a_harness_word_still_blocks(tmp_path, cfg, private, text):
    assert failed(_names_verdict(tmp_path, cfg, [private], text)) == ["names"]


@pytest.mark.parametrize("private,text", [("claude", "Claude Code"), (".claude", "Claude"), ("Claude Code", "Claude Code"),
                                          ("codex", "Codex"), (".codex", "Codex")])
def test_every_harness_display_word_alone_is_relaxed(tmp_path, cfg, private, text):
    assert "names" not in failed(_names_verdict(tmp_path, cfg, [private], text))


@pytest.mark.parametrize("private,text", [("alpha", "Alpha Agent"), ("alpha-agent", "Alpha Agent")])
def test_an_adapter_display_word_is_never_relaxed(tmp_path, cfg, private, text):
    """Only the built-in harness names are relaxed: a private repo named like an operator's adapter still blocks."""
    assert failed(_names_verdict(tmp_path, cfg, [private], text)) == ["names"]


def test_the_same_names_do_block_when_they_are_not_relaxed_words(tmp_path, cfg):
    # positive control for the test above: the names check really sees these texts
    assert "names" in failed(_names_verdict(tmp_path, cfg, ["codex-playbook"], "Codex Playbook"))


def test_a_hard_exclude_entry_is_never_relaxed(tmp_path, cfg):
    strict = dataclasses.replace(cfg, hard_exclude=cfg.hard_exclude + ("claude",))
    assert failed(_names_verdict(tmp_path, strict, ["claude"], "Claude Code shipped it")) == ["names"]


def test_the_ledger_text_bar_agrees_with_the_gate(cfg):
    from daily import ledger as L
    relaxed = L._text_checks(cfg, (), None, [".claude"])
    assert relaxed("Claude Code wrote the fix") is None
    assert L._text_checks(cfg, (), None, ["claude-notebook"])("see claude notebook") == "blocked_name"
    strict = dataclasses.replace(cfg, hard_exclude=cfg.hard_exclude + ("claude",))
    assert L._text_checks(strict, (), None, [".claude"])("Claude Code wrote the fix") == "blocked_name"


def test_the_relaxed_set_is_derived_from_the_built_in_display_names():
    from daily import ledger as L
    assert L.HARNESS_WORDS == {"claude", "claude code", "codex"}
    assert not L.is_harness_word("claude-notebook") and not L.is_harness_word("code") and not L.is_harness_word("")
