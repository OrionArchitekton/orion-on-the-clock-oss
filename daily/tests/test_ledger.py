"""build_ledger: the allowlist, headline-only private lanes, quote eligibility, quote selection, session notes and
teasers. Synthetic fixtures only: build_ledger is pure."""
import dataclasses
import datetime as dt
import hashlib
import json

import pytest

from daily.config import Lane
from daily.ledger import (MAX_QUOTES, MAX_SESSIONS, MAX_TEASERS, LedgerError, RawSources, build_ledger,
                          resolve_view_id,
                          arc_ref, exclude_fragments, quote_id, writer_view)
from daily.sources.adapters import AdapterDay
from daily.sources.claude import UNRESOLVED, ClaudeDay, HarnessDay, QuoteCandidate, SessionNote
from daily.sources.codex import CodexDay
from daily.sources.tracker import ActiveArc, ArcClose
from daily.sources.github import PR
from daily.sources.transcripts import digest_fragments

DAY = dt.date(2026, 10, 16)
GEN = "2026-10-17T12:30:00Z"
SRC = "/work/2026-10-16/sources/github.jsonl"
GOOD = "The new browser pool shipped today and every single test passed."
OTHER = "The second pool landed cleanly and every check stayed green today."
COUNTERS = ("unlisted_lane", "hard_exclude", "embargo", "unapproved", "tainted", "fragment_in_text", "unknown_scope",
            "blocked_name", "prefiltered", "non_ascii", "path_or_url")
FRAGS = ("diary-app", "secret-garden")  # exclude_fragments of the conftest config
DIGEST = digest_fragments(FRAGS)


def pr(n, repo="owner/demo-tool", title="Add the demo", body="Body text", ts="2026-10-16T21:00:00Z", harness="codex"):
    return PR(repo, n, title, body, f"https://github.com/{repo}/pull/{n}", ts, harness)


def q(text=GOOD, own="demo-tool", repos=("demo-tool",), cwds=("/w/demo-tool",), cwd="/w/demo-tool",
      path="/t/s.jsonl", line=2, harness="claude_code", ts="2026-10-16T20:00:00Z", touched=False):
    return QuoteCandidate(harness=harness, repo=None, cwd=cwd, text=text, ts=ts, path=path, line=line, record="u1",
                          session_repos=tuple(repos), session_cwds=tuple(cwds), own_repo=own,
                          touches_excluded=touched)


def note(ref="aaaa0001", title="Shipping the browser pool", repos=("demo-tool",), cwds=("/w/demo-tool",),
         touched=False, ts="2026-10-16T20:00:00.000Z", harness="claude_code"):
    return SessionNote(harness, ref, title, tuple(repos), tuple(cwds), touched, ts)


def arc(arc_id="a1", title="Add the export button", repo="owner/demo-tool", harness="codex",
        ts="2026-10-16T20:00:00+00:00"):
    return ActiveArc(arc_id, title, repo, harness, ts)


def raw(prs=(), arcs=(), cq=(), xq=(), cn=(), xn=(), active=()):
    return RawSources(tuple(prs), tuple(arcs),
                      ClaudeDay(3, 7, tuple(cq), notes=tuple(cn), fragments_checked=2, fragments_digest=DIGEST),
                      CodexDay(2, 1, tuple(xq), notes=tuple(xn), fragments_checked=2, fragments_digest=DIGEST),
                      tuple(active))


def build(cfg, r, **kw):
    return build_ledger(DAY, r, cfg, GEN, SRC, **kw)


def ledger(cfg, r, **kw):
    return build(cfg, r, **kw)[0]


def aid(arc_id):
    """The ledger's opaque id for a tracked arc, derived independently of arc_ref."""
    return "arc:" + hashlib.sha256(arc_id.encode("utf-8")).hexdigest()[:8]


def counters(**hits):
    return {k: hits.get(k, 0) for k in COUNTERS}


# ---- items ----

def test_public_pr_becomes_a_full_item(cfg):  # positive control
    led, recs = build(cfg, raw(prs=[pr(7, body="x" * 2000)]))
    [item] = led["items"]
    assert item["id"] == "pr:demo-tool#7" and item["kind"] == "pr" and item["disclosure"] == "public"
    assert item["harness"] == "codex" and item["title"] == "Add the demo" and len(item["body"]) == 1200
    assert led["schema"] == "daily-build/ledger/v1" and led["day"] == "2026-10-16"
    assert led["tz"] == "America/Los_Angeles" and led["generated_at"] == GEN
    assert recs == []  # no quotable sentence, so no record


def test_headline_lane_is_only_label_why_and_count(cfg):
    secret_body = "The restore drill finished and every single file came back."
    led, recs = build(cfg, raw(prs=[pr(9, repo="owner/backup-box", title="SECRET-TITLE-PR", body=secret_body)],
                               arcs=[ArcClose("a1", "SECRET-TITLE-ARC", "backup-box", "codex",
                                              "2026-10-16T20:00:00+00:00")]))
    assert led["items"] == [{"id": "lane:backup-box", "kind": "headline", "lane": "backup-box",
                             "disclosure": "headline", "harness": "unknown", "label": "the offsite backup system",
                             "why": "so one dead disk never costs a week", "count": 2,
                             "source": {"records": ["pr:backup-box#9", "arc:a1"]}}]
    assert "SECRET-TITLE" not in json.dumps(led) and "restore drill" not in json.dumps(led)
    assert led["quotes"] == [] and recs == []


def test_unlisted_and_hard_excluded_are_dropped_and_counted(cfg):
    led = ledger(cfg, raw(prs=[pr(1, repo="owner/mystery-repo"), pr(2, repo="owner/diary-app"), pr(3)]))
    assert [i["id"] for i in led["items"]] == ["pr:demo-tool#3"]
    assert led["excluded"] == counters(unlisted_lane=1, hard_exclude=1)


def test_embargo_drops_items_and_quotes(cfg):
    talk = "We rehearsed the tree talk demo slides for an hour today."
    led = ledger(cfg, raw(prs=[pr(4, body="notes for the tree talk"), pr(5)], cq=[q(talk, line=1), q()]))
    assert [i["id"] for i in led["items"]] == ["pr:demo-tool#5"]
    assert [x["text"] for x in led["quotes"]] == [GOOD]
    assert led["excluded"] == counters(embargo=2)


def test_scoreboard_counts_all_work_but_never_hard_excluded_repos(cfg):
    """Hard-excluded repos (personal and client work) are never counted. Unlisted repos, headline lanes and arcs with
    no repo stay in the counts (numbers only); the session counts are the readers' own."""
    led = ledger(cfg, raw(prs=[pr(1), pr(2, repo="owner/mystery-repo"), pr(3, repo="owner/backup-box"),
                               pr(4, repo="owner/Diary-App.git"), pr(5, repo="owner/secret-garden")],
                          arcs=[ArcClose("a", "t", None, "codex", "2026-10-16T20:00:00+00:00"),
                                ArcClose("b", "t", "owner/secret-garden", "codex", "2026-10-16T20:00:00+00:00"),
                                ArcClose("c", "t", "owner/mystery-repo", "codex", "2026-10-16T20:00:00+00:00")]))
    assert led["scoreboard"] == {"prs_merged": 3, "arcs_closed": 2, "sessions": {"claude_code": 3, "codex": 2},
                                 "subagents": {"claude_code": 7, "codex": 1}}
    assert led["coverage"] == {"claude_code": "ok", "codex": "ok"}


def test_a_source_that_was_not_read_is_an_unknown_count_never_zero(cfg):
    """GitHub switched off, or no tracker configured: the count is None (unknown), which the writer is told never to
    call zero. A source that was read and found nothing is a real zero."""
    off = dataclasses.replace(raw(prs=[pr(1)], arcs=[ArcClose("a", "t", None, "codex", "2026-10-16T20:00:00+00:00")]),
                              prs_read=False, arcs_read=False)
    sb = ledger(cfg, off)["scoreboard"]
    assert sb["prs_merged"] is None and sb["arcs_closed"] is None
    sb = ledger(cfg, raw())["scoreboard"]
    assert sb["prs_merged"] == 0 and sb["arcs_closed"] == 0  # positive control: read, and nothing there


def test_items_newest_first_and_headline_last(cfg):
    led = ledger(cfg, raw(prs=[pr(1, ts="2026-10-16T15:00:00Z"), pr(2, ts="2026-10-17T01:00:00Z"),
                               pr(3, repo="owner/backup-box")],
                          arcs=[ArcClose("a9", "Arc title", "owner/demo-tool", "claude_code",
                                         "2026-10-16T20:00:00+00:00")]))
    assert [i["id"] for i in led["items"]] == ["pr:demo-tool#2", aid("a9"), "pr:demo-tool#1", "lane:backup-box"]


