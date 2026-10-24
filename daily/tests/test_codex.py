import datetime as dt
import json
import os
import sqlite3
import subprocess

import pytest

from daily.sources import claude as C
from daily.sources import codex as X
from daily_helpers import write_jsonl

DAY = dt.date(2026, 10, 16)
IN_TS = "2026-10-16T20:00:00.000Z"
AFTER_TS = "2026-10-17T20:00:00.000Z"
IN = int(dt.datetime(2026, 10, 16, 20, tzinfo=dt.timezone.utc).timestamp())
BEFORE = int(dt.datetime(2026, 10, 15, 20, tzinfo=dt.timezone.utc).timestamp())
AFTER = int(dt.datetime(2026, 10, 17, 20, tzinfo=dt.timezone.utc).timestamp())
GOOD = "Codex finished the parser rewrite and the whole suite went green."
SECOND = "The second sentence comes from a directory that has a remote of its own."
ORIGIN = "https://github.com/owner/demo-tool.git"
SUB = '{"subagent":{"thread_spawn":{"parent_thread_id":"p","depth":1}}}'
U_A = "01a1427c-1d20-7000-8000-00000000000a"
U_B = "01a1427c-1d20-7000-8000-00000000000b"
U_C = "01a1427c-1d20-7000-8000-00000000000c"


def meta(ts=IN_TS, cwd="/w/demo-tool"):
    return {"timestamp": ts, "type": "session_meta", "payload": {"id": "t", "cwd": cwd}}


def context(ts=IN_TS, cwd="/w/demo-tool"):
    return {"timestamp": ts, "type": "turn_context", "payload": {"cwd": cwd}}


def message(text, ts=IN_TS, id_="msg_1", role="assistant", block="output_text"):
    return {"timestamp": ts, "type": "response_item", "payload": {
        "type": "message", "role": role, "id": id_, "phase": "final_answer",
        "content": [{"type": block, "text": text}]}}


def rollout(path, ts=IN_TS, text=GOOD):
    return write_jsonl(path, [meta(ts), message(text, ts)])


def line_of(rec_):
    return (json.dumps(rec_) + "\n").encode()