def test_a_quiet_day_has_every_section_empty_and_every_counter_zero(cfg):
    led, recs = build(cfg, raw())
    assert list(led) == ["schema", "day", "tz", "generated_at", "scoreboard", "coverage", "items", "quotes",
                         "sessions", "teasers", "excluded", "private_names", "diagnostics"]
    assert led["diagnostics"] == {} and led["private_names"] == []
    assert led["items"] == led["quotes"] == led["sessions"] == led["teasers"] == [] and recs == []
    assert led["excluded"] == counters()


# ---- refusing a ledger the exclude check never touched, or touched with another list ----

def side_day(side, quotes=(), notes=(), digest=DIGEST):
    cls = ClaudeDay if side == "claude_code" else CodexDay
    return cls(1, 0, tuple(quotes), notes=tuple(notes), fragments_checked=2, fragments_digest=digest)


def sides(side, day):
    other = "codex" if side == "claude_code" else "claude_code"
    days = {side: day, other: side_day(other)}
    return RawSources((), (), days["claude_code"], days["codex"])


def test_the_conftest_fragments():
    assert DIGEST == digest_fragments(("SECRET-GARDEN ", "diary-app", "diary-app"))  # a set: spelling, order, repeats


def test_exclude_fragments_of_the_conftest_config(cfg):
    assert exclude_fragments(cfg) == FRAGS


WRONG_DIGESTS = {"missing": "", "none": None, "partial": digest_fragments(("diary-app",)),
                 "superset": digest_fragments(FRAGS + ("other-thing",))}


@pytest.mark.parametrize("what", ["quotes", "notes"])
@pytest.mark.parametrize("digest", WRONG_DIGESTS.values(), ids=WRONG_DIGESTS.keys())
@pytest.mark.parametrize("side", ["claude_code", "codex"])
def test_refuses_a_reader_that_applied_another_fragment_list(cfg, side, digest, what):
    data = {"quotes": [q(harness=side)]} if what == "quotes" else {"notes": [note(harness=side)]}
    with pytest.raises(LedgerError, match=f"^exclude_not_applied:{side}$"):
        build(cfg, sides(side, side_day(side, digest=digest, **data)))


@pytest.mark.parametrize("side", ["claude_code", "codex"])
def test_a_plain_harness_day_with_quotes_is_refused(cfg, side):
    with pytest.raises(LedgerError, match=f"exclude_not_applied:{side}"):
        build(cfg, sides(side, HarnessDay(1, 0, (q(harness=side),))))


@pytest.mark.parametrize("side", ["claude_code", "codex"])
def test_a_reader_with_the_exact_list_passes(cfg, side):  # positive control for the refusal
    led = ledger(cfg, sides(side, side_day(side, quotes=[q(harness=side)], notes=[note(harness=side)])))
    assert [x["text"] for x in led["quotes"]] == [GOOD] and len(led["sessions"]) == 1


def test_a_wider_config_needs_the_wider_list(cfg):
    wider = dataclasses.replace(cfg, hard_exclude=("diary-app", "/w/secret-garden"))  # 3 fragments
    with pytest.raises(LedgerError, match="^exclude_not_applied:claude_code$"):
        build(wider, sides("claude_code", side_day("claude_code", notes=[note()])))
    full = digest_fragments(exclude_fragments(wider))
    led = ledger(wider, RawSources((), (), side_day("claude_code", notes=[note()], digest=full),
                                   side_day("codex", notes=[note(ref="bbbb0002", harness="codex")], digest=full)))
    assert len(led["sessions"]) == 2


def test_a_reader_that_saw_nothing_needs_no_check(cfg):
    led = ledger(cfg, RawSources((), (), side_day("claude_code", digest=""), HarnessDay(4, 2, ())))
    assert led["scoreboard"]["sessions"] == {"claude_code": 1, "codex": 4}


def test_no_hard_exclude_means_nothing_to_check(cfg):
    bare = dataclasses.replace(cfg, hard_exclude=())
    led = ledger(bare, sides("codex", side_day("codex", quotes=[q(harness="codex")], digest="")))
    assert [x["text"] for x in led["quotes"]] == [GOOD]


# ---- transcript quote eligibility v2, rules (a) to (f) ----

RULES = {
    "a_own_repo_unapproved": ({"own": "client-site"}, "unapproved"),
    "a_own_repo_hard_excluded": ({"own": "diary-app"}, "hard_exclude"),
    "a_own_cwd_under_an_excluded_folder": ({"cwd": "/w/secret-garden/demo-tool"}, "hard_exclude"),
    "b_session_repo_unlisted": ({"repos": ("demo-tool", "client-site")}, "tainted"),
    "b_session_repo_hard_excluded": ({"repos": ("demo-tool", "diary-app")}, "tainted"),
    "c_session_cwd_matches_a_fragment": ({"cwds": ("/w/demo-tool", "/home/me/Secret-Garden/notes")}, "tainted"),
    "c_session_scope_unknown": ({"cwds": ()}, "unknown_scope"),
    "d_session_touched_an_excluded_fragment": ({"touched": True}, "tainted"),
    "e_text_names_an_exclude_fragment": ({"text": "We moved the Secret-Garden notes and every test passed."},
                                         "fragment_in_text"),
    "e_text_hits_the_embargo": ({"text": "We rehearsed the tree talk demo slides for an hour today."}, "embargo"),
    "f_not_a_quotable_sentence": ({"text": "See `x` now and then again later today please."}, None),
}


@pytest.mark.parametrize("side", ["claude_code", "codex"])
@pytest.mark.parametrize("change,counter", RULES.values(), ids=RULES.keys())
def test_each_quote_rule_excludes_on_its_own(cfg, side, change, counter):
    variant = q(**{"text": OTHER, "line": 9, "harness": side, **change})
    r = raw(cq=[q(line=1)], xq=[variant]) if side == "codex" else raw(cq=[q(line=1), variant])
    led = ledger(cfg, r)
    assert [x["text"] for x in led["quotes"]] == [GOOD]  # the positive control beside it is admitted
    assert led["excluded"] == counters(**({counter: 1} if counter else {}))


def test_the_rule_variants_are_admitted_without_their_one_change(cfg):  # positive control for RULES
    led = ledger(cfg, raw(cq=[q(line=1)], xq=[q(text=OTHER, line=9, harness="codex")]))
    assert sorted(x["text"] for x in led["quotes"]) == sorted([GOOD, OTHER])
    assert led["excluded"] == counters()


def test_a_candidate_built_without_its_session_scope_is_unknown_scope(cfg):
    bare = QuoteCandidate("claude_code", None, "/w/demo-tool", OTHER, "2026-10-16T20:00:00Z", "/t/s.jsonl", 1, "u1")
    led = ledger(cfg, raw(cq=[bare, q()]))
    assert [x["text"] for x in led["quotes"]] == [GOOD] and led["excluded"] == counters(unknown_scope=1)


ADMITTED = {
    "own_repo_no_remote": ({"own": None}, "console", "console"),
    "own_repo_unresolved": ({"own": UNRESOLVED}, "console", "console"),
    "own_repo_headline_lane": ({"own": "backup-box", "repos": ("backup-box",)}, "backup-box", "headline"),
    "session_repos_hold_none_and_unresolved": ({"repos": (None, UNRESOLVED, "demo-tool")}, "demo-tool", "public"),
    "session_repos_hold_a_headline_lane": ({"repos": ("backup-box", "demo-tool")}, "demo-tool", "public"),
}


@pytest.mark.parametrize("change,lane,disclosure", ADMITTED.values(), ids=ADMITTED.keys())
def test_v2_admits_approved_and_console_scopes(cfg, change, lane, disclosure):
    [only] = ledger(cfg, raw(cq=[q(**change)]))["quotes"]
    assert (only["lane"], only["disclosure"], only["text"]) == (lane, disclosure, GOOD)


def test_a_transcript_quote_has_the_full_shape(cfg):
    [only] = ledger(cfg, raw(xq=[q(line=4, harness="codex", path="/t/r.jsonl")]))["quotes"]
    assert only == {"id": quote_id("codex", GOOD, "/t/r.jsonl", 4), "kind": "transcript", "harness": "codex",
                    "lane": "demo-tool", "disclosure": "public", "text": GOOD, "ts": "2026-10-16T20:00:00Z",
                    "source": {"path": "/t/r.jsonl", "line": 4, "record": "u1"}}
    assert only["id"].startswith("q:codex:") and len(only["id"]) == len("q:codex:") + 8


def test_exclude_fragments_hold_each_entry_and_its_repo_name(cfg):
    c = dataclasses.replace(cfg, hard_exclude=("Diary-App", "/home/me/Private-Notes/", "owner/Secret.git"))
    assert exclude_fragments(c) == ("diary-app", "/home/me/private-notes/", "private-notes", "owner/secret.git",
                                    "secret")


# ---- PR description quotes ----

BODY = ("## Summary\n"
        "The browser pool now leases one Chrome per identity.\n"
        "- run `make test`\n"
        "Every lease renews itself while the agent is still working.\n"
        + "filler " * 300 + "\n"
        "The last sentence sits past the item cut and still counts.")


def test_public_pr_body_sentences_become_pr_quotes(cfg):
    led, recs = build(cfg, raw(prs=[pr(7, body=BODY)]))
    assert recs == [{"repo": "owner/demo-tool", "number": 7, "harness": "codex", "closed_at": "2026-10-16T21:00:00Z",
                     "body": BODY}]
    assert [x["text"] for x in led["quotes"]] == ["The browser pool now leases one Chrome per identity.",
                                                  "Every lease renews itself while the agent is still working.",
                                                  "The last sentence sits past the item cut and still counts."]
    assert "The last sentence" not in led["items"][0]["body"]  # the item keeps 1200 chars, quotes use the full body
    for x in led["quotes"]:
        assert x["kind"] == "pr" and x["harness"] == "codex" and x["lane"] == "demo-tool"
        assert x["disclosure"] == "public"
        assert x["ts"] == "2026-10-16T21:00:00Z"
        assert x["source"] == {"path": SRC, "line": 1, "record": "pr:demo-tool#7"}
        assert x["id"] == quote_id("codex", x["text"], SRC, 1)
        assert x["text"] in recs[x["source"]["line"] - 1]["body"]  # what the gate re-checks


PR_BARS = {
    "headline_lane": {"repo": "owner/backup-box"},
    "unknown_harness": {"harness": "unknown"},
    "hard_excluded_lane": {"repo": "owner/diary-app"},
    "unlisted_lane": {"repo": "owner/mystery-repo"},
    "embargoed_pr": {"title": "Slides for the tree talk"},
}


@pytest.mark.parametrize("change", PR_BARS.values(), ids=PR_BARS.keys())
def test_each_pr_quote_bar_works_on_its_own(cfg, change):
    led, recs = build(cfg, raw(prs=[pr(1, body=GOOD, harness="claude_code"), pr(2, body=OTHER, **change)]))
    assert [x["text"] for x in led["quotes"]] == [GOOD]
    assert [(r["number"], r["harness"]) for r in recs] == [(1, "claude_code")]


def test_pr_records_hold_only_prs_that_gave_a_quote_in_line_order(cfg):  # positive control for PR_BARS
    led, recs = build(cfg, raw(prs=[pr(3, body="No sentence here"), pr(9, body=OTHER, harness="claude_code"),
                                    pr(5, body=GOOD)]))
    assert [r["number"] for r in recs] == [5, 9]
    lines = {x["text"]: x["source"]["line"] for x in led["quotes"]}
    assert lines == {GOOD: 1, OTHER: 2}
    for x in led["quotes"]:
        rec = recs[x["source"]["line"] - 1]
        assert x["source"]["record"] == f"pr:demo-tool#{rec['number']}" and x["text"] in rec["body"]


def test_pr_sentences_naming_an_exclude_fragment_are_dropped(cfg):
    body = f"{GOOD}\nWe copied the Diary-App export and every row matched."
    led, recs = build(cfg, raw(prs=[pr(6, body=body)]))
    assert [x["text"] for x in led["quotes"]] == [GOOD] and len(recs) == 1
    assert led["excluded"] == counters(fragment_in_text=2)  # the sentence, and the body line cut from the item
    assert led["items"][0]["body"] == GOOD


# ---- selection ----

def at(minutes):
    t = dt.datetime(2026, 10, 16, 12, 0, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=minutes)
    return t.isoformat().replace("+00:00", "Z")


def test_selection_round_robins_sessions_interleaving_harnesses(cfg):
    def session(name, harness, newest, n):
        return [q(text=f"Session {name} step {i} shipped and every test passed.", path=f"/t/{name}.jsonl",
                  harness=harness, line=i + 1, ts=at(newest - i)) for i in range(n)]

    # both Claude sessions are newer than the Codex one, so time order alone would read cone, ctwo, xone
    c1, c2 = session("cone", "claude_code", 500, 3), session("ctwo", "claude_code", 450, 2)
    x1 = session("xone", "codex", 400, 2)
    pr_text = "The codex branch landed the export and every check passed."
    led = ledger(cfg, raw(prs=[pr(8, body=pr_text, ts=at(100))], cq=list(reversed(c1 + c2)),
                          xq=list(reversed(x1))))
    expected = [c1[0], x1[0], c2[0], None, c1[1], x1[1], c2[1], c1[2]]  # groups: cone, xone, ctwo, the PR
    assert [x["text"] for x in led["quotes"]] == [e.text if e else pr_text for e in expected]


def test_quotes_cap_at_40_newest_first_within_a_session(cfg):
    many = [q(text=f"Quote number {i} shipped today and every test passed.", line=i, ts=at(i)) for i in range(50)]
    led = ledger(cfg, raw(cq=many))
    assert len(led["quotes"]) == MAX_QUOTES == 40
    assert [x["text"] for x in led["quotes"]] == [f"Quote number {i} shipped today and every test passed."
                                                  for i in range(49, 9, -1)]


def test_duplicate_texts_keep_only_the_first_pick(cfg):
    dup = "the new browser POOL   shipped today and every single test passed."
    led = ledger(cfg, raw(prs=[pr(2, body=GOOD, ts=at(10))],
                          cq=[q(line=1, ts=at(50)), q(line=7, ts=at(40)), q(OTHER, line=8, ts=at(30))],
                          xq=[q(dup, harness="codex", path="/t/x.jsonl", ts=at(45))]))
    assert [x["text"] for x in led["quotes"]] == [GOOD, OTHER]
    assert led["quotes"][0]["source"]["line"] == 1


# ---- ledger.sessions ----

NOTE_BARS = {
    "b_session_repo_unlisted": {"repos": ("demo-tool", "client-site")},
    "b_session_repo_hard_excluded": {"repos": ("demo-tool", "diary-app")},
    "c_session_cwd_matches_a_fragment": {"cwds": ("/w/demo-tool", "/w/Secret-Garden")},
    "c_session_scope_unknown": {"cwds": ()},
    "d_session_touched_an_excluded_fragment": {"touched": True},
    "empty_title": {"title": ""},
    "blank_title": {"title": "   "},
    "title_names_an_exclude_fragment": {"title": "Tidy the Diary-App backlog"},
    "title_hits_the_embargo": {"title": "Tree talk rehearsal"},
}


@pytest.mark.parametrize("change", NOTE_BARS.values(), ids=NOTE_BARS.keys())
def test_each_session_note_bar_works_on_its_own(cfg, change):
    led = ledger(cfg, raw(cn=[note()], xn=[note(ref="bbbb0002", harness="codex", **change)]))
    assert [s["ref"] for s in led["sessions"]] == ["aaaa0001"]


def test_sessions_project_four_fields_newest_first(cfg):  # positive control for NOTE_BARS
    led = ledger(cfg, raw(cn=[note(ts="2026-10-16T18:00:00.000Z"),
                              note(ref="cccc0003", title="Backups", repos=(None, UNRESOLVED, "backup-box"),
                                   ts="2026-10-16T22:00:00.000Z")],
                          xn=[note(ref="bbbb0002", harness="codex", ts="2026-10-16T20:00:00.000Z")]))
    assert led["sessions"] == [
        {"harness": "claude_code", "ref": "cccc0003", "title": "Backups", "last_ts": "2026-10-16T22:00:00.000Z"},
        {"harness": "codex", "ref": "bbbb0002", "title": "Shipping the browser pool",
         "last_ts": "2026-10-16T20:00:00.000Z"},
        {"harness": "claude_code", "ref": "aaaa0001", "title": "Shipping the browser pool",
         "last_ts": "2026-10-16T18:00:00.000Z"}]
    assert "/w/" not in json.dumps(led["sessions"]) and "backup-box" not in json.dumps(led["sessions"])


def test_sessions_cap_at_30_newest(cfg):
    notes = [note(ref=f"r{i:07d}", ts=at(i)) for i in range(35)]
    led = ledger(cfg, raw(cn=notes))
    assert len(led["sessions"]) == MAX_SESSIONS == 30
    assert [s["ref"] for s in led["sessions"]] == [f"r{i:07d}" for i in range(34, 4, -1)]


# ---- ledger.teasers ----