def put_raw(path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def folder_rollout(tmp_path, folder_day, uuid, records):
    d = tmp_path / "sessions" / f"{folder_day:%Y}" / f"{folder_day:%m}" / f"{folder_day:%d}"
    return write_jsonl(d / f"rollout-{folder_day:%Y-%m-%d}T05-00-00-{uuid}.jsonl", records)


def make_db(tmp_path, rows, titles=None):
    """rows: (id, rollout_path, thread_source, git_origin_url, cwd, created_at, updated_at[, source]);
    titles: {id: (title, first_user_message)}."""
    (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
    db = tmp_path / "state_5.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, source TEXT NOT NULL, "
                "thread_source TEXT, git_origin_url TEXT, cwd TEXT NOT NULL, created_at INTEGER NOT NULL, "
                "updated_at INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '', "
                "first_user_message TEXT NOT NULL DEFAULT '')")
    full = [r if len(r) > 7 else r + (SUB if r[2] in ("subagent", "guardian_review") else "vscode",) for r in rows]
    con.executemany("INSERT INTO threads (id, rollout_path, thread_source, git_origin_url, cwd, created_at, "
                    "updated_at, source) VALUES (?,?,?,?,?,?,?,?)", full)
    for tid, (title, first) in (titles or {}).items():
        con.execute("UPDATE threads SET title = ?, first_user_message = ? WHERE id = ?", (title, first, tid))
    con.commit()
    con.close()
    return db


def no_resolver(cwd):
    raise AssertionError("resolver must not be called when git_origin_url is set")


def thread(tid, path, origin=ORIGIN, cwd="/w/demo-tool", kind="user", created=IN, updated=IN):
    return (tid, str(path), kind, origin, cwd, created, updated)


def test_user_thread_is_counted_with_quotes(tmp_path):  # positive control
    r = rollout(tmp_path / "r1.jsonl")
    db = make_db(tmp_path, [("t1", str(r), "user", ORIGIN, "/w/demo-tool", IN, IN)])
    got = X.scan_codex(DAY, db, no_resolver)
    assert (got.sessions, got.subagents) == (1, 0)
    [q] = got.quotes
    assert (q.harness, q.repo, q.text, q.line, q.record) == ("codex", ORIGIN, GOOD, 2, "msg_1")
    assert (q.cwd, q.ts, q.path) == ("/w/demo-tool", IN_TS, str(r))
    assert (q.session_repos, q.session_cwds, q.own_repo) == (("demo-tool",), ("/w/demo-tool",), "demo-tool")
    assert (got.skipped, got.vanished, got.unresolved, got.git_timeouts, got.unindexed) == (0,) * 5


def test_subagent_is_counted_without_quotes(tmp_path):
    r = rollout(tmp_path / "r2.jsonl")
    db = make_db(tmp_path, [("t2", str(r), "subagent", ORIGIN, "/w/demo-tool", IN, IN)])
    got = X.scan_codex(DAY, db, no_resolver)
    assert (got.sessions, got.subagents, got.quotes) == (0, 1, ())


def test_subagents_are_recognised_by_their_source_json_not_only_by_thread_source(tmp_path):
    # 196 real rows have no thread_source at all; the guardian reviews carry thread_source "guardian_review"
    r = rollout(tmp_path / "r.jsonl")
    db = make_db(tmp_path, [
        ("a", str(r), None, ORIGIN, "/w/demo-tool", IN, IN, SUB),
        ("b", str(r), "guardian_review", ORIGIN, "/w/demo-tool", IN, IN, '{"subagent":{"other":"guardian"}}'),
        ("c", str(r), None, ORIGIN, "/w/demo-tool", IN, IN, "cli"),
        ("d", str(r), "voice_chat", ORIGIN, "/w/demo-tool", IN, IN, "vscode"),
        ("e", str(r), "user", ORIGIN, "/w/demo-tool", IN, IN, "exec")])
    got = X.scan_codex(DAY, db, no_resolver)
    assert (got.sessions, got.subagents, len(got.quotes)) == (3, 2, 3)


def test_thread_outside_the_db_range_is_not_selected(tmp_path):
    r = rollout(tmp_path / "r3.jsonl")
    db = make_db(tmp_path, [("t3", str(r), "user", ORIGIN, "/w", BEFORE, BEFORE)])
    assert X.scan_codex(DAY, db, no_resolver).sessions == 0


def test_rollout_lines_decide_activity(tmp_path):
    r = rollout(tmp_path / "r4.jsonl", ts=AFTER_TS)
    db = make_db(tmp_path, [("t4", str(r), "user", ORIGIN, "/w", IN, AFTER)])
    assert X.scan_codex(DAY, db, no_resolver).sessions == 0


def test_the_local_day_edges_decide_which_lines_count(tmp_path):
    # PDT: the day is 07:00:00Z to 07:00:00Z the next UTC day, end exclusive; the rollout stamps are UTC
    first = "The first sentence of the day was written right on the edge."
    last = "The last sentence of the day was written right on the edge."
    r = write_jsonl(tmp_path / "r.jsonl", [
        message("This sentence was written one second before the day began.", "2026-10-16T06:59:59.000Z"),
        message(first, "2026-10-16T07:00:00.000Z", "m2"),
        message(last, "2026-10-17T06:59:59.000Z", "m3"),
        message("This sentence was written right when the next day began.", "2026-10-17T07:00:00.000Z")])
    db = make_db(tmp_path, [("t", str(r), "user", ORIGIN, "/w/demo-tool", IN, IN)])
    got = X.scan_codex(DAY, db, no_resolver)
    assert got.sessions == 1 and [q.text for q in got.quotes] == [first, last]


def test_a_thread_whose_only_in_day_line_is_a_settings_touch_is_not_a_session(tmp_path):
    touched = write_jsonl(tmp_path / "touched.jsonl", [
        meta("2026-10-15T20:00:00.000Z"),
        {"timestamp": IN_TS, "type": "event_msg", "payload": {"type": "thread_settings_applied"}}])
    briefed = write_jsonl(tmp_path / "briefed.jsonl", [meta("2026-10-15T20:00:00.000Z"),
                                                        message("Standing instructions.", role="developer")])
    worked = write_jsonl(tmp_path / "worked.jsonl", [
        meta("2026-10-15T20:00:00.000Z"),
        {"timestamp": IN_TS, "type": "response_item", "payload": {"type": "function_call", "name": "shell"}}])
    db = make_db(tmp_path, [thread("a", touched), thread("b", briefed), thread("c", worked)])
    assert X.scan_codex(DAY, db, no_resolver).sessions == 1  # only the one that made a tool call


def test_missing_rollout_counts_by_db_time_without_quotes(tmp_path):
    db = make_db(tmp_path, [("t5", str(tmp_path / "gone.jsonl"), "user", ORIGIN, "/w", IN, IN)])
    got = X.scan_codex(DAY, db, no_resolver)
    assert (got.sessions, got.quotes, got.vanished) == (1, (), 1)


def test_a_rollout_path_that_cannot_be_located_is_vanished_not_fatal(tmp_path):
    db = make_db(tmp_path, [thread("a", "relative/rollout.jsonl"), thread("b", ""), thread("c", "/w/x\x00y.jsonl")])
    got = X.scan_codex(DAY, db, no_resolver)
    assert (got.sessions, got.quotes, got.vanished) == (3, (), 3)


def test_missing_origin_uses_the_resolver(tmp_path):
    r = rollout(tmp_path / "r6.jsonl")
    db = make_db(tmp_path, [("t6", str(r), None, None, "/w/demo-tool", IN, IN)])
    got = X.scan_codex(DAY, db, lambda cwd: "owner/demo-tool" if cwd == "/w/demo-tool" else None)
    assert got.sessions == 1 and got.quotes[0].repo == "owner/demo-tool"


def test_missing_db_raises_instead_of_returning_an_empty_day(tmp_path):
    (tmp_path / "sessions").mkdir()
    with pytest.raises(X.CodexReadError):
        X.scan_codex(DAY, tmp_path / "nope.sqlite", no_resolver)


def test_a_db_that_cannot_be_queried_raises(tmp_path):
    (tmp_path / "sessions").mkdir()
    junk = put_raw(tmp_path / "junk.sqlite", b"this is not a sqlite database at all, just text" * 40)
    with pytest.raises(X.CodexReadError):
        X.scan_codex(DAY, junk, no_resolver)
    other = tmp_path / "other.sqlite"
    con = sqlite3.connect(other)
    con.execute("CREATE TABLE unrelated (x INTEGER)")
    con.commit()
    con.close()
    with pytest.raises(X.CodexReadError):  # a readable database without the threads table is not a quiet day
        X.scan_codex(DAY, other, no_resolver)


def test_the_db_is_opened_read_only(tmp_path):
    r = rollout(tmp_path / "r.jsonl")
    db = make_db(tmp_path, [thread("a", r)])
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    X.scan_codex(DAY, db, no_resolver)
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before


def test_missing_sessions_root_raises(tmp_path):
    r = rollout(tmp_path / "r.jsonl")
    db = make_db(tmp_path, [thread("a", r)])
    assert X.scan_codex(DAY, db, no_resolver).sessions == 1  # positive control: the default root is next to the db
    with pytest.raises(X.CodexReadError):
        X.scan_codex(DAY, db, no_resolver, sessions=tmp_path / "nope")
    (tmp_path / "afile").write_text("x")
    with pytest.raises(X.CodexReadError):
        X.scan_codex(DAY, db, no_resolver, sessions=tmp_path / "afile")
    (tmp_path / "sessions").rmdir()
    with pytest.raises(X.CodexReadError):
        X.scan_codex(DAY, db, no_resolver)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_unreadable_rollouts_and_folders_raise(tmp_path):
    r = rollout(tmp_path / "r.jsonl")
    db = make_db(tmp_path, [thread("a", r)])
    r.chmod(0)
    try:
        with pytest.raises(X.CodexReadError):
            X.scan_codex(DAY, db, no_resolver)
    finally:
        r.chmod(0o644)
    assert X.scan_codex(DAY, db, no_resolver).sessions == 1  # positive control: readable again
    day_dir = tmp_path / "sessions" / "2026" / "10" / "16"
    day_dir.mkdir(parents=True)
    day_dir.chmod(0)
    try:
        with pytest.raises(X.CodexReadError):
            X.scan_codex(DAY, db, no_resolver)
    finally:
        day_dir.chmod(0o755)
    folder_rollout(tmp_path, DAY, U_A, [meta(), message(GOOD)]).chmod(0)
    try:
        with pytest.raises(X.CodexReadError):  # an unindexed rollout that cannot be opened is not skipped silently
            X.scan_codex(DAY, db, no_resolver)
    finally:
        next(day_dir.iterdir()).chmod(0o644)


def test_a_directory_where_a_rollout_should_be_raises(tmp_path):
    (tmp_path / "adir").mkdir()
    db = make_db(tmp_path, [thread("a", tmp_path / "adir")])
    with pytest.raises(X.CodexReadError):
        X.scan_codex(DAY, db, no_resolver)


def test_only_assistant_output_text_is_quoted(tmp_path):
    r = write_jsonl(tmp_path / "r.jsonl", [
        meta(),
        message(GOOD, role="user", id_="u1"),
        message(GOOD, role="developer", id_="d1"),
        message(GOOD, block="input_text", id_="i1"),
        {"timestamp": IN_TS, "type": "response_item", "payload": {"type": "reasoning", "summary": [
            {"type": "output_text", "text": GOOD}]}},
        {"timestamp": IN_TS, "type": "response_item", "payload": {"type": "function_call", "arguments": GOOD}},
        {"timestamp": IN_TS, "type": "response_item", "payload": {"type": "custom_tool_call_output", "output": GOOD}},
        {"timestamp": IN_TS, "type": "event_msg", "payload": {"type": "agent_message", "message": GOOD}},
        message(GOOD, id_="a1")])
    db = make_db(tmp_path, [thread("a", r)])
    [q] = X.scan_codex(DAY, db, no_resolver).quotes
    assert (q.line, q.record) == (9, "a1")


def test_the_sentence_filter_is_the_shared_one(tmp_path):
    r = write_jsonl(tmp_path / "r.jsonl", [meta(), message(
        GOOD + " See `x` now.\nAnother line here that is long enough to keep, really!\nToo short.\n"
        "A path like src/app.py inside it is dropped for good today.")])
    db = make_db(tmp_path, [thread("a", r)])
    got = X.scan_codex(DAY, db, no_resolver)
    assert [q.text for q in got.quotes] == [GOOD, "Another line here that is long enough to keep, really!"]


def test_quote_line_number_is_the_exact_line_the_gate_will_read(tmp_path):
    # a U+2028 inside a line must not split it: the gate re-reads with file iteration, never splitlines()
    sep = json.dumps(message("A line holding a separator inside its text."), ensure_ascii=False).encode() + b"\n"
    path = put_raw(tmp_path / "r.jsonl", b"".join([
        line_of(meta()), b"\n", b"not json at all\n", sep, line_of(message(GOOD, id_="a1"))]))
    db = make_db(tmp_path, [thread("a", path)])
    [q] = X.scan_codex(DAY, db, no_resolver).quotes
    assert q.line == 5  # the blank line, the unreadable line and the separator line each count once
    with open(path, encoding="utf-8", errors="replace") as fh:  # the gate's own way of finding a line
        text = [ln for n, ln in enumerate(fh, 1) if n == q.line][0]
    assert q.text in json.loads(text)["payload"]["content"][0]["text"]
    assert len(path.read_text(encoding="utf-8").splitlines()) == 6  # positive control: splitlines would disagree


def test_unreadable_and_non_object_lines_are_counted_not_fatal(tmp_path):
    whole = line_of(message(GOOD, id_="a1"))
    path = put_raw(tmp_path / "r.jsonl", b"{broken\n[1, 2]\n\"text\"\n42\nnull\n\n   \n" + whole + whole[:40])
    db = make_db(tmp_path, [thread("a", path)])
    got = X.scan_codex(DAY, db, no_resolver)
    assert (got.sessions, len(got.quotes), got.skipped) == (1, 1, 6)  # blank lines are not skipped records


def test_deeply_nested_json_is_a_skipped_line_not_a_crash(tmp_path):
    path = put_raw(tmp_path / "r.jsonl", b"[" * 100000 + b"\n" + line_of(message(GOOD)))
    db = make_db(tmp_path, [thread("a", path)])
    got = X.scan_codex(DAY, db, no_resolver)
    assert (got.sessions, got.skipped) == (1, 1)


def test_undecodable_bytes_do_not_fail_the_read_and_never_reach_a_quote(tmp_path):
    bad = line_of(message("The new browser pool shipped today and every \x00 test passed.", id_="a1"))
    bad = bad.replace(b"\\u0000", b"\xff\xfe")  # invalid UTF-8 where the escape was
    path = put_raw(tmp_path / "r.jsonl", bad + line_of(message(GOOD, id_="a2")))
    db = make_db(tmp_path, [thread("a", path)])
    got = X.scan_codex(DAY, db, no_resolver)
    assert (got.sessions, [q.text for q in got.quotes], got.skipped) == (1, [GOOD], 0)


def test_undecodable_db_text_does_not_fail_the_read(tmp_path):
    r = rollout(tmp_path / "r.jsonl")
    db = make_db(tmp_path, [thread("a", r)])
    con = sqlite3.connect(db)
    con.execute("UPDATE threads SET cwd = CAST(? AS TEXT)", (b"/w/demo-tool\xff",))
    con.commit()
    con.close()
    got = X.scan_codex(DAY, db, lambda cwd: ORIGIN)
    assert got.sessions == 1 and "�" in got.quotes[0].session_cwds[-1]


def test_hostile_field_values_cannot_reach_the_quote_candidate(tmp_path):
    odd_id = message(GOOD, id_="a1‮\nx")
    odd_ts = message(GOOD + " Second.", ts=IN)  # inside the day, but not an ISO string
    odd_content = {"timestamp": IN_TS, "type": "response_item", "payload": {
        "type": "message", "role": "assistant", "content": "not a list"}}
    no_text = {"timestamp": IN_TS, "type": "response_item", "payload": {
        "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": ["x"]}, "str", None]}}
    odd_payload = {"timestamp": IN_TS, "type": "response_item", "payload": "not a dict"}
    odd_cwd = {"timestamp": IN_TS, "type": "turn_context", "payload": {"cwd": ["not", "a", "string"]}}
    path = write_jsonl(tmp_path / "r.jsonl", [meta(), odd_cwd, odd_id, odd_ts, odd_content, no_text, odd_payload,
                                               message(GOOD + " Third.", id_=["not", "a", "string"])])
    db = make_db(tmp_path, [thread("a", path)])
    got = X.scan_codex(DAY, db, no_resolver)
    assert [(q.record, q.cwd, q.ts) for q in got.quotes] == [("", "/w/demo-tool", IN_TS), ("", "/w/demo-tool", IN_TS)]
    assert got.quotes[0].session_cwds == ("/w/demo-tool",)