TEASER_BARS = {
    "headline_lane": {"repo": "owner/backup-box"},
    "unlisted_lane": {"repo": "owner/mystery-repo"},
    "no_repo": {"repo": None},
    "hard_excluded_lane": {"repo": "owner/diary-app"},
    "empty_title": {"title": ""},
    "title_hits_the_embargo": {"title": "Cut the tree talk demo"},
    "title_names_an_exclude_fragment": {"title": "Port the secret-garden importer"},
}


@pytest.mark.parametrize("change", TEASER_BARS.values(), ids=TEASER_BARS.keys())
def test_each_teaser_bar_works_on_its_own(cfg, change):
    led = ledger(cfg, raw(active=[arc("a1"), arc("a2", **{"title": "Wire the retry button", **change})]))
    assert led["teasers"] == [{"id": aid("a1"), "title": "Add the export button", "harness": "codex"}]


def test_teasers_newest_first_at_most_five(cfg):  # positive control for TEASER_BARS
    arcs = [arc(f"a{i}", title=f"Arc number {i}", harness="claude_code", ts=at(i)) for i in range(7)]
    led = ledger(cfg, raw(active=arcs))
    assert len(led["teasers"]) == MAX_TEASERS == 5
    assert led["teasers"][0] == {"id": aid("a6"), "title": "Arc number 6", "harness": "claude_code"}
    assert [t["id"] for t in led["teasers"]] == [aid("a6"), aid("a5"), aid("a4"), aid("a3"), aid("a2")]


# ---- prefilter: the writer only chooses text the gate can pass ----

def one_of_each(text):
    """A transcript quote, a PR quote, a note title and a teaser title, each holding text, beside a clean control of
    each kind."""
    sentence = f"We tuned {text} today and every single test passed."
    return raw(prs=[pr(1, body=GOOD), pr(2, body=sentence, ts="2026-10-16T22:00:00Z")],
               cq=[q(OTHER, line=1), q(sentence.replace("tuned", "fixed"), line=2)],
               cn=[note(), note(ref="bbbb0002", title=f"Tune {text}")],
               active=[arc("a1"), arc("a2", title=f"Tune {text}")])


def kept(led):
    return ([x["text"] for x in led["quotes"]], [s["ref"] for s in led["sessions"]],
            [t["id"] for t in led["teasers"]])


CLEAN = ([GOOD, OTHER], ["aaaa0001"], [aid("a1")])


@pytest.mark.parametrize("name", ["the Backup-Box restore", "the zephyr build", "Zephyrs"])
def test_text_naming_a_blocked_name_is_dropped_everywhere(cfg, name):
    led = ledger(cfg, one_of_each(name))
    assert sorted(kept(led)[0]) == sorted(CLEAN[0]) and kept(led)[1:] == CLEAN[1:]
    assert led["excluded"] == counters(blocked_name=5)  # 2 quotes, a note, a teaser and the PR's body line


def test_a_blocked_name_inside_a_word_is_not_a_hit(cfg):  # positive control: matching starts at a word
    led = ledger(cfg, one_of_each("the unzephyr pass"))
    assert len(led["quotes"]) == 4 and len(led["sessions"]) == 2 and len(led["teasers"]) == 2
    assert led["excluded"] == counters()


def test_text_ok_false_drops_each_kind_and_counts_it(cfg):
    seen = []

    def text_ok(s):
        seen.append(s)
        return "veto" not in s

    led = ledger(cfg, one_of_each("the veto path"), text_ok=text_ok)
    assert sorted(kept(led)[0]) == sorted(CLEAN[0]) and kept(led)[1:] == CLEAN[1:]
    assert led["excluded"] == counters(prefiltered=5)  # 2 quotes, a note, a teaser and the PR's body line
    assert {GOOD, OTHER, "Shipping the browser pool", "Add the export button", "Add the demo"} <= set(seen)


def test_text_ok_rejecting_everything_drops_every_checked_text(cfg):
    r = one_of_each("the plain path")
    led = ledger(cfg, dataclasses.replace(r, prs=r.prs + (pr(3, repo="owner/backup-box"),)), text_ok=lambda s: False)
    assert led["quotes"] == led["sessions"] == led["teasers"] == []
    assert [i["id"] for i in led["items"]] == ["lane:backup-box"]  # the operator's own label and reason are not prefiltered
    assert led["excluded"] == counters(prefiltered=8)  # 2 PR titles (so no body, no PR quote), 2 quotes, 2 notes,
    # 2 teasers


def test_text_ok_none_changes_nothing(cfg):
    r = one_of_each("the plain path")
    assert build(cfg, r, text_ok=None) == build(cfg, r) == build(cfg, r, text_ok=lambda s: True)
    assert len(ledger(cfg, r)["quotes"]) == 4


# ---- item text: the title decides the item, body lines are filtered (review item 1) ----

TITLE_BARS = {
    "blocked_name_any_separator": ("Wire the Backup Box restore", "blocked_name"),
    "blocked_name": ("Fix the zephyr build", "blocked_name"),
    "fragment_any_separator": ("Move the diary_app notes", "fragment_in_text"),
    "fragment_behind_a_zero_width_space": ("Move the diary\u200b-app notes", "fragment_in_text"),
    "homoglyph": ("Add the d\u0435mo export", "non_ascii"),
    "text_ok": ("Add the veto path", "prefiltered"),
}


@pytest.mark.parametrize("title,counter", TITLE_BARS.values(), ids=TITLE_BARS.keys())
def test_a_pr_title_the_gate_would_block_drops_the_item_and_its_quotes(cfg, title, counter):
    led, recs = build(cfg, raw(prs=[pr(1, body=GOOD), pr(2, title=title, body=OTHER)]),
                      text_ok=lambda s: "veto" not in s)
    assert [i["id"] for i in led["items"]] == ["pr:demo-tool#1"]
    assert [x["text"] for x in led["quotes"]] == [GOOD] and [r["number"] for r in recs] == [1]
    assert led["excluded"] == counters(**{counter: 1})


@pytest.mark.parametrize("title,counter", TITLE_BARS.values(), ids=TITLE_BARS.keys())
def test_an_arc_title_the_gate_would_block_drops_the_item(cfg, title, counter):
    arcs = [ArcClose("a1", "Close the export arc", "owner/demo-tool", "codex", "2026-10-16T20:00:00+00:00"),
            ArcClose("a2", title, "owner/demo-tool", "codex", "2026-10-16T19:00:00+00:00")]
    led = ledger(cfg, raw(arcs=arcs), text_ok=lambda s: "veto" not in s)
    assert [i["id"] for i in led["items"]] == [aid("a1")]
    assert led["excluded"] == counters(**{counter: 1})


def test_item_titles_are_kept_normalized(cfg):  # positive control: NFKC folds a ligature and full-width letters
    led = ledger(cfg, raw(prs=[pr(1, title="Add the \ufb01le export for \uff24emo")]))
    assert led["items"][0]["title"] == "Add the file export for Demo"


def test_a_pr_body_keeps_only_lines_the_gate_would_pass(cfg):
    hosts = dataclasses.replace(cfg, blocked_names=("Zephyr", "nimbus-01"))
    body = ("The export now streams rows.\n"
            "Deployed to Nimbus-01 last night.\n"
            "Zephyr wiring is done.\n"
            "Moved the diary app notes.\n"
            + "zephyr " * 300 + "\n"
            "\n"
            "Every test passed on the first run.")
    [item] = ledger(hosts, raw(prs=[pr(4, body=body)]))["items"]
    assert item["body"] == "The export now streams rows.\n\nEvery test passed on the first run."
    assert ledger(hosts, raw(prs=[pr(4, body=body)]))["excluded"] == counters(blocked_name=3, fragment_in_text=1)


# ---- separator-flexible matching (review item 2) ----

SPELLINGS = {
    "fragment_space": ("We parked the diary app work and every test passed today.", "fragment_in_text"),
    "fragment_underscore": ("We parked the diary_app work and every test passed today.", "fragment_in_text"),
    "fragment_case": ("We parked the Diary-App work and every test passed today.", "fragment_in_text"),
    "fragment_wide_gap": ("We parked the DIARY  -  APP work and every test passed today.", "fragment_in_text"),
    "blocked_space": ("We parked the backup box work and every test passed today.", "blocked_name"),
    "blocked_underscore": ("We parked the Backup_Box work and every test passed today.", "blocked_name"),
}


@pytest.mark.parametrize("text,counter", SPELLINGS.values(), ids=SPELLINGS.keys())
def test_fragments_and_blocked_names_match_across_separators(cfg, text, counter):
    led = ledger(cfg, raw(cq=[q(line=1), q(text, line=2)]))
    assert [x["text"] for x in led["quotes"]] == [GOOD] and led["excluded"] == counters(**{counter: 1})


# ---- NFKC and homoglyphs (review item 5) ----

def test_homoglyph_quotes_notes_and_teasers_are_dropped_and_counted(cfg):
    cyrillic = "The n\u0435w lease shipped today and every single test passed."
    led = ledger(cfg, raw(cq=[q(line=1), q(cyrillic, line=2)],
                          cn=[note(), note(ref="bbbb0002", title="Shipping the br\u043ewser pool")],
                          active=[arc("a1"), arc("a2", title="Add the \u0435xport button")]))
    assert kept(led) == ([GOOD], ["aaaa0001"], [aid("a1")])
    assert led["excluded"] == counters(non_ascii=3)


def test_a_zero_width_space_cannot_split_a_fragment_in_a_quote(cfg):
    hidden = "We moved the diary\u200b-app notes and every single test passed."
    led = ledger(cfg, raw(cq=[q(line=1), q(hidden, line=2)]))
    assert [x["text"] for x in led["quotes"]] == [GOOD] and led["excluded"] == counters(fragment_in_text=1)


def test_a_full_width_embargo_term_in_a_pr_body_drops_the_whole_pr(cfg):
    body = f"Rehearsed the \uff54\uff52\uff45\uff45 \uff54\uff41\uff4c\uff4b slides.\n{OTHER}"
    led, recs = build(cfg, raw(prs=[pr(1, body=GOOD), pr(2, body=body)]))
    assert [i["id"] for i in led["items"]] == ["pr:demo-tool#1"] and [r["number"] for r in recs] == [1]
    assert [x["text"] for x in led["quotes"]] == [GOOD] and led["excluded"] == counters(embargo=1)


def test_full_width_text_is_folded_before_the_checks(cfg):
    led = ledger(cfg, raw(cn=[note(title="\uff33hipping the pool")],
                          active=[arc("a1"), arc("a2", title="Wire the \uff5a\uff45\uff50\uff48\uff59\uff52 build")]))
    assert [s["title"] for s in led["sessions"]] == ["Shipping the pool"]  # kept, in its folded form
    assert [t["id"] for t in led["teasers"]] == [aid("a1")] and led["excluded"] == counters(blocked_name=1)


# ---- writer_view: the only thing the writer is sent (review item 3) ----

def test_writer_view_strips_sources_urls_and_private_lane_names(cfg):
    two = dataclasses.replace(cfg, lanes={**cfg.lanes, "attic-vault": Lane("attic-vault", "headline",
                                                                             "the attic archive", "so old work stays")})
    r = raw(prs=[pr(7, body=GOOD), pr(8, repo="owner/attic-vault"), pr(9, repo="owner/backup-box")],
            arcs=[ArcClose("a5", "Close the export arc", "owner/demo-tool", "codex", "2026-10-16T19:00:00+00:00")],
            cq=[q(OTHER, own="backup-box", repos=("backup-box",), line=4),
                q("The console helper printed every lease twice today.", own=None, line=5)],
            xq=[q("The codex lane merged the export flag and the tests stayed green.", harness="codex",
                  path="/t/x.jsonl")],
            cn=[note()], xn=[note(ref="bbbb0002", harness="codex")], active=[arc()])
    led = ledger(two, r)
    assert {i["kind"] for i in led["items"]} == {"pr", "arc", "headline"}
    assert {(x["kind"], x["disclosure"]) for x in led["quotes"]} == {("pr", "public"), ("transcript", "public"),
                                                                     ("transcript", "headline"),
                                                                     ("transcript", "console")}
    assert led["sessions"] and led["teasers"]
    before = json.dumps(led)
    view = writer_view(led)
    assert json.dumps(led) == before  # the ledger itself is untouched
    assert (view["schema"], view["tz"]) == ("daily-build/ledger/v1", "America/Los_Angeles")  # the only "/" allowed
    dumped = json.dumps({k: v for k, v in view.items() if k not in ("schema", "tz")}).lower()
    for name in two.names_to_block():
        assert name.lower() not in dumped
    assert "/" not in dumped and "http" not in dumped and '"source"' not in dumped and '"url"' not in dumped
    assert all("source" in i for i in led["items"]) and not any("source" in i for i in view["items"])
    assert "private_names" in led and "private_names" not in view and "attic-vault" not in dumped
    heads = [(i["id"], i["lane"], i["label"]) for i in view["items"] if i["kind"] == "headline"]
    assert heads == [("lane:private-1", "private-1", "the attic archive"),
                     ("lane:private-2", "private-2", "the offsite backup system")]
    by_text = {x["text"]: x for x in view["quotes"]}
    assert by_text[OTHER]["lane"] == "private-2"
    assert by_text["The console helper printed every lease twice today."]["lane"] == "console"
    assert all(set(x) == {"id", "kind", "harness", "lane", "text", "ts"} for x in view["quotes"])
    assert [x["id"] for x in view["quotes"]] == [x["id"] for x in led["quotes"]]
    assert view["sessions"] == led["sessions"] and view["teasers"] == led["teasers"]


# ---- opaque arc ids: a tracker's arc id is free text ----

def test_arc_ref_is_arc_plus_8_hex_of_sha256():
    assert arc_ref("a1") == aid("a1") and len(arc_ref("a1")) == len("arc:") + 8
    assert arc_ref("a1") != arc_ref("a2")


def test_a_tracker_arc_id_naming_a_blocked_name_never_reaches_the_ledger_or_the_writer(cfg):
    leaky = "port-diary-app-to-backup-box"
    closed = [ArcClose(leaky, "Port the importer", "owner/demo-tool", "codex", "2026-10-16T20:00:00+00:00"),
              ArcClose("zephyr-cleanup", "Tidy the exporter", "owner/demo-tool", "codex", "2026-10-16T19:00:00+00:00")]
    led = ledger(cfg, raw(arcs=closed, active=[arc(leaky, title="Wire the importer"),
                                               arc("zephyr-next", title="Wire the exporter")]))
    assert [i["id"] for i in led["items"]] == [aid(leaky), aid("zephyr-cleanup")]  # positive control: both kept
    assert {t["id"] for t in led["teasers"]} == {aid(leaky), aid("zephyr-next")}
    assert [i["source"] for i in led["items"]] == [{"record": "arc:" + leaky}, {"record": "arc:zephyr-cleanup"}]
    sourceless = {**led, "items": [{k: v for k, v in i.items() if k != "source"} for i in led["items"]]}
    for dumped in (json.dumps(sourceless).lower(), json.dumps(writer_view(led)).lower()):  # the raw id: source only
        for name in [*cfg.names_to_block(), leaky, "zephyr-cleanup", "zephyr-next"]:
            assert name.lower() not in dumped


def test_distinct_arcs_keep_distinct_ids(cfg):
    closed = [ArcClose(f"arc-{i}", f"Close part {i}", "owner/demo-tool", "codex", "2026-10-16T20:00:00+00:00")
              for i in range(50)]
    led = ledger(cfg, raw(arcs=closed, active=[arc(f"arc-{i}", title=f"Open part {i}") for i in range(5)]))
    ids = [i["id"] for i in led["items"]]
    assert len(ids) == len(set(ids)) == 50 and set(ids) == {aid(f"arc-{i}") for i in range(50)}
    assert len({t["id"] for t in led["teasers"]}) == 5


# ---- paths and URLs never reach the writer ----

PATHY = {
    "home": "Notes from /home/someone/src today",
    "tilde": "Edit ~/notes before the run",
    "mnt": "Copied it to /mnt/c for the backup",
    "tmp": "Left the dump in /tmp/dump overnight",
    "users": "Synced /Users/someone with the laptop",
    "users_lowercase": "Synced /users/someone with the laptop",
    "windows_drive": "Opened C:\\Work in the editor",
    "windows_drive_forward": "Opened d:/work in the editor",
    "http": "See http://example.org for the notes",
    "https": "See https://example.org for the notes",
    "www": "See www.example.org for the notes",
    "two_slash_token": "Moved production/formats/daily into place",
    "full_width_slashes": "Moved src\uff0fdaily\uff0fledger into place",
    "github_any_case": "Pushed it to GITHUB.COM/owner after lunch",
    "git_remote": "Added git@example-host:repo as the remote",
    "backslash_path": "Fixed the src\\daily loader",
    "unc_path": "Mapped \\\\fileserver\\share for the run",
    # the gate's URL shapes, the one path rule
    "scheme_without_slashes": "Pointed the hook at https:example.org",
    "javascript_scheme": "Blocked javascript:void in the page",
    "domain_with_a_path": "Ported the node.js/react helpers",
    "domain_port_path": "Moved it to example.org:8080/admin",
    "ipv4_path": "Served it from 203.0.113.5/admin",
    "defanged_dot": "Logged example[.]org as a bad host",
    "ideographic_dot": "Moved it to example" + chr(0x3002) + "org/x today",
    "mailto_scheme": "Wrote to mailto:admin@example.org",
    "ssh_scheme": "Cloned ssh:example-host over the tunnel",
}