def test_quote_order_is_stable_across_threads(tmp_path):
    b = rollout(tmp_path / "b.jsonl", text="The second file holds a sentence that is long enough.")
    a = rollout(tmp_path / "a.jsonl", text="The first file holds a sentence that is long enough.")
    db = make_db(tmp_path, [thread("t2", b), thread("t1", a)])
    got = X.scan_codex(DAY, db, no_resolver)
    assert [os.path.basename(q.path) for q in got.quotes] == ["a.jsonl", "b.jsonl"]


def test_the_resolver_is_asked_once_per_directory(tmp_path):
    asked = []

    def counting(cwd):
        asked.append(cwd)
        return ORIGIN

    a, b = rollout(tmp_path / "a.jsonl"), rollout(tmp_path / "b.jsonl")
    db = make_db(tmp_path, [thread("t1", a, origin=None), thread("t2", b, origin=None)])
    assert len(X.scan_codex(DAY, db, counting).quotes) == 2
    assert asked == ["/w/demo-tool"]


def test_session_cwds_and_repos_list_every_in_day_directory_the_thread_used(tmp_path):
    urls = {"/w/demo-tool": ORIGIN, "/w/other": "git@github.com:owner/Other-Repo.git",
            "/w/yesterday": "https://github.com/owner/yesterday.git"}
    path = write_jsonl(tmp_path / "r.jsonl", [
        meta("2026-10-15T20:00:00.000Z", "/w/yesterday"),  # outside the day: not listed
        context(IN_TS, "/w/other"),                         # a turn that moved to another directory
        message(GOOD, id_="m1"),                            # said under /w/other
        context(IN_TS, "/w/nowhere"),                       # no remote: stands as None
        message(SECOND, id_="m2")])                         # said under /w/nowhere, which has no lane
    db = make_db(tmp_path, [thread("a", path, origin=None)])
    got = X.scan_codex(DAY, db, urls.get)
    q, n = got.quotes
    assert (q.text, q.cwd, q.repo, q.own_repo) == (GOOD, "/w/other", "git@github.com:owner/Other-Repo.git", "other-repo")
    assert (n.text, n.cwd, n.repo, n.own_repo) == (SECOND, "/w/nowhere", None, None)  # no remote: kept, tagged None
    for c in (q, n):  # every candidate of the thread carries the thread's whole scope
        # the directory recorded on the thread row is a directory the thread used, too
        assert c.session_cwds == ("/w/demo-tool", "/w/nowhere", "/w/other")
        assert c.session_repos == (None, "demo-tool", "other-repo")  # None first, then sorted names


def test_the_directory_in_force_follows_earlier_turns_even_from_a_previous_day(tmp_path):
    path = write_jsonl(tmp_path / "r.jsonl", [
        context("2026-10-15T20:00:00.000Z", "/w/other"), message(GOOD)])
    db = make_db(tmp_path, [thread("a", path, origin=ORIGIN, cwd="/w/demo-tool")])
    [q] = X.scan_codex(DAY, db, lambda cwd: "owner/" + cwd.rsplit("/", 1)[1]).quotes
    assert (q.cwd, q.repo) == ("/w/other", "owner/other")  # the recorded origin belongs to the row's own directory
    assert q.session_cwds == ("/w/demo-tool", "/w/other")


def test_a_directory_with_no_remote_keeps_its_quotes_tagged_none(tmp_path):
    path = write_jsonl(tmp_path / "r.jsonl", [meta(cwd="/w/elsewhere"), message(GOOD, id_="a1"),
                                               message(SECOND, id_="a2")])
    db = make_db(tmp_path, [thread("a", path, origin=None, cwd="/w/elsewhere")])
    got = X.scan_codex(DAY, db, lambda cwd: None)
    assert [(q.text, q.repo, q.own_repo, q.session_repos) for q in got.quotes] == [
        (GOOD, None, None, (None,)), (SECOND, None, None, (None,))]
    assert (got.sessions, got.unresolved) == (1, 0)


def test_credentials_in_a_recorded_origin_are_dropped(tmp_path):
    r = rollout(tmp_path / "r.jsonl")
    db = make_db(tmp_path, [thread("a", r, origin=" https://user:s3cret@github.com/owner/demo-tool.git ")])
    [q] = X.scan_codex(DAY, db, no_resolver).quotes
    assert q.repo == ORIGIN and "s3cret" not in q.repo


def test_a_blank_recorded_origin_falls_back_to_the_directory(tmp_path):
    r = rollout(tmp_path / "r.jsonl")
    db = make_db(tmp_path, [thread("a", r, origin="  ")])
    [q] = X.scan_codex(DAY, db, lambda cwd: ORIGIN).quotes
    assert q.repo == ORIGIN


def test_a_recorded_origin_keeps_its_lane_when_the_directory_is_gone(tmp_path):
    r = rollout(tmp_path / "r.jsonl")
    gone = str(tmp_path / "gone")  # a removed worktree; the origin was recorded when the thread started
    r = write_jsonl(tmp_path / "r.jsonl", [meta(cwd=gone), message(GOOD)])
    db = make_db(tmp_path, [thread("a", r, origin=ORIGIN, cwd=gone)])
    got = X.scan_codex(DAY, db, no_resolver)
    [q] = got.quotes
    assert (q.repo, q.session_repos, got.unresolved) == (ORIGIN, ("demo-tool",), 0)


def fake_git(code, out=""):
    def run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")
    return run


def test_a_deleted_directory_is_unresolved_and_its_quotes_are_kept_tagged_so(tmp_path):
    gone = str(tmp_path / "gone")
    path = write_jsonl(tmp_path / "r.jsonl", [meta(cwd=gone), message(GOOD)])
    db = make_db(tmp_path, [thread("a", path, origin=None, cwd=gone)])
    C.git_repo_for.cache_clear()
    got = X.scan_codex(DAY, db, C.git_repo_for)
    [q] = got.quotes
    assert (q.repo, q.own_repo, q.session_repos, q.session_cwds) == (None, "!unresolved", ("!unresolved",), (gone,))
    assert (got.sessions, got.unresolved, got.git_timeouts) == (1, 1, 0)