def every_entry(text):
    """One of each writer-bound titled entry holding text, beside a clean control of each kind, plus a PR whose body
    has that text on one line between two clean lines."""
    return raw(prs=[pr(1, title="Add the demo"), pr(2, title=text, ts="2026-10-16T22:00:00Z"),
                    pr(3, title="Tidy the docs", body=f"First clean line.\n{text}\nLast clean line.",
                       ts="2026-10-16T20:00:00Z")],
               arcs=[ArcClose("c1", "Close the export arc", "owner/demo-tool", "codex", "2026-10-16T19:00:00+00:00"),
                     ArcClose("c2", text, "owner/demo-tool", "codex", "2026-10-16T18:00:00+00:00")],
               cn=[note(), note(ref="bbbb0002", title=text)],
               active=[arc("a1"), arc("a2", title=text)])


@pytest.mark.parametrize("text", PATHY.values(), ids=PATHY.keys())
def test_path_or_url_is_the_public_path_rule(text):
    from daily import ledger as ledger_module
    assert ledger_module.path_or_url(text) is True  # it reads the raw text and its normal form itself
    assert ledger_module.path_or_url("The pool shipped today, 24/7 and a/b tested.") is False  # control


@pytest.mark.parametrize("text", PATHY.values(), ids=PATHY.keys())
def test_a_path_or_url_drops_titled_entries_and_body_lines(cfg, text):
    led = ledger(cfg, every_entry(text))
    assert [i["id"] for i in led["items"]] == ["pr:demo-tool#1", "pr:demo-tool#3", aid("c1")]
    assert led["items"][1]["body"] == "First clean line.\nLast clean line."
    assert [s["ref"] for s in led["sessions"]] == ["aaaa0001"] and [x["id"] for x in led["teasers"]] == [aid("a1")]
    assert led["excluded"] == counters(path_or_url=5)  # PR title, body line, arc title, note title, teaser title


@pytest.mark.parametrize("text", ["Ran a/b testing on the pool", "The pool now runs 24/7 without a restart",
                                  "Wrote the and/or rule down", "Talked about GitHub actions with the git team",
                                  "Updated config.py defaults", "Mailed admin at example about it",
                                  "Read the data: every row came back", "Shipped one file: the config",
                                  "Moved example.com to the new host"])
def test_a_single_slash_stays(cfg, text):  # positive control for PATHY: a colon in prose, a bare host name
    led = ledger(cfg, every_entry(text))
    assert len(led["items"]) == 5 and len(led["sessions"]) == 2 and len(led["teasers"]) == 2
    assert led["items"][2]["body"] == f"First clean line.\n{text}\nLast clean line."
    assert led["excluded"] == counters()


@pytest.mark.parametrize("text", ["The pool shipped from the daily folder of the repo.",
                                  "We moved production/formats/daily and every single test passed.",
                                  "Read https://example.org for the notes we wrote today.",
                                  "Read www.example.org for the notes we wrote today please.",
                                  "The pool runs 24/7 now and every single test passed."])
def test_quote_ok_already_keeps_slashes_and_urls_out_of_quotes(cfg, text):
    slash_or_url = "/" in text or "www." in text
    led = ledger(cfg, raw(cq=[q(text, line=1)]))
    assert [x["text"] for x in led["quotes"]] == ([] if slash_or_url else [text])
    assert led["excluded"] == counters()  # quote_ok drops it, uncounted: no second path rule for quotes
    led, recs = build(cfg, raw(prs=[pr(5, body=text, harness="claude_code")]))
    assert [x["text"] for x in led["quotes"]] == ([] if slash_or_url else [text])
    assert len(recs) == (0 if slash_or_url else 1)


# ---- the writer names a project by its label ----

def test_every_public_item_carries_its_lane_label_and_why(cfg):
    two = dataclasses.replace(cfg, lanes={**cfg.lanes, "pool-kit": Lane("pool-kit", "public", "the browser pool",
                                                                          "so agents share signed-in browsers")})
    led = ledger(two, raw(prs=[pr(1), pr(2, repo="owner/pool-kit"), pr(3, repo="owner/backup-box")],
                          arcs=[ArcClose("c1", "Close the export arc", "owner/demo-tool", "codex",
                                         "2026-10-16T19:00:00+00:00"),
                                ArcClose("c2", "Close the lease arc", "owner/pool-kit", "codex",
                                         "2026-10-16T18:00:00+00:00")]))
    view = writer_view(led)
    public = [i for i in view["items"] if i["kind"] in ("pr", "arc")]
    assert len(public) == 4 and all(i["label"].strip() and i["why"].strip() for i in public)
    assert {(i["id"], i["lane"], i["label"], i["why"]) for i in public} == {
        ("pr:demo-tool#1", "demo-tool", "the demo tool", "so every demo runs itself"),
        (aid("c1"), "demo-tool", "the demo tool", "so every demo runs itself"),
        ("pr:pool-kit#2", "pool-kit", "the browser pool", "so agents share signed-in browsers"),
        (aid("c2"), "pool-kit", "the browser pool", "so agents share signed-in browsers")}
    [head] = [i for i in view["items"] if i["kind"] == "headline"]  # unchanged: opaque id, label and why
    assert (head["id"], head["label"]) == ("lane:private-1", "the offsite backup system")


# ---- purity and what reaches the writer ----

def test_plain_harness_days_and_no_active_arcs_are_accepted(cfg):
    led, recs = build_ledger(DAY, RawSources((), (), HarnessDay(1, 0, ()), HarnessDay(0, 0, ())), cfg, GEN, SRC)
    assert led["sessions"] == led["teasers"] == led["quotes"] == [] and recs == []


def test_build_ledger_is_pure(cfg, tmp_path):
    src = tmp_path / "work" / "sources" / "github.jsonl"
    r = raw(prs=[pr(7, body=GOOD)], cq=[q(OTHER)], cn=[note()], active=[arc()])
    first = build_ledger(DAY, r, cfg, GEN, str(src))
    assert build_ledger(DAY, r, cfg, GEN, str(src)) == first
    assert not (tmp_path / "work").exists()


def test_only_source_paths_carry_a_path(cfg):
    led, _ = build(cfg, raw(prs=[pr(7, body=GOOD)], cq=[q(OTHER)], cn=[note()], active=[arc()]))
    assert {x["source"]["path"] for x in led["quotes"]} == {SRC, "/t/s.jsonl"}
    for x in led["quotes"]:
        del x["source"]
    dumped = json.dumps(led)
    assert "/w/" not in dumped and "/t/" not in dumped and SRC not in dumped


# ---- every harness, coverage and diagnostics: built-ins first, then the operator's adapters ----

HARNESSES = ("claude_code", "codex", "alpha_agent", "beta_agent")


def everyone(**kw):
    others = kw.pop("others", {"alpha_agent": AdapterDay(26, 4, ()), "beta_agent": AdapterDay(2, 0, ())})
    r = raw(prs=[pr(7, body=GOOD)], cq=[q(OTHER)], cn=[note()])
    return dataclasses.replace(r, others=others, **kw)


def test_every_harness_reaches_the_scoreboard(cfg):
    led = ledger(cfg, everyone())
    assert led["scoreboard"]["sessions"] == {"claude_code": 3, "codex": 2, "alpha_agent": 26, "beta_agent": 2}
    assert led["scoreboard"]["subagents"] == {"claude_code": 7, "codex": 1, "alpha_agent": 4, "beta_agent": 0}
    assert "handoffs" not in led["scoreboard"]
    assert list(led["coverage"].items()) == [(h, "ok") for h in HARNESSES]


def test_coverage_keeps_built_ins_first_then_the_configured_order(cfg):
    given = {"beta_agent": "unavailable:bad_adapter_output", "alpha_agent": "ok"}
    led = ledger(cfg, everyone(others={"alpha_agent": AdapterDay(1, 0, ()), "beta_agent": None}, coverage=given))
    assert list(led["coverage"].items()) == [("claude_code", "ok"), ("codex", "ok"), ("alpha_agent", "ok"),
                                             ("beta_agent", "unavailable:bad_adapter_output")]


def test_an_adapter_cannot_take_a_built_in_id(cfg):
    led = ledger(cfg, everyone(others={"codex": AdapterDay(99, 99, ())}))
    assert led["scoreboard"]["sessions"]["codex"] == 2  # the transcript reader's own count, not the adapter's