def test_an_existing_directory_with_no_remote_is_none_not_unresolved(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    path = write_jsonl(tmp_path / "r.jsonl", [meta(cwd=str(scratch)), message(GOOD)])
    db = make_db(tmp_path, [thread("a", path, origin=None, cwd=str(scratch))])
    C.git_repo_for.cache_clear()
    got = X.scan_codex(DAY, db, C.git_repo_for)
    [q] = got.quotes
    assert (q.repo, q.own_repo, q.session_repos, got.unresolved) == (None, None, (None,), 0)


def test_an_unresolved_sibling_directory_marks_the_thread_unresolved_not_no_remote(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    subprocess.run(["git", "init", "-q", str(good)], check=True)
    subprocess.run(["git", "-C", str(good), "remote", "add", "origin", ORIGIN], check=True)
    gone = str(tmp_path / "gone")
    path = write_jsonl(tmp_path / "r.jsonl", [meta(cwd=str(good)), context(IN_TS, gone), message(GOOD)])
    db = make_db(tmp_path, [thread("a", path, origin=None, cwd=str(good))])
    C.git_repo_for.cache_clear()
    got = X.scan_codex(DAY, db, C.git_repo_for)
    [q] = got.quotes
    assert (q.cwd, q.repo, q.own_repo) == (gone, None, "!unresolved")
    assert q.session_repos == ("!unresolved", "demo-tool")  # not (None, "demo-tool")
    assert got.unresolved == 1


def test_a_timed_out_directory_is_counted_apart_from_no_remote(tmp_path, monkeypatch):
    slow = tmp_path / "slow"
    slow.mkdir()
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if str(slow) in cmd:
            raise subprocess.TimeoutExpired(cmd, 10)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    path = write_jsonl(tmp_path / "r.jsonl", [meta(cwd=str(slow)), message(GOOD)])
    db = make_db(tmp_path, [thread("a", path, origin=None, cwd=str(slow))])
    C.git_repo_for.cache_clear()
    got = X.scan_codex(DAY, db, C.git_repo_for)
    [q] = got.quotes
    assert (q.own_repo, q.session_repos) == ("!unresolved", ("!unresolved",))
    assert (got.unresolved, got.git_timeouts) == (1, 1)


def test_an_unexpected_git_exit_code_is_unresolved(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_git(128))
    path = write_jsonl(tmp_path / "r.jsonl", [meta(cwd=str(tmp_path)), message(GOOD)])
    db = make_db(tmp_path, [thread("a", path, origin=None, cwd=str(tmp_path))])
    C.git_repo_for.cache_clear()
    got = X.scan_codex(DAY, db, C.git_repo_for)
    [q] = got.quotes
    assert (q.own_repo, got.unresolved) == ("!unresolved", 1)


def test_a_remote_with_no_usable_name_is_unresolved_never_no_remote(tmp_path):
    r = rollout(tmp_path / "r.jsonl")
    db = make_db(tmp_path, [thread("a", r, origin=None)])
    [q] = X.scan_codex(DAY, db, lambda cwd: "/").quotes  # a remote string that names no repository
    assert (q.own_repo, q.session_repos) == ("!unresolved", ("!unresolved",))
    db = make_db(tmp_path / "second", [thread("a", r, origin="/")])  # junk recorded origin: the directory decides
    [q] = X.scan_codex(DAY, db, lambda cwd: ORIGIN).quotes
    assert (q.repo, q.own_repo) == (ORIGIN, "demo-tool")


def test_a_missing_git_binary_is_a_codex_read_error(tmp_path, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing)
    path = write_jsonl(tmp_path / "r.jsonl", [meta(cwd=str(tmp_path)), message(GOOD)])
    db = make_db(tmp_path, [thread("a", path, origin=None, cwd=str(tmp_path))])
    C.git_repo_for.cache_clear()
    with pytest.raises(X.CodexReadError):
        X.scan_codex(DAY, db, C.git_repo_for)


def test_subagent_threads_never_ask_for_a_lane(tmp_path):
    r = rollout(tmp_path / "r.jsonl")
    db = make_db(tmp_path, [thread("a", r, origin=None, kind="subagent")])
    assert X.scan_codex(DAY, db, no_resolver).subagents == 1


# ---- session notes: one per main thread active on the day, whether or not it has a quotable sentence ----

def ref_of(tid):
    import hashlib
    return hashlib.sha256(tid.encode()).hexdigest()[:8]


def test_every_active_main_thread_gets_a_note_with_its_scope(tmp_path):
    spoke = rollout(tmp_path / "a.jsonl")
    quiet = write_jsonl(tmp_path / "b.jsonl", [meta(cwd="/w/other"), {
        "timestamp": IN_TS, "type": "response_item", "payload": {"type": "function_call", "name": "shell"}}])
    sub = rollout(tmp_path / "c.jsonl")
    idle = rollout(tmp_path / "d.jsonl", ts=AFTER_TS)
    db = make_db(tmp_path, [thread("t-a", spoke), thread("t-b", quiet, origin=None, cwd="/w/other"),
                            thread("t-c", sub, kind="subagent"), thread("t-d", idle)],
                 titles={"t-a": ("Rewrite the parser", ""), "t-b": ("Check the other tool", "")})
    got = X.scan_codex(DAY, db, lambda cwd: "https://github.com/owner/other-tool.git")
    assert got.sessions == 2 and len(got.quotes) == 1  # the quiet thread has a note but nothing to quote
    assert got.notes == (
        X.SessionNote("codex", ref_of("t-a"), "Rewrite the parser", ("demo-tool",), ("/w/demo-tool",), False, IN_TS),
        X.SessionNote("codex", ref_of("t-b"), "Check the other tool", ("other-tool",), ("/w/other",), False, IN_TS))


def test_a_note_ref_is_the_first_eight_hex_of_the_thread_id_hash(tmp_path):
    r = rollout(tmp_path / "r.jsonl")
    db = make_db(tmp_path, [thread(U_A, r)])
    [note] = X.scan_codex(DAY, db, no_resolver).notes
    assert note.ref == ref_of(U_A) and len(note.ref) == 8 and U_A[:8] not in note.ref + note.title


def test_a_note_title_is_the_thread_title_else_the_first_line_of_the_first_message(tmp_path):
    rs = [rollout(tmp_path / f"{i}.jsonl") for i in range(4)]
    db = make_db(tmp_path, [thread(f"t{i}", r) for i, r in enumerate(rs)], titles={
        "t0": ("The real title", "ignored first message"),
        "t1": ("   ", "\n\n  First line of the message  \nsecond line"),
        "t2": ("", ""),
        "t3": ("\u202e\u200b", "Fallback after a title of invisible characters")})
    notes = {n.ref: n.title for n in X.scan_codex(DAY, db, no_resolver).notes}
    assert notes == {ref_of("t0"): "The real title", ref_of("t1"): "First line of the message",
                     ref_of("t2"): "", ref_of("t3"): "Fallback after a title of invisible characters"}


def test_a_note_title_is_sanitized_and_capped_like_a_task_title(tmp_path):
    r = rollout(tmp_path / "r.jsonl")
    nasty = "Fix\x07 the\u202e bidi\u200b  and\ttabs\nand lines " + "word " * 60
    db = make_db(tmp_path, [thread("t", r)], titles={"t": (nasty, "")})
    [note] = X.scan_codex(DAY, db, no_resolver).notes
    assert note.title.startswith("Fix the bidi and tabs and lines word word")
    assert len(note.title) == 120 and note.title == note.title.rstrip()
    assert not any(ord(ch) < 32 or ch in "\u202e\u200b" for ch in note.title)


def test_a_vanished_rollout_still_gets_a_note_by_db_time(tmp_path):
    db = make_db(tmp_path, [thread("t", tmp_path / "gone.jsonl")], titles={"t": ("Work whose log is gone", "")})
    got = X.scan_codex(DAY, db, no_resolver)
    assert [(n.title, n.session_repos, n.session_cwds) for n in got.notes] == [
        ("Work whose log is gone", ("demo-tool",), ("/w/demo-tool",))]


def test_a_note_marks_unresolved_directories_like_a_quote_does(tmp_path):
    gone = str(tmp_path / "gone")
    r = write_jsonl(tmp_path / "r.jsonl", [meta(cwd=gone), message(GOOD)])
    db = make_db(tmp_path, [thread("t", r, origin=None, cwd=gone)])
    C.git_repo_for.cache_clear()
    [note] = X.scan_codex(DAY, db, C.git_repo_for).notes
    assert (note.session_repos, note.session_cwds) == (("!unresolved",), (gone,))


# ---- the sessions folders: local dates pick candidate files, the lines inside decide ----

def test_a_rollout_in_the_days_folder_finds_a_thread_whose_db_times_are_stale(tmp_path):
    near = folder_rollout(tmp_path, DAY, U_A, [meta(), message(GOOD)])
    far = folder_rollout(tmp_path, dt.date(2026, 10, 10), U_B, [meta(), message(SECOND, id_="m2")])
    db = make_db(tmp_path, [thread(U_A, near, created=BEFORE, updated=BEFORE),
                            thread(U_B, far, created=BEFORE, updated=BEFORE)])
    got = X.scan_codex(DAY, db, no_resolver)
    assert (got.sessions, [q.text for q in got.quotes], got.unindexed) == (1, [GOOD], 0)


def test_the_neighbouring_folders_are_candidates_and_the_lines_decide(tmp_path):
    # a thread started the evening before (local time) lives in the previous day's folder; its UTC lines are in the day
    late = "2026-10-16T08:00:00.000Z"  # 01:00 local on the 16th
    prev = folder_rollout(tmp_path, dt.date(2026, 10, 15), U_A, [meta("2026-10-15T20:00:00.000Z"), message(GOOD, late)])
    nxt = folder_rollout(tmp_path, dt.date(2026, 10, 17), U_B, [meta(), message(SECOND, IN_TS, "m2")])
    old = folder_rollout(tmp_path, dt.date(2026, 10, 14), U_C, [meta(), message(GOOD + " Again.", IN_TS, "m3")])
    db = make_db(tmp_path, [thread(U_A, prev, created=BEFORE, updated=BEFORE),
                            thread(U_B, nxt, created=BEFORE, updated=BEFORE),
                            thread(U_C, old, created=BEFORE, updated=BEFORE)])
    got = X.scan_codex(DAY, db, no_resolver)
    assert got.sessions == 2 and sorted(q.text for q in got.quotes) == sorted([GOOD, SECOND])  # two days back: not listed


def test_a_continuation_file_in_the_folder_is_read_as_part_of_its_thread(tmp_path):
    # the file in the folder carries two ids and the row names a different file: still the same thread, counted once
    cont = folder_rollout(tmp_path, DAY, U_A + "_" + U_B, [meta(), message(SECOND, id_="m2")])
    real = rollout(tmp_path / "real.jsonl", ts=IN_TS)
    db = make_db(tmp_path, [thread(U_B, real, created=BEFORE, updated=BEFORE)])
    got = X.scan_codex(DAY, db, no_resolver)
    assert (got.sessions, got.unindexed, got.vanished) == (1, 0, 0)
    assert sorted((q.text, q.path) for q in got.quotes) == sorted([(GOOD, str(real)), (SECOND, str(cont))])


# ---- a thread whose log rotated: the row names the newest file, older segments sit in the day's folders ----

def test_an_older_segment_of_a_thread_in_the_days_folder_is_read_with_the_row_file(tmp_path):
    # measured on the live box: the row names the newest file; an older file of the same id holds other, earlier work
    newest = rollout(tmp_path / "newest.jsonl")
    older = folder_rollout(tmp_path, DAY, U_A, [meta(cwd="/w/other"), message(SECOND, id_="m2")])
    db = make_db(tmp_path, [thread(U_A, newest)])
    got = X.scan_codex(DAY, db, lambda cwd: "https://github.com/owner/other.git")
    assert (got.sessions, got.unindexed, got.vanished, len(got.notes)) == (1, 0, 0, 1)
    assert sorted((q.text, q.path, q.line) for q in got.quotes) == sorted([(GOOD, str(newest), 2),
                                                                           (SECOND, str(older), 2)])
    [note] = got.notes
    assert note.session_cwds == ("/w/demo-tool", "/w/other") and note.session_repos == ("demo-tool", "other")
    assert {q.session_cwds for q in got.quotes} == {("/w/demo-tool", "/w/other")}


def test_a_thread_active_only_in_an_older_segment_still_counts_once(tmp_path):
    newest = rollout(tmp_path / "newest.jsonl", ts=AFTER_TS)
    folder_rollout(tmp_path, DAY, U_A, [meta(), message(GOOD)])
    db = make_db(tmp_path, [thread(U_A, newest, created=BEFORE, updated=AFTER)])
    got = X.scan_codex(DAY, db, no_resolver)
    assert (got.sessions, [q.text for q in got.quotes], got.unindexed) == (1, [GOOD], 0)


def test_older_segments_of_a_subagent_are_counted_with_it_and_never_quoted(tmp_path):
    newest = rollout(tmp_path / "newest.jsonl")
    folder_rollout(tmp_path, DAY, U_A, [meta(), message(SECOND, id_="m2")])
    db = make_db(tmp_path, [thread(U_A, newest, kind="subagent")])
    got = X.scan_codex(DAY, db, no_resolver)
    assert (got.sessions, got.subagents, got.quotes, got.unindexed) == (0, 1, (), 0)


def test_an_older_segment_the_row_names_is_not_read_twice(tmp_path):
    seg = folder_rollout(tmp_path, DAY, U_A, [meta(), message(GOOD)])
    db = make_db(tmp_path, [thread(U_A, seg)])
    assert [q.text for q in X.scan_codex(DAY, db, no_resolver).quotes] == [GOOD]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_an_unreadable_older_segment_raises(tmp_path):
    newest = rollout(tmp_path / "newest.jsonl")
    older = folder_rollout(tmp_path, DAY, U_A, [meta(), message(SECOND, id_="m2")])
    db = make_db(tmp_path, [thread(U_A, newest)])
    older.chmod(0)
    try:
        with pytest.raises(X.CodexReadError):
            X.scan_codex(DAY, db, no_resolver)
    finally:
        older.chmod(0o644)
    assert X.scan_codex(DAY, db, no_resolver).sessions == 1  # positive control: readable again


def test_an_active_rollout_no_thread_row_names_is_counted_as_unindexed(tmp_path):
    folder_rollout(tmp_path, DAY, U_A, [meta(), message(GOOD)])  # nothing in the db at all
    folder_rollout(tmp_path, DAY, U_B, [meta("2026-10-15T20:00:00.000Z")])  # no work on the day
    folder_rollout(tmp_path, DAY, U_C, [meta(), message("Hello.", role="developer")])  # no work on the day
    db = make_db(tmp_path, [])
    got = X.scan_codex(DAY, db, no_resolver)
    assert (got.sessions, got.subagents, got.quotes, got.unindexed) == (0, 0, (), 1)


def test_a_missing_day_folder_is_normal(tmp_path):
    r = rollout(tmp_path / "r.jsonl")
    db = make_db(tmp_path, [thread("a", r)])
    (tmp_path / "sessions" / "2026").mkdir()
    (tmp_path / "sessions" / "2026" / "10").write_text("a plain file where a month folder could be")
    assert X.scan_codex(DAY, db, no_resolver).sessions == 1


def test_sessions_may_be_given_explicitly(tmp_path):
    other = tmp_path / "elsewhere"
    folder_rollout(other.parent, DAY, U_A, [meta(), message(GOOD)])  # tmp_path/sessions
    db = make_db(tmp_path / "dbdir", [])
    other.mkdir()
    assert X.scan_codex(DAY, db, no_resolver, sessions=other).unindexed == 0
    assert X.scan_codex(DAY, db, no_resolver, sessions=tmp_path / "sessions").unindexed == 1


def test_the_result_is_a_harness_day():
    from daily.sources.claude import HarnessDay
    assert issubclass(X.CodexDay, HarnessDay)
    assert X.CodexDay(1, 2, ()).unindexed == 0


# ---- policy v2: shared candidate and note types; threads that touch an excluded path ----

PRIVATE = "/home/x/diary-app/notes.md"
FRAG = ("diary-app",)


def call(arguments, ts=IN_TS, kind="function_call"):
    key = "arguments" if kind == "function_call" else "input"
    return {"timestamp": ts, "type": "response_item", "payload": {"type": kind, "name": "shell", "call_id": "c1",
                                                                  key: arguments}}


def spawned_by(parent):
    return json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}})