@pytest.mark.parametrize("harness", HARNESSES)
def test_an_unavailable_harness_is_in_no_map(cfg, harness):
    others = {"alpha_agent": AdapterDay(26, 4, ()), "beta_agent": AdapterDay(2, 0, ())}
    if harness in others:
        others[harness] = None
        r = everyone(others=others, coverage={harness: "unavailable:boom"})
    else:
        field = {"claude_code": "claude", "codex": "codex"}[harness]
        r = everyone(**{field: None, "coverage": {harness: "unavailable:boom"}})
    led = ledger(cfg, r)
    sb = led["scoreboard"]
    assert led["coverage"][harness] == "unavailable:boom"
    assert harness not in sb["sessions"] and harness not in sb["subagents"]
    rest = [h for h in HARNESSES if h != harness]
    assert all(led["coverage"][h] == "ok" for h in rest)  # positive control: the rest still count
    assert len(sb["sessions"]) == 3


def test_an_unavailable_transcript_reader_gives_no_quotes_notes_or_refusal(cfg):
    """Coverage decides, not the object: a day passed for an unavailable reader is ignored, so its missing exclude
    digest is no reason to refuse, and none of its quotes or notes reach the ledger."""
    stale = HarnessDay(1, 0, (q(GOOD, line=9),))  # no digest: would be refused if it were used
    led = ledger(cfg, dataclasses.replace(raw(xq=[q(OTHER, harness="codex", path="/t/x.jsonl")],
                                              xn=[note(ref="bbbb0002", harness="codex")]),
                                          claude=stale, coverage={"claude_code": "unavailable:empty_root"}))
    assert [x["text"] for x in led["quotes"]] == [OTHER] and [s["harness"] for s in led["sessions"]] == ["codex"]
    assert "claude_code" not in led["scoreboard"]["sessions"]
    with pytest.raises(LedgerError, match="^exclude_not_applied:claude_code$"):  # positive control: used, refused
        ledger(cfg, dataclasses.replace(raw(), claude=stale))


def test_a_none_reader_is_an_empty_day(cfg):
    led = ledger(cfg, RawSources((), (), None, CodexDay(2, 1, (q(harness="codex"),), fragments_checked=2,
                                                             fragments_digest=DIGEST),
                                 coverage={"claude_code": "unavailable:root_missing"}))
    assert led["scoreboard"]["sessions"] == {"codex": 2} and [x["text"] for x in led["quotes"]] == [GOOD]


def test_a_switched_off_reader_is_untried_not_unavailable(cfg):
    led = ledger(cfg, RawSources((), (), None, CodexDay(2, 1, (), fragments_checked=2, fragments_digest=DIGEST)))
    assert led["coverage"] == {"codex": "ok"} and led["scoreboard"]["sessions"] == {"codex": 2}


def test_ok_coverage_without_data_is_not_ok(cfg):
    led = ledger(cfg, everyone(others={"alpha_agent": None}, coverage={"alpha_agent": "ok"}))
    assert led["coverage"]["alpha_agent"] == "unavailable:no_data"
    assert "alpha_agent" not in led["scoreboard"]["sessions"]


def test_counts_only_readers_never_give_quotes(cfg):
    said = "The helper cleared every queue before noon and nothing broke today."
    led = ledger(cfg, everyone(others={"alpha_agent": HarnessDay(1, 0, (q(said, line=5),))}))
    assert said not in [x["text"] for x in led["quotes"]] and len(led["quotes"]) == 2
    led = ledger(cfg, dataclasses.replace(everyone(), claude=dataclasses.replace(everyone().claude, quotes=(q(said),))))
    assert said in [x["text"] for x in led["quotes"]]  # positive control: from a transcript reader it is admitted


def test_diagnostics_pass_through(cfg):
    diag = {"github": {"lookups": 3, "failed_lookups": 1}, "tracker": {"skipped": 2, "active_skipped": 0}}
    led = ledger(cfg, everyone(diagnostics=diag))
    assert led["diagnostics"] == diag and led["diagnostics"] is not diag


def test_writer_view_whitelists_keys_and_drops_diagnostics_and_coverage_reasons(cfg):
    led = ledger(cfg, everyone(others={"alpha_agent": None, "beta_agent": AdapterDay(2, 0, ())},
                               coverage={"alpha_agent": "unavailable:TimeoutExpired"},
                               diagnostics={"github": {"lookups": 3, "failed_lookups": 1}}))
    led["smuggled"] = "a key build_ledger never writes"
    view = writer_view(led)
    assert list(view) == ["schema", "day", "tz", "generated_at", "scoreboard", "coverage", "items", "quotes",
                          "sessions", "teasers", "excluded"]
    assert view["coverage"] == {"claude_code": "ok", "codex": "ok", "alpha_agent": "unavailable", "beta_agent": "ok"}
    dumped = json.dumps(view)
    assert "TimeoutExpired" not in dumped and "lookups" not in dumped and "smuggled" not in dumped
    assert view["scoreboard"] == led["scoreboard"] and view["items"][0]["title"] == led["items"][0]["title"]


# ---- resolve_view_id: a story id the writer copied from the view, back to the ledger id ----

def _two_headline_ledger(cfg):
    two = dataclasses.replace(cfg, lanes={**cfg.lanes, "attic-vault": Lane("attic-vault", "headline",
                                                                             "the attic archive", "so old work stays")})
    r = raw(prs=[pr(7, body=GOOD), pr(8, repo="owner/attic-vault"), pr(9, repo="owner/backup-box")],
            cq=[q(OTHER, own="backup-box", repos=("backup-box",), line=4)], cn=[note()])
    return ledger(two, r)


def test_every_story_id_the_view_shows_resolves_to_its_ledger_item(cfg):
    led = _two_headline_ledger(cfg)
    view = writer_view(led)
    got = {i["id"]: resolve_view_id(led, i["id"]) for i in view["items"]}
    assert got == {"pr:demo-tool#7": "pr:demo-tool#7", "lane:private-1": "lane:attic-vault",
                   "lane:private-2": "lane:backup-box"}
    assert all(resolve_view_id(led, i["id"]) in {x["id"] for x in led["items"]} for i in view["items"])


def test_a_raw_ledger_item_id_still_resolves(cfg):  # positive control
    led = _two_headline_ledger(cfg)
    assert [resolve_view_id(led, i["id"]) for i in led["items"]] == [i["id"] for i in led["items"]]


@pytest.mark.parametrize("vid", ["lane:private-3", "lane:private-0", "lane:private-", "private-1", "pr:demo-tool#99",
                                 "", None, 7, ["lane:private-1"]])
def test_an_unknown_id_or_alias_resolves_to_none(cfg, vid):
    assert resolve_view_id(_two_headline_ledger(cfg), vid) is None


def test_a_headline_alias_from_a_quote_alone_has_no_item_to_resolve_to(cfg):
    """backup-box appears only through a quote here: its alias is real in the view but names no ledger item."""
    led = ledger(cfg, raw(prs=[pr(7, body=GOOD)], cq=[q(OTHER, own="backup-box", repos=("backup-box",), line=4)]))
    assert any(x["lane"] == "private-1" for x in writer_view(led)["quotes"])
    assert resolve_view_id(led, "lane:private-1") is None


def test_session_refs_resolve_only_when_the_ledger_holds_the_note(cfg):
    led = _two_headline_ledger(cfg)
    ref = led["sessions"][0]["ref"]
    assert resolve_view_id(led, f"session:{ref}") == f"session:{ref}"
    for bad in ("session:ffffffff", "session:", f"session:{ref}x", ref):
        assert resolve_view_id(led, bad) is None


def test_an_alias_that_is_also_another_raw_id_is_ambiguous(cfg):
    """A headline lane literally named private-1 sorts after attic-vault, whose alias is private-1: refuse to guess."""
    lanes = {**cfg.lanes, "attic-vault": Lane("attic-vault", "headline", "the attic archive", "so old work stays"),
             "private-1": Lane("private-1", "headline", "a lane named oddly", "to test the alias")}
    led = ledger(dataclasses.replace(cfg, lanes=lanes),
                 raw(prs=[pr(8, repo="owner/attic-vault"), pr(9, repo="owner/private-1")]))
    assert resolve_view_id(led, "lane:private-2") == "lane:private-1"  # positive control: unambiguous alias
    assert resolve_view_id(led, "lane:private-1") is None


def test_arc_titles_lose_backticks_but_session_and_teaser_titles_stay_strict(cfg):
    from daily import gate
    r = raw(arcs=[ArcClose("a5", "Close the `export` arc", "owner/demo-tool", "codex", "2026-10-16T19:00:00+00:00")],
            cn=[note(title="Ship the `foo` flag"), note(ref="aaaa0002", title="Ship the foo flag")],
            active=[arc(title="Add the `export` button"), arc(arc_id="a2", title="Add the export button")])
    led = ledger(cfg, r, text_ok=lambda t: not gate.code_shapes(t))  # the collect stage's no_code half
    assert [i["title"] for i in led["items"]] == ["Close the export arc"]
    assert [s["title"] for s in led["sessions"]] == ["Ship the foo flag"]  # the backtick title is dropped
    assert [t["title"] for t in led["teasers"]] == ["Add the export button"]
    assert led["excluded"]["prefiltered"] == 2