def test_candidates_and_notes_are_the_shared_types(tmp_path):
    r = rollout(tmp_path / "r.jsonl")
    got = X.scan_codex(DAY, make_db(tmp_path, [thread("a", r)]), no_resolver)
    assert type(got.quotes[0]) is C.QuoteCandidate and type(got.notes[0]) is C.SessionNote
    assert X.SessionNote is C.SessionNote and not hasattr(X, "CodexQuote")
    assert (got.quotes[0].touches_excluded, got.notes[0].touches_excluded) == (False, False)


def test_a_function_call_argument_hit_taints_its_thread(tmp_path):
    hit = write_jsonl(tmp_path / "a.jsonl", [meta(), call(json.dumps({"cmd": f"cat {PRIVATE}"})), message(GOOD)])
    clean = write_jsonl(tmp_path / "b.jsonl", [meta(), call(json.dumps({"cmd": "cat /w/demo-tool/notes.md"})),
                                                message(SECOND, id_="m2")])  # positive control
    db = make_db(tmp_path, [thread("t-a", hit), thread("t-b", clean)])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert [(q.text, q.touches_excluded) for q in got.quotes] == [(GOOD, True), (SECOND, False)]
    assert [(n.ref, n.touches_excluded) for n in got.notes] == [(ref_of("t-a"), True), (ref_of("t-b"), False)]


def test_without_fragments_no_thread_is_tainted(tmp_path):
    hit = write_jsonl(tmp_path / "a.jsonl", [meta(cwd="/home/x/diary-app"), call(json.dumps({"cmd": PRIVATE})),
                                              message(GOOD)])
    got = X.scan_codex(DAY, make_db(tmp_path, [thread("t-a", hit)]), lambda cwd: None)
    assert ([q.touches_excluded for q in got.quotes], [n.touches_excluded for n in got.notes]) == ([False], [False])


def test_a_custom_tool_call_input_and_an_escaped_argument_both_taint(tmp_path):
    patch = "*** Begin Patch\n*** Update File: /home/x/Diary-App/notes.md\n@@\n-a\n+b\n*** End Patch\n"
    custom = write_jsonl(tmp_path / "a.jsonl", [meta(), call(patch, kind="custom_tool_call"), message(GOOD)])
    escaped = write_jsonl(tmp_path / "b.jsonl", [meta(), call('{"cmd":"cat \\/home\\/x\\/diary\\u002dapp\\/n.md"}'),
                                                  message(SECOND, id_="m2")])
    db = make_db(tmp_path, [thread("t-a", custom), thread("t-b", escaped)])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [True, True]


def test_a_tool_output_naming_the_folder_bare_or_prose_naming_the_path_is_not_a_touch(tmp_path):
    r = write_jsonl(tmp_path / "a.jsonl", [
        meta(), call(json.dumps({"cmd": "ls /home/x"})),
        {"timestamp": IN_TS, "type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1",
                                                                  "output": "notes.md diary-app other"}},
        message(f"Please read {PRIVATE} for me.", role="user", id_="u1"),
        message(GOOD)])
    got = X.scan_codex(DAY, make_db(tmp_path, [thread("t-a", r)]), no_resolver, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [False]


def test_an_excluded_directory_taints_the_thread(tmp_path):
    moved = write_jsonl(tmp_path / "a.jsonl", [meta(), context(IN_TS, "/home/x/Diary-App"), message(GOOD)])
    row = write_jsonl(tmp_path / "b.jsonl", [message(SECOND, id_="m2")])  # only the row names the directory
    clean = rollout(tmp_path / "c.jsonl", text="A third thread works in a directory that is fine to show.")
    db = make_db(tmp_path, [thread("t-a", moved), thread("t-b", row, origin=None, cwd="/home/x/diary-app"),
                            thread("t-c", clean)])
    got = X.scan_codex(DAY, db, lambda cwd: None, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [True, True, False]


def test_a_call_from_another_day_in_force_directory_counts_when_used_on_the_day(tmp_path):
    # the directory named yesterday is still the one the thread works in today
    r = write_jsonl(tmp_path / "a.jsonl", [context("2026-10-15T20:00:00.000Z", "/home/x/diary-app"),
                                            call(json.dumps({"cmd": "ls"}))])
    got = X.scan_codex(DAY, make_db(tmp_path, [thread("t-a", r)]), no_resolver, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [True]


def test_a_subagent_thread_touching_an_excluded_path_taints_its_parent(tmp_path):
    parent = rollout(tmp_path / "p.jsonl")
    child = write_jsonl(tmp_path / "c.jsonl", [meta(), call(json.dumps({"cmd": f"cat {PRIVATE}"}))])
    other = rollout(tmp_path / "o.jsonl", text="The other parent wrote a sentence that is long enough.")
    clean = write_jsonl(tmp_path / "k.jsonl", [meta(), call(json.dumps({"cmd": "ls /w/demo-tool"}))])
    db = make_db(tmp_path, [thread("t-p", parent), thread("t-o", other),
                            ("t-c", str(child), "subagent", ORIGIN, "/w/demo-tool", IN, IN, spawned_by("t-p")),
                            ("t-k", str(clean), "subagent", ORIGIN, "/w/demo-tool", IN, IN, spawned_by("t-o"))])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert got.subagents == 2
    assert {n.ref: n.touches_excluded for n in got.notes} == {ref_of("t-p"): True, ref_of("t-o"): False}
    assert {q.text: q.touches_excluded for q in got.quotes} == {GOOD: True, "The other parent wrote a sentence "
                                                                            "that is long enough.": False}


def test_codex_exclusion_matching_ignores_case(tmp_path):
    r = write_jsonl(tmp_path / "a.jsonl", [meta(), call(json.dumps({"cmd": "cat /home/x/DIARY-APP/n.md"})),
                                            message(GOOD)])
    db = make_db(tmp_path, [thread("t-a", r)])
    assert [n.touches_excluded for n in X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG).notes] == [True]
    assert [n.touches_excluded for n in X.scan_codex(DAY, db, no_resolver, exclude_fragments=("Diary-App",)).notes
            ] == [True]
    assert [n.touches_excluded for n in X.scan_codex(DAY, db, no_resolver, exclude_fragments=("other-thing",)).notes
            ] == [False]  # positive control


def test_codex_exclude_fragments_must_be_a_collection_of_strings(tmp_path):
    db = make_db(tmp_path, [thread("t-a", rollout(tmp_path / "a.jsonl"))])
    with pytest.raises(TypeError):
        X.scan_codex(DAY, db, no_resolver, exclude_fragments="diary-app")


# ---- follow-up: taint from every segment and spawned thread on any day, last_ts on notes, fragments_checked ----

OUT_TS = "2026-10-15T20:00:00.000Z"


def test_a_thread_that_touched_the_excluded_path_the_day_before_is_tainted_today(tmp_path):
    hit = write_jsonl(tmp_path / "a.jsonl", [meta(OUT_TS), call(json.dumps({"cmd": f"cat {PRIVATE}"}), ts=OUT_TS),
                                              message("Yesterday a sentence was said that is long enough.", OUT_TS),
                                              message(GOOD, id_="m2")])
    clean = write_jsonl(tmp_path / "b.jsonl", [meta(OUT_TS), call(json.dumps({"cmd": "ls /w"}), ts=OUT_TS),
                                                message(SECOND, id_="m3")])  # positive control
    db = make_db(tmp_path, [thread("t-a", hit, created=BEFORE), thread("t-b", clean, created=BEFORE)])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert [(q.text, q.touches_excluded) for q in got.quotes] == [(GOOD, True), (SECOND, False)]  # in-day only
    assert [n.touches_excluded for n in got.notes] == [True, False]


def test_an_old_segment_outside_the_days_folders_taints_its_thread(tmp_path):
    newest = rollout(tmp_path / "newest.jsonl")
    folder_rollout(tmp_path, dt.date(2026, 10, 1), U_A, [meta(OUT_TS), call(json.dumps({"cmd": PRIVATE}), ts=OUT_TS)])
    other = rollout(tmp_path / "other.jsonl", text="The other thread said a sentence that is long enough.")
    folder_rollout(tmp_path, dt.date(2026, 10, 1), U_B, [meta(OUT_TS), call(json.dumps({"cmd": "ls"}), ts=OUT_TS)])
    db = make_db(tmp_path, [thread(U_A, newest), thread(U_B, other)])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [True, False]
    assert [q.text for q in got.quotes] == [GOOD, "The other thread said a sentence that is long enough."]
    assert (got.sessions, got.unindexed) == (2, 0)  # the old segment is read for the taint only


def test_a_subagent_spawned_on_an_earlier_day_taints_its_parent(tmp_path):
    parent = rollout(tmp_path / "p.jsonl")
    old_child = write_jsonl(tmp_path / "c.jsonl", [meta(OUT_TS),
                                                    call(json.dumps({"cmd": f"cat {PRIVATE}"}), ts=OUT_TS)])
    grandchild = write_jsonl(tmp_path / "g.jsonl", [meta(OUT_TS), call(json.dumps({"cmd": "ls"}), ts=OUT_TS)])
    other = rollout(tmp_path / "o.jsonl", text="The other parent wrote a sentence that is long enough.")
    old_clean = write_jsonl(tmp_path / "k.jsonl", [meta(OUT_TS), call(json.dumps({"cmd": "ls"}), ts=OUT_TS)])
    db = make_db(tmp_path, [
        thread("t-p", parent), thread("t-o", other),
        ("t-c", str(old_child), "subagent", ORIGIN, "/w/demo-tool", BEFORE, BEFORE, spawned_by("t-x")),
        ("t-x", str(grandchild), "subagent", ORIGIN, "/w/demo-tool", BEFORE, BEFORE, spawned_by("t-p")),
        ("t-k", str(old_clean), "subagent", ORIGIN, "/w/demo-tool", BEFORE, BEFORE, spawned_by("t-o"))])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert got.subagents == 0  # none of them worked on the day
    assert {n.ref: n.touches_excluded for n in got.notes} == {ref_of("t-p"): True, ref_of("t-o"): False}


def test_a_note_carries_the_latest_in_day_line_time_in_utc(tmp_path):
    r = write_jsonl(tmp_path / "a.jsonl", [meta("2026-10-16T09:00:00.000Z"), message(GOOD, "2026-10-16T20:00:00.5Z"),
                                            context("2026-10-16T23:30:00+02:00"),             # 21:30 UTC, any line
                                            message(SECOND, "2026-10-17T08:00:00.000Z", "m2")])  # next local day
    db = make_db(tmp_path, [thread("t-a", r), thread("t-b", tmp_path / "gone.jsonl")])  # t-b: rollout vanished
    a, b = X.scan_codex(DAY, db, no_resolver).notes
    assert a.last_ts == "2026-10-16T21:30:00.000Z"
    assert b.last_ts == "2026-10-16T20:00:00.000Z"  # by the db's own in-day time when the log is gone


def test_codex_reports_the_number_of_fragments_applied(tmp_path):
    db = make_db(tmp_path, [thread("t-a", rollout(tmp_path / "a.jsonl"))])
    assert X.scan_codex(DAY, db, no_resolver).fragments_checked == 0
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=("diary-app", " DIARY-APP ", "client-work"))
    assert got.fragments_checked == 2
    from daily.sources.transcripts import digest_fragments
    assert X.scan_codex(DAY, db, no_resolver).fragments_digest == ""
    assert got.fragments_digest == digest_fragments(("client-work", "diary-app")) != ""  # the set, in any order


def test_last_ts_is_the_latest_across_lines_out_of_order_and_across_segments(tmp_path):
    newest = write_jsonl(tmp_path / "newest.jsonl", [meta("2026-10-16T21:30:00.000Z"),
                                                      message(GOOD, "2026-10-16T09:00:00.000Z")])  # later line, earlier
    folder_rollout(tmp_path, DAY, U_A, [meta("2026-10-16T20:00:00.000Z")])  # an older segment, read after the row's
    [note] = X.scan_codex(DAY, make_db(tmp_path, [thread(U_A, newest)]), no_resolver).notes
    assert note.last_ts == "2026-10-16T21:30:00.000Z"


# ---- compressed older segments (.jsonl.zst): read for the taint only, and never taken as clean unread ----

import shutil  # noqa: E402

ZSTD = shutil.which("zstd")


def zst_segment(tmp_path, folder_day, uuid, records=None, raw=None):
    """A compressed segment in the sessions tree, built with the zstd CLI from records (or written as raw bytes)."""
    d = tmp_path / "sessions" / f"{folder_day:%Y}" / f"{folder_day:%m}" / f"{folder_day:%d}"
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"rollout-{folder_day:%Y-%m-%d}T05-00-00-{uuid}.jsonl.zst"
    if raw is not None:
        out.write_bytes(raw)
    else:
        src = write_jsonl(tmp_path / "plain" / f"{uuid}.jsonl", records)
        subprocess.run([ZSTD, "-q", "-f", "-o", str(out), str(src)], check=True)
    return out


def fake_zstd(tmp_path, body):
    script = tmp_path / "fake-zstd"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    return str(script)


@pytest.mark.skipif(ZSTD is None, reason="the zstd CLI is not installed")
def test_a_compressed_segment_holding_an_excluded_path_taints_its_thread(tmp_path):
    newest_a, newest_b = rollout(tmp_path / "a.jsonl"), rollout(tmp_path / "b.jsonl", text=SECOND)
    zst_segment(tmp_path, dt.date(2026, 10, 1), U_A, [meta(OUT_TS),
                                                      call(json.dumps({"cmd": f"cat {PRIVATE}"}), ts=OUT_TS),
                                                      message("A compressed sentence that is long enough here.")])
    zst_segment(tmp_path, dt.date(2026, 10, 1), U_B, [meta(OUT_TS), call(json.dumps({"cmd": "ls /w"}), ts=OUT_TS)])
    db = make_db(tmp_path, [thread(U_A, newest_a), thread(U_B, newest_b)])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [True, False]  # the clean compressed segment does not taint
    assert [(q.text, q.path) for q in got.quotes] == [(GOOD, str(newest_a)), (SECOND, str(newest_b))]  # never quoted
    assert (got.zst_unread, got.skipped) == (0, 0)


def test_without_zstd_a_compressed_segment_forces_the_taint_and_is_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(X.shutil, "which", lambda name: None)
    parent, other = rollout(tmp_path / "p.jsonl"), rollout(tmp_path / "o.jsonl", text=SECOND)
    child = rollout(tmp_path / "c.jsonl", ts=OUT_TS)  # spawned yesterday; its older segment is compressed
    zst_segment(tmp_path, dt.date(2026, 10, 1), U_C, raw=b"not even zstd")
    db = make_db(tmp_path, [thread("t-p", parent), thread("t-o", other),
                            (U_C, str(child), "subagent", ORIGIN, "/w/demo-tool", BEFORE, BEFORE, spawned_by("t-p"))])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert {n.ref: n.touches_excluded for n in got.notes} == {ref_of("t-p"): True, ref_of("t-o"): False}
    assert got.zst_unread == 1
    plain = X.scan_codex(DAY, db, no_resolver)  # no fragments: nothing to check, nothing read
    assert ([n.touches_excluded for n in plain.notes], plain.zst_unread) == ([False, False], 0)


CLEAN_LINE = json.dumps(meta(OUT_TS))


@pytest.mark.parametrize("body,unread", [
    (f"printf '%s\\n' '{CLEAN_LINE}'", False),       # positive control: a clean segment decompressed in full
    (f"printf '%s\\n' '{CLEAN_LINE}'; exit 1", True),  # zstd failed
    ("sleep 5", True),                                # timed out
    ("printf '{\"type\": \"event_msg\", \"payload\": {\"text\": \"a\\377b\"}}\\n'", True),  # not UTF-8, in a JSON line
    (f"printf '%s\\nnot json\\n' '{CLEAN_LINE}'", True)])  # a line that is not a JSON object
def test_a_compressed_segment_that_cannot_be_read_in_full_forces_the_taint(tmp_path, monkeypatch, body, unread):
    exe = fake_zstd(tmp_path, body)
    monkeypatch.setattr(X.shutil, "which", lambda name: exe if name == "zstd" else None)
    monkeypatch.setattr(X, "ZSTD_BUDGET", 1)
    zst_segment(tmp_path, dt.date(2026, 10, 1), U_A, raw=b"stand-in")
    db = make_db(tmp_path, [thread(U_A, rollout(tmp_path / "a.jsonl"))])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert ([n.touches_excluded for n in got.notes], got.zst_unread) == ([unread], int(unread))


# ---- review round: tool output, fork bases, missing children, compacted history, archived segments, zstd budget ----

def output(text, ts=IN_TS, kind="function_call_output"):
    return {"timestamp": ts, "type": "response_item", "payload": {"type": kind, "call_id": "c1", "output": text}}


def forked(base, ts=OUT_TS, how="forked_from_id"):
    payload = {"id": "t", "cwd": "/w/demo-tool"}
    payload.update({"forked_from_id": base} if how == "forked_from_id" else
                   {"history_base": {"thread_id": base, "end_byte_offset": 10, "end_ordinal_exclusive": 1}})
    return {"timestamp": ts, "type": "session_meta", "payload": payload}


@pytest.mark.parametrize("kind", ["function_call_output", "custom_tool_call_output"])
def test_a_tool_output_listing_an_excluded_path_taints_the_thread(tmp_path, kind):
    hit = write_jsonl(tmp_path / "a.jsonl", [meta(), call(json.dumps({"cmd": "grep -rl tracker /home/x"})),
                                              output("/home/x/Diary-App/notes.md:3: tracker", kind=kind),
                                              message(GOOD)])
    bare = write_jsonl(tmp_path / "b.jsonl", [meta(), call(json.dumps({"cmd": "ls /home/x"})),
                                               output("diary-app notes", kind=kind), message(SECOND, id_="m2")])
    db = make_db(tmp_path, [thread("t-a", hit), thread("t-b", bare)])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [True, False]  # a folder named bare is not a path into it


def test_an_escaped_path_in_a_json_tool_output_still_taints(tmp_path):
    r = write_jsonl(tmp_path / "a.jsonl", [meta(), output('{"output":"\\/home\\/x\\/diary-app\\/n.md"}'),
                                            message(GOOD)])
    got = X.scan_codex(DAY, make_db(tmp_path, [thread("t-a", r)]), no_resolver, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [True]


@pytest.mark.parametrize("how", ["forked_from_id", "history_base"])
def test_a_fork_base_whose_history_touched_the_path_taints_the_fork(tmp_path, how):
    base = write_jsonl(tmp_path / "base.jsonl", [meta(OUT_TS), call(json.dumps({"cmd": f"cat {PRIVATE}"}), ts=OUT_TS)])
    clean_base = write_jsonl(tmp_path / "cbase.jsonl", [meta(OUT_TS), call(json.dumps({"cmd": "ls"}), ts=OUT_TS)])
    fork = write_jsonl(tmp_path / "fork.jsonl", [forked("t-base", how=how), message(GOOD)])
    other = write_jsonl(tmp_path / "other.jsonl", [forked("t-cbase", how=how), message(SECOND, id_="m2")])
    db = make_db(tmp_path, [thread("t-fork", fork), thread("t-other", other),
                            thread("t-base", base, created=BEFORE, updated=BEFORE),
                            thread("t-cbase", clean_base, created=BEFORE, updated=BEFORE)])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert {n.ref: n.touches_excluded for n in got.notes} == {ref_of("t-fork"): True, ref_of("t-other"): False}


def test_fork_chains_are_followed_and_cycles_end(tmp_path):
    a = write_jsonl(tmp_path / "a.jsonl", [forked("t-b"), message(GOOD)])
    b = write_jsonl(tmp_path / "b.jsonl", [forked("t-c")])
    c = write_jsonl(tmp_path / "c.jsonl", [forked("t-a"), call(json.dumps({"cmd": f"cat {PRIVATE}"}), ts=OUT_TS)])
    loop = write_jsonl(tmp_path / "l.jsonl", [forked("t-m"), message(SECOND, id_="m2")])
    m = write_jsonl(tmp_path / "m.jsonl", [forked("t-loop")])
    db = make_db(tmp_path, [thread("t-a", a), thread("t-loop", loop),
                            *(thread(i, f, created=BEFORE, updated=BEFORE) for i, f in (("t-b", b), ("t-c", c),
                                                                                       ("t-m", m)))])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert {n.ref: n.touches_excluded for n in got.notes} == {ref_of("t-a"): True, ref_of("t-loop"): False}


def test_a_spawned_child_whose_rollout_is_missing_forces_the_taint_and_is_counted(tmp_path):
    parent, other = rollout(tmp_path / "p.jsonl"), rollout(tmp_path / "o.jsonl", text=SECOND)
    present = write_jsonl(tmp_path / "k.jsonl", [meta(OUT_TS), call(json.dumps({"cmd": "ls"}), ts=OUT_TS)])
    db = make_db(tmp_path, [thread("t-p", parent), thread("t-o", other),
                            ("t-c", str(tmp_path / "gone.jsonl"), "subagent", ORIGIN, "/w", BEFORE, BEFORE,
                             spawned_by("t-p")),
                            ("t-k", str(present), "subagent", ORIGIN, "/w", BEFORE, BEFORE, spawned_by("t-o"))])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert {n.ref: n.touches_excluded for n in got.notes} == {ref_of("t-p"): True, ref_of("t-o"): False}
    assert got.missing_child == 1
    assert X.scan_codex(DAY, db, no_resolver).missing_child == 0  # nothing is checked without fragments


def test_compacted_history_tool_calls_and_outputs_count(tmp_path):
    def compacted(items):
        return {"timestamp": OUT_TS, "type": "compacted",
                "payload": {"message": "summary", "replacement_history": items}}
    by_call = write_jsonl(tmp_path / "a.jsonl", [meta(), compacted([
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {"type": "function_call", "name": "shell", "arguments": json.dumps({"cmd": f"cat {PRIVATE}"})}]),
        message(GOOD)])
    by_output = write_jsonl(tmp_path / "b.jsonl", [meta(), compacted([
        {"type": "function_call_output", "call_id": "c", "output": f"listed {PRIVATE}"}]), message(SECOND, id_="m2")])
    prose = write_jsonl(tmp_path / "c.jsonl", [meta(), compacted([  # positive control: prose naming the path
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": PRIVATE}]}]),
        message("A third thread says a sentence that is long enough here.", id_="m3")])
    db = make_db(tmp_path, [thread("t-a", by_call), thread("t-b", by_output), thread("t-c", prose)])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [True, True, False]


def archived_zst(tmp_path, uuid, records):
    d = tmp_path / "archived_sessions"
    d.mkdir(parents=True, exist_ok=True)
    src = write_jsonl(tmp_path / "plain" / f"arch-{uuid}.jsonl", records)
    out = d / f"rollout-2026-09-01T05-00-00-{uuid}.jsonl.zst"
    subprocess.run([ZSTD, "-q", "-f", "-o", str(out), str(src)], check=True)
    return out


@pytest.mark.skipif(ZSTD is None, reason="the zstd CLI is not installed")
def test_an_archived_segment_of_the_thread_taints_it(tmp_path):
    archived_zst(tmp_path, U_A, [meta(OUT_TS), call(json.dumps({"cmd": f"cat {PRIVATE}"}), ts=OUT_TS)])
    archived_zst(tmp_path, U_B, [meta(OUT_TS), call(json.dumps({"cmd": "ls"}), ts=OUT_TS)])
    db = make_db(tmp_path, [thread(U_A, rollout(tmp_path / "a.jsonl")),
                            thread(U_B, rollout(tmp_path / "b.jsonl", text=SECOND))])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert ([n.touches_excluded for n in got.notes], got.zst_unread) == ([True, False], 0)


def test_segments_left_when_the_zstd_budget_runs_out_force_the_taint(tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    exe = fake_zstd(tmp_path, f"touch {marker}; printf '%s\\n' '{CLEAN_LINE}'")
    monkeypatch.setattr(X.shutil, "which", lambda name: exe if name == "zstd" else None)
    zst_segment(tmp_path, dt.date(2026, 10, 1), U_A, raw=b"stand-in")
    db = make_db(tmp_path, [thread(U_A, rollout(tmp_path / "a.jsonl"))])
    monkeypatch.setattr(X, "ZSTD_BUDGET", 0)
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert ([n.touches_excluded for n in got.notes], got.zst_unread, marker.exists()) == ([True], 1, False)
    monkeypatch.setattr(X, "ZSTD_BUDGET", 30)  # positive control: with time left the same segment reads clean
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert ([n.touches_excluded for n in got.notes], got.zst_unread, marker.exists()) == ([False], 0, True)


def test_a_json_tool_output_listing_the_folder_bare_is_not_a_touch(tmp_path):
    # the output is JSON text: the escaped newline after a bare name must not read as a Windows separator
    listing = json.dumps({"output": "demo-tool\ndiary-app\nnotes\n", "metadata": {"exit_code": 0}})
    assert "diary-app\\n" in listing  # positive control: the raw text really holds a backslash after the name
    r = write_jsonl(tmp_path / "a.jsonl", [meta(), call(json.dumps({"cmd": "ls /home/x"})), output(listing),
                                            message(GOOD)])
    got = X.scan_codex(DAY, make_db(tmp_path, [thread("t-a", r)]), no_resolver, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [False]


# ---- shell commands Codex logs only as event_msg item_completed CommandExecution records ----

def command_run(command=("ls", "/home/x"), cwd="/w/demo-tool", output="", ts=OUT_TS):
    return {"timestamp": ts, "type": "event_msg", "payload": {
        "type": "item_completed", "thread_id": "t", "turn_id": "u", "started_at_ms": 1, "completed_at_ms": 2,
        "item": {"type": "CommandExecution", "id": "c", "command": list(command), "cwd": cwd,
                 "parsed_cmd": [{"type": "unknown", "cmd": " ".join(command)}], "aggregated_output": output,
                 "exit_code": 0, "status": "completed", "source": "userShell", "duration": None, "process_id": None}}}


@pytest.mark.parametrize("record,hit", [
    (command_run(output="/home/x/Diary-App/notes.md\n/home/x/other.md\n"), True),  # the output lists a path into it
    (command_run(output="demo-tool\ndiary-app\nnotes\n"), False),                     # positive control: a bare name
    (command_run(command=("cat", PRIVATE)), True),                                   # the command names it
    (command_run(cwd="/home/x/diary-app"), True)])                                   # it ran inside it
def test_a_logged_shell_command_counts_like_a_tool_call(tmp_path, record, hit):
    r = write_jsonl(tmp_path / "a.jsonl", [meta(), record, message(GOOD)])
    got = X.scan_codex(DAY, make_db(tmp_path, [thread("t-a", r)]), no_resolver, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [hit]


def test_a_logged_shell_command_in_an_older_segment_or_a_fork_base_taints(tmp_path):
    listing = command_run(output=f"{PRIVATE}\n")
    folder_rollout(tmp_path, dt.date(2026, 10, 1), U_A, [meta(OUT_TS), listing])  # an older, rotated segment
    base = write_jsonl(tmp_path / "base.jsonl", [meta(OUT_TS), listing])
    fork = write_jsonl(tmp_path / "fork.jsonl", [forked("t-base"), message(SECOND, id_="m2")])
    clean = rollout(tmp_path / "c.jsonl", text="A third thread says a sentence that is long enough here.")
    db = make_db(tmp_path, [thread(U_A, rollout(tmp_path / "a.jsonl")), thread("t-fork", fork), thread("t-c", clean),
                            thread("t-base", base, created=BEFORE, updated=BEFORE)])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert {n.ref: n.touches_excluded for n in got.notes} == {ref_of(U_A): True, ref_of("t-fork"): True,
                                                              ref_of("t-c"): False}


@pytest.mark.skipif(ZSTD is None, reason="the zstd CLI is not installed")
def test_a_logged_shell_command_in_a_compressed_segment_taints(tmp_path):
    zst_segment(tmp_path, dt.date(2026, 10, 1), U_A, [meta(OUT_TS), command_run(output=f"{PRIVATE}\n")])
    zst_segment(tmp_path, dt.date(2026, 10, 1), U_B, [meta(OUT_TS), command_run(output="diary-app\n")])
    db = make_db(tmp_path, [thread(U_A, rollout(tmp_path / "a.jsonl")),
                            thread(U_B, rollout(tmp_path / "b.jsonl", text=SECOND))])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert ([n.touches_excluded for n in got.notes], got.zst_unread) == ([True, False], 0)



# ---- a historical segment that vanished after indexing taints (pipeline cycle 3) ----

@pytest.mark.parametrize("delete", [True, False], ids=["vanished", "both_exist"])
def test_a_historical_segment_gone_before_the_taint_scan_taints_and_is_counted(tmp_path, monkeypatch, delete):
    newest = rollout(tmp_path / "newest.jsonl")  # the row's own (primary) rollout
    old = folder_rollout(tmp_path, dt.date(2026, 10, 1), U_A, [meta(OUT_TS), call(json.dumps({"cmd": "ls"}), ts=OUT_TS)])
    other = rollout(tmp_path / "other.jsonl", text=SECOND)
    real_index = X._rollout_index

    def index_then_delete(root, *args):
        found = real_index(root, *args)
        if delete:
            old.unlink(missing_ok=True)  # indexed, then gone before own() reads it
        return found
    monkeypatch.setattr(X, "_rollout_index", index_then_delete)
    db = make_db(tmp_path, [thread(U_A, newest), thread(U_B, other)])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [delete, False]  # both clean segments: no taint (control)
    assert (got.vanished_segment, got.vanished, got.zst_unread) == (int(delete), 0, 0)


def test_a_primary_rollout_that_is_gone_keeps_its_old_handling(tmp_path):
    db = make_db(tmp_path, [thread(U_A, tmp_path / "gone.jsonl"), thread(U_B, rollout(tmp_path / "b.jsonl"))])
    got = X.scan_codex(DAY, db, no_resolver, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [False, False]  # its own missing rollout does not taint it
    assert (got.vanished, got.vanished_segment) == (1, 0)