@pytest.mark.parametrize("title", ["Ship the BackupBox sync", "Ship the backupbox sync", "Ship Zeph-yr sync"])
def test_the_ledger_matches_blocked_names_like_the_gate(cfg, title):
    led = ledger(cfg, raw(prs=[pr(7, title=title), pr(8, title="Ship the backup plan")]))  # 8: positive control
    assert [i["title"] for i in led["items"]] == ["Ship the backup plan"]
    assert led["excluded"]["blocked_name"] == 1


# ---- accented config terms: both spellings are dropped at collect ----

JOSE = "Jos" + chr(0xE9) + " Ram" + chr(0xED) + "rez"
ZOE = "Zo" + chr(0xEB)
CAFE = "caf" + chr(0xE9)


def test_accented_config_terms_drop_both_spellings_at_collect(cfg):
    accented = dataclasses.replace(cfg, blocked_names=("Zephyr", ZOE), embargo_terms=("tree talk", JOSE),
                                   hard_exclude=cfg.hard_exclude + (CAFE + "-notes",))
    prs = [pr(1, title="Met " + JOSE + " about the pool"), pr(2, title="Met Jose Ramirez about the pool"),
           pr(3, title=ZOE + " reviewed the pool"), pr(4, title="Zoe reviewed the pool"),
           pr(5, title="Moved the " + CAFE + "-notes folder"), pr(6, title="Moved the cafe-notes folder"),
           pr(7, title="Joseph reviewed the cafe menu")]  # 7: positive control
    led = ledger(accented, raw(prs=prs))
    assert [i["id"] for i in led["items"]] == ["pr:demo-tool#7"]
    assert led["excluded"] == counters(embargo=2, blocked_name=2, fragment_in_text=2)  # accents are no non_ascii


def test_ascii_spellings_of_accented_terms_drop_quotes_at_collect(cfg):
    accented = dataclasses.replace(cfg, blocked_names=("Zephyr", ZOE), embargo_terms=("tree talk", JOSE))
    quotes = [q("We talked to Jose Ramirez about the pool and it shipped.", line=1),
              q("Zoe said the pool shipped and every single test passed.", line=2), q(line=3)]
    led = ledger(accented, raw(cq=quotes))
    assert [x["text"] for x in led["quotes"]] == [GOOD]  # positive control kept
    assert led["excluded"] == counters(embargo=1, blocked_name=1)


# ---- one normalization: collect reads the same two forms as the gate ----

def test_a_combining_mark_inside_an_embargo_term_drops_the_quote_at_collect(cfg):
    marked = "We rehearsed the tre" + chr(0x332) + "e talk again this morning, all of it."
    led = ledger(cfg, raw(cq=[q(line=1), q(marked, line=2)]))
    assert [x["text"] for x in led["quotes"]] == [GOOD]  # one quote lost, never passed on to fail the gate
    assert led["excluded"] == counters(embargo=1)


def test_a_combining_mark_inside_a_blocked_name_drops_the_title_at_collect(cfg):
    led = ledger(cfg, raw(prs=[pr(1, title="Fix the Zep" + chr(0x332) + "hyr build"), pr(2)]))
    assert [i["id"] for i in led["items"]] == ["pr:demo-tool#2"] and led["excluded"] == counters(blocked_name=1)


def test_an_accented_title_is_kept_in_the_normal_form_the_gate_reads(cfg):
    led = ledger(cfg, raw(prs=[pr(1, title="Fixed the caf" + chr(0xE9) + " menu and the re" + chr(0x301) + "sume page")],
                          cn=[note(title="Tre" + chr(0x332) + "ating the na" + chr(0xEF) + "ve export")]))
    assert [i["title"] for i in led["items"]] == ["Fixed the cafe menu and the resume page"]
    assert [s["title"] for s in led["sessions"]] == ["Treating the naive export"]
    assert led["excluded"] == counters()



def test_quote_ok_uses_the_one_path_rule(cfg):
    blocked = "We blocked javascript:void on the page and the run finished well today."
    prose = "We looked at the data: every run finished on time and the pool held up."
    led = ledger(cfg, raw(cq=[q(blocked, line=1), q(prose, line=2)]))
    assert [x["text"] for x in led["quotes"]] == [prose]  # a colon in prose is no scheme



# ---- pipeline findings on PR 15 ----

@pytest.mark.parametrize("cwds", [("",), ("", "/w/demo-tool"), ("   ",), ("/w/demo-tool", "\t")],
                         ids=["blank", "blank_and_public", "spaces", "tab"])
def test_a_blank_session_directory_is_an_unknown_scope(cfg, cwds):  # #0
    led = ledger(cfg, raw(cq=[q(line=1), q(OTHER, own=None, repos=(None,), cwds=cwds, cwd="", line=2)],
                          cn=[note(), note(ref="bbbb0002", cwds=cwds)]))
    assert [x["text"] for x in led["quotes"]] == [GOOD] and [n["ref"] for n in led["sessions"]] == ["aaaa0001"]
    assert led["excluded"] == counters(unknown_scope=1)


def test_every_item_carries_its_source_record(cfg):  # #3
    led = ledger(cfg, raw(prs=[pr(7), pr(8, repo="owner/backup-box")],
                          arcs=[ArcClose("raw-arc-1", "Close the export arc", "owner/demo-tool", "codex",
                                         "2026-10-16T19:00:00+00:00"),
                                ArcClose("raw-arc-2", "Close the restore arc", "owner/backup-box", "codex",
                                         "2026-10-16T18:00:00+00:00")]))
    by_id = {i["id"]: i for i in led["items"]}
    assert by_id["pr:demo-tool#7"]["source"] == {"record": "pr:demo-tool#7",
                                                  "url": "https://github.com/owner/demo-tool/pull/7"}
    assert by_id[aid("raw-arc-1")]["source"] == {"record": "arc:raw-arc-1"}
    assert by_id["lane:backup-box"]["source"] == {"records": ["pr:backup-box#8", "arc:raw-arc-2"]}
    assert by_id["lane:backup-box"]["count"] == 2
    view = json.dumps(writer_view(led))
    assert '"source"' not in view and "raw-arc" not in view and "pull/7" not in view


def test_private_names_hold_every_repo_seen_that_is_not_a_public_lane(cfg):  # #2
    led = ledger(cfg, raw(prs=[pr(1), pr(2, repo="owner/Mystery-Repo.git"), pr(3, repo="owner/backup-box"),
                               pr(4, repo="owner/diary-app")],
                          arcs=[ArcClose("a1", "Close it", "owner/arc-only-repo", "codex", "2026-10-16T19:00:00+00:00"),
                                ArcClose("a2", "Close it too", None, "codex", "2026-10-16T19:00:00+00:00")],
                          active=[arc("a3", repo="owner/active-only-repo")],
                          cq=[q(own="client-thing", repos=("client-thing",), line=1),
                              q(OTHER, repos=("demo-tool", "side-repo", None, UNRESOLVED), line=2)],
                          cn=[note(repos=("demo-tool", "note-repo"))]))
    assert led["private_names"] == ["active-only-repo", "arc-only-repo", "backup-box", "client-thing", "diary-app",
                                    "mystery-repo", "note-repo", "side-repo"]
    assert "private_names" not in writer_view(led)


def test_text_naming_a_private_repo_seen_that_day_is_dropped_at_collect(cfg):  # #2, the ledger matches like the gate
    led = ledger(cfg, raw(prs=[pr(1, title="Port the mystery repo importer"), pr(2, title="Add the demo"),
                               pr(3, repo="owner/mystery-repo")]))
    assert [i["id"] for i in led["items"]] == ["pr:demo-tool#2"]
    assert led["excluded"] == counters(blocked_name=1, unlisted_lane=1)


@pytest.mark.parametrize("cwd", ["/w/diary" + chr(0x200B) + "-app/notes",
                                 "/w/" + "".join(chr(ord(c) + 0xFEE0) for c in "diary-app") + "/notes"],
                         ids=["zero_width", "fullwidth"])
def test_a_disguised_excluded_fragment_in_a_session_directory_taints(cfg, cwd):  # #10
    led = ledger(cfg, raw(cq=[q(line=1), q(OTHER, cwds=("/w/demo-tool", cwd), line=2)]))
    assert [x["text"] for x in led["quotes"]] == [GOOD] and led["excluded"] == counters(tainted=1)
