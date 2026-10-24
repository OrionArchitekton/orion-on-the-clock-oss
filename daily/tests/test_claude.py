import datetime as dt
import json
import os
import subprocess

import pytest

from daily.sources import claude as C
from daily_helpers import write_jsonl

DAY = dt.date(2026, 10, 16)
IN = "2026-10-16T20:00:00.000Z"
OUT = "2026-10-15T20:00:00.000Z"
GOOD = "The new browser pool shipped today and every single test passed."
LANE_SENTENCE = "The second sentence comes from a directory that does have a remote."
STAMP = dt.datetime(2026, 10, 16, 21, tzinfo=dt.timezone.utc).timestamp()


def rec(type_, ts, text=None, uuid="u1", cwd="/w/demo-tool", entrypoint="claude-vscode", version="2.1.302",
        sidechain=False):
    r = {"type": type_, "timestamp": ts, "uuid": uuid, "cwd": cwd, "version": version, "isSidechain": sidechain}
    if entrypoint is not None:
        r["entrypoint"] = entrypoint
    if text is not None:
        r["message"] = {"content": [{"type": "text", "text": text}, {"type": "tool_use", "name": "Bash"}]}
    return r


def put(path, records, stamp=STAMP):
    write_jsonl(path, records)
    os.utime(path, (stamp, stamp))
    return path


def put_raw(path, data: bytes, stamp=STAMP):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (stamp, stamp))
    return path


def line_of(rec_):
    return (json.dumps(rec_) + "\n").encode()


def resolver(cwd):
    return "https://github.com/owner/demo-tool.git" if cwd.endswith("demo-tool") else None


def test_sentence_filter():
    ok = C.sentences(GOOD + " See `x` now.\nAnother line here that is long enough to keep, really!")
    assert ok == [GOOD, "Another line here that is long enough to keep, really!"]
    for bad in ("Too short to keep.", "This one has a path like src/app.py inside it today.",
                "This one links https:example and some more words here.", "Fixed in #205 after a long and hard day.",
                "Ping @someone about the very long thing today.", "No ending punctuation on this long sentence here",
                " ".join(["word"] * 31) + "."):
        assert C.sentences(bad) == [], bad


def test_sentence_filter_rejects_links_markup_and_invisible_characters():
    assert C.sentences(GOOD) == [GOOD]  # positive control: the same shape with none of the below is kept
    for bad in ("Read more at www.example.org before the long meeting today.",
                "Read more at example.com before the long meeting today.",
                "Read more at HTTPS://example.org before the long meeting today.",
                "## The new browser pool shipped today and every single test passed.",
                "- The new browser pool shipped today and every single test passed.",
                "The new browser pool shipped today and every single test\u202epassed.",   # bidi override
                "The new browser pool shipped today and every single test\u200bpassed.",   # zero width space
                "The new browser pool shipped today and every single test\x07passed.",     # control character
                "The new browser pool shipped today and every single test\tpassed.",       # tab inside a sentence
                "The new browser pool shipped today and every single test\u00a0passed.",   # no-break space
                "The new browser pool shipped today and every single test\ud800passed.",   # lone surrogate
                "The new browser pool shipped today and every single test\ufffdpassed.",   # undecodable byte
                "The new browser pool shipped today and every single test\ue000passed.",   # private use
                "The new browser pool shipped today and every single test\U000e0041passed."):  # tag character
        assert C.sentences(bad) == [], repr(bad)


def test_session_is_counted_and_quotes_are_taken(tmp_path):  # positive control
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", IN), rec("assistant", IN, GOOD + " See `x` now.", uuid="a1")])
    got = C.scan_claude(DAY, tmp_path, resolver)
    assert (got.sessions, got.subagents) == (1, 0)
    [q] = got.quotes
    assert (q.harness, q.text, q.line, q.record, q.cwd) == ("claude_code", GOOD, 2, "a1", "/w/demo-tool")
    assert q.repo == "https://github.com/owner/demo-tool.git"
    assert q.path.endswith("s1.jsonl") and q.ts == IN


def test_out_of_day_records_make_no_session(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", OUT), rec("assistant", OUT, GOOD)])
    got = C.scan_claude(DAY, tmp_path, resolver)
    assert (got.sessions, got.quotes) == (0, ())


def test_the_local_day_edges_decide_which_records_count(tmp_path):
    # PDT: the day is 07:00:00Z to 07:00:00Z the next UTC day, end exclusive
    first = "The first sentence of the day was written right on the edge."
    last = "The last sentence of the day was written right on the edge."
    put(tmp_path / "-w" / "s1.jsonl", [
        rec("assistant", "2026-10-16T06:59:59.000Z", "This sentence was written one second before the day began."),
        rec("assistant", "2026-10-16T07:00:00.000Z", first, uuid="a2"),
        rec("assistant", "2026-10-17T06:59:59.000Z", last, uuid="a3"),
        rec("assistant", "2026-10-17T07:00:00.000Z", "This sentence was written right when the next day began.")])
    got = C.scan_claude(DAY, tmp_path, resolver)
    assert got.sessions == 1
    assert [q.text for q in got.quotes] == [first, last]


def test_summarizer_runs_are_excluded(tmp_path):
    put(tmp_path / "-w" / "agent-abc.jsonl", [rec("assistant", IN, GOOD)])
    put(tmp_path / "-w" / "s2.jsonl", [rec("user", IN, entrypoint=None, version="2.0.77"),
                                       rec("assistant", IN, GOOD, entrypoint=None, version="2.0.77")])
    put(tmp_path / "-w" / "s3.jsonl", [rec("user", IN)])  # positive control
    got = C.scan_claude(DAY, tmp_path, resolver)
    assert (got.sessions, got.quotes) == (1, ())


def test_the_summarizer_signature_is_version_and_a_missing_entrypoint_together(tmp_path):
    # the same old version WITH an entrypoint is a real session; a missing entrypoint on a current version is too
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", IN, version="2.0.77")])
    put(tmp_path / "-w" / "s2.jsonl", [rec("user", IN, entrypoint=None)])
    put(tmp_path / "-w" / "s3.jsonl", [rec("user", IN, entrypoint=None, version="2.0.77")])
    assert C.scan_claude(DAY, tmp_path, resolver).sessions == 2


def test_a_summarizer_record_anywhere_in_the_file_drops_the_whole_file(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [rec("assistant", IN, GOOD), rec("user", IN, entrypoint=None, version="2.0.77")])
    assert C.scan_claude(DAY, tmp_path, resolver).sessions == 0


def test_subagents_are_counted_without_quotes(tmp_path):
    put(tmp_path / "-w" / "s1" / "subagents" / "agent-1.jsonl", [rec("assistant", IN, GOOD, sidechain=True)])
    got = C.scan_claude(DAY, tmp_path, resolver)
    assert (got.sessions, got.subagents, got.quotes) == (0, 1, ())


def test_subagent_files_outside_the_day_or_not_named_agent_are_not_counted(tmp_path):
    old = dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc).timestamp()
    put(tmp_path / "-w" / "s1" / "subagents" / "agent-1.jsonl", [rec("assistant", IN)], stamp=old)
    put(tmp_path / "-w" / "s1" / "subagents" / "agent-2.jsonl", [rec("assistant", OUT)])
    put(tmp_path / "-w" / "s1" / "subagents" / "notes.jsonl", [rec("assistant", IN)])
    put(tmp_path / "-w" / "s1" / "subagents" / "agent-3.jsonl", [rec("assistant", IN)])  # positive control
    assert C.scan_claude(DAY, tmp_path, resolver).subagents == 1


def test_sidechain_text_is_never_quoted(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", IN), rec("assistant", IN, GOOD, sidechain=True)])
    got = C.scan_claude(DAY, tmp_path, resolver)
    assert (got.sessions, got.quotes) == (1, ())


def test_only_assistant_text_is_quoted(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", IN, GOOD), rec("system", IN, GOOD),
                                       rec("assistant", IN, GOOD, uuid="a1")])
    [q] = C.scan_claude(DAY, tmp_path, resolver).quotes
    assert (q.line, q.record) == (3, "a1")


def test_file_last_written_before_the_day_is_skipped(tmp_path):
    old = dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc).timestamp()
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", IN)], stamp=old)
    assert C.scan_claude(DAY, tmp_path, resolver).sessions == 0


def test_a_cwd_with_no_git_remote_keeps_its_quotes_tagged_none(tmp_path):
    # policy v2: the reader drops nothing for its lane; own_repo says where it was said and the ledger decides
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", IN, cwd="/w/elsewhere"),
                                       rec("assistant", IN, GOOD, cwd="/w/elsewhere"),
                                       rec("assistant", IN, LANE_SENTENCE, uuid="a2")])
    got = C.scan_claude(DAY, tmp_path, resolver)
    assert got.sessions == 1
    assert [(q.text, q.cwd, q.repo, q.own_repo) for q in got.quotes] == [
        (GOOD, "/w/elsewhere", None, None), (LANE_SENTENCE, "/w/demo-tool", "https://github.com/owner/demo-tool.git",
                                             "demo-tool")]
    assert got.no_lane == 1  # still counted, for visibility


def test_the_resolver_is_asked_once_per_directory(tmp_path):
    asked = []

    def counting(cwd):
        asked.append(cwd)
        return resolver(cwd)

    put(tmp_path / "-w" / "s1.jsonl", [rec("assistant", IN, GOOD, uuid="a1"), rec("assistant", IN, GOOD, uuid="a2")])
    put(tmp_path / "-w" / "s2.jsonl", [rec("assistant", IN, GOOD, uuid="a3")])
    assert len(C.scan_claude(DAY, tmp_path, counting).quotes) == 3
    assert asked == ["/w/demo-tool"]


def test_quote_line_number_is_the_exact_line_the_gate_will_read(tmp_path):
    path = put_raw(tmp_path / "-w" / "s1.jsonl", b"".join([
        line_of(rec("user", IN)), b"\n", b"not json at all\n", line_of(rec("assistant", IN, GOOD, uuid="a1"))]))
    [q] = C.scan_claude(DAY, tmp_path, resolver).quotes
    assert q.line == 4  # the blank line and the unreadable line both count
    with open(path, encoding="utf-8", errors="replace") as fh:  # the gate's own way of finding a line
        text = [ln for n, ln in enumerate(fh, 1) if n == q.line][0]
    r = json.loads(text)
    assert r["uuid"] == q.record and q.text in r["message"]["content"][0]["text"]


def test_a_truncated_last_line_is_tolerated_and_counted(tmp_path):
    whole = line_of(rec("assistant", IN, GOOD, uuid="a1"))
    half = line_of(rec("assistant", IN, GOOD, uuid="a2"))[:40]  # a writer caught mid-line: no newline, cut off
    put_raw(tmp_path / "-w" / "s1.jsonl", whole + half)
    got = C.scan_claude(DAY, tmp_path, resolver)
    assert (got.sessions, len(got.quotes), got.skipped) == (1, 1, 1)


def test_unreadable_and_non_object_lines_are_counted_not_fatal(tmp_path):
    put_raw(tmp_path / "-w" / "s1.jsonl", b"{broken\n[1, 2]\n\"text\"\n42\nnull\n\n   \n" + line_of(rec("user", IN)))
    got = C.scan_claude(DAY, tmp_path, resolver)
    assert (got.sessions, got.skipped) == (1, 5)  # blank lines are not skipped records


def test_deeply_nested_json_is_a_skipped_line_not_a_crash(tmp_path):
    put_raw(tmp_path / "-w" / "s1.jsonl", b"[" * 100000 + b"\n" + line_of(rec("user", IN)))
    got = C.scan_claude(DAY, tmp_path, resolver)
    assert (got.sessions, got.skipped) == (1, 1)


def test_undecodable_bytes_do_not_fail_the_read_and_never_reach_a_quote(tmp_path):
    bad = line_of(rec("assistant", IN, "The new browser pool shipped today and every \x00 test passed.", uuid="a1"))
    bad = bad.replace(b"\\u0000", b"\xff\xfe")  # invalid UTF-8 where the escape was
    keep = line_of(rec("assistant", IN, GOOD, uuid="a2"))
    put_raw(tmp_path / "-w" / "s1.jsonl", bad + keep)
    got = C.scan_claude(DAY, tmp_path, resolver)
    assert (got.sessions, [q.text for q in got.quotes], got.skipped) == (1, [GOOD], 0)


def test_hostile_field_values_cannot_reach_the_quote_candidate(tmp_path):
    r = rec("assistant", IN, GOOD, uuid="a1\u202e\nx")
    r2 = rec("assistant", IN, GOOD + " Second.", uuid=["not", "a", "string"])
    r3 = rec("assistant", IN, GOOD + " Third.", uuid="a3", cwd=["not", "a", "string"])
    r4 = rec("assistant", STAMP, GOOD + " Fourth.", uuid="a4")  # inside the day, but not an ISO string
    put(tmp_path / "-w" / "s1.jsonl", [r, r2, r3, r4])
    got = C.scan_claude(DAY, tmp_path, lambda cwd: "https://github.com/owner/demo-tool.git")
    assert [(q.record, q.cwd, q.ts) for q in got.quotes] == [("", "/w/demo-tool", IN), ("", "/w/demo-tool", IN),
                                                            ("a3", "", IN)]


def test_quote_order_is_stable_across_files(tmp_path):
    put(tmp_path / "-w" / "b.jsonl", [rec("assistant", IN, "The second file holds a sentence that is long enough.")])
    put(tmp_path / "-w" / "a.jsonl", [rec("assistant", IN, "The first file holds a sentence that is long enough.")])
    got = C.scan_claude(DAY, tmp_path, resolver)
    assert [os.path.basename(q.path) for q in got.quotes] == ["a.jsonl", "b.jsonl"]


def test_missing_projects_root_raises_instead_of_returning_an_empty_day(tmp_path):
    with pytest.raises(C.ClaudeReadError):
        C.scan_claude(DAY, tmp_path / "nope", resolver)
    (tmp_path / "afile").write_text("x")
    with pytest.raises(C.ClaudeReadError):
        C.scan_claude(DAY, tmp_path / "afile", resolver)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_unreadable_directories_and_files_raise(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", IN)])
    assert C.scan_claude(DAY, tmp_path, resolver).sessions == 1  # positive control: readable, so it counts
    sessions = tmp_path / "-w" / "s1.jsonl"
    sessions.chmod(0)
    try:
        with pytest.raises(C.ClaudeReadError):
            C.scan_claude(DAY, tmp_path, resolver)
    finally:
        sessions.chmod(0o644)
    proj = tmp_path / "-w"
    proj.chmod(0)
    try:
        with pytest.raises(C.ClaudeReadError):
            C.scan_claude(DAY, tmp_path, resolver)
    finally:
        proj.chmod(0o755)
    (tmp_path / "-w" / "s2").mkdir()
    (tmp_path / "-w" / "s2" / "subagents").mkdir()
    (tmp_path / "-w" / "s2" / "subagents").chmod(0)
    try:
        with pytest.raises(C.ClaudeReadError):
            C.scan_claude(DAY, tmp_path, resolver)
    finally:
        (tmp_path / "-w" / "s2" / "subagents").chmod(0o755)


def test_a_session_without_a_subagents_directory_is_normal(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", IN)])
    (tmp_path / "-w" / "s1").mkdir()  # a session directory with no subagents inside
    (tmp_path / "-w" / "s2").write_text("a plain file where a session directory could be")
    assert C.scan_claude(DAY, tmp_path, resolver).sessions == 1


def test_a_relative_root_gives_absolute_source_paths(tmp_path, monkeypatch):
    put(tmp_path / "proj" / "-w" / "s1.jsonl", [rec("assistant", IN, GOOD)])
    monkeypatch.chdir(tmp_path)
    [q] = C.scan_claude(DAY, "proj", resolver).quotes
    assert os.path.isabs(q.path) and os.path.isfile(q.path)


def test_git_repo_for_a_directory_that_cannot_be_asked_is_unresolved(tmp_path):
    # a removed worktree may have belonged to any repository, so it is unknowable, not "no remote"
    for cwd in ("/definitely/not/here", "", str(tmp_path / "gone")):
        with pytest.raises(C.GitUnresolved):
            C.git_repo_for(cwd)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    C.git_repo_for.cache_clear()
    assert C.git_repo_for(str(scratch)) is None  # positive control: a directory that exists and has no remote


def test_git_repo_for_reads_the_origin_remote_of_a_real_checkout(tmp_path):
    repo = tmp_path / "checkout"
    sub = repo / "deep" / "dir"
    sub.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    C.git_repo_for.cache_clear()
    assert C.git_repo_for(str(sub)) is None  # a repository with no remote has no lane
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/owner/demo-tool.git"],
                   check=True)
    C.git_repo_for.cache_clear()  # answers are cached for the life of the process
    assert C.git_repo_for(str(sub)) == "https://github.com/owner/demo-tool.git"
    with pytest.raises(C.GitUnresolved):
        C.git_repo_for(str(sub) + "\x00")


def test_git_repo_for_drops_credentials_from_a_remote_url(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://user:s3cret@github.com/owner/x.git"],
                   check=True)
    got = C.git_repo_for(str(repo))
    assert got == "https://github.com/owner/x.git" and "s3cret" not in got


def test_git_repo_for_ignores_a_relative_directory(tmp_path, monkeypatch):
    # a transcript's cwd is untrusted: a relative one would resolve against THIS process's checkout
    repo = tmp_path / "checkout"
    (repo / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/owner/demo-tool.git"],
                   check=True)
    monkeypatch.chdir(repo)
    assert C.git_repo_for(str(repo)) == "https://github.com/owner/demo-tool.git"  # positive control
    for relative in (".", "sub"):
        with pytest.raises(C.GitUnresolved):
            C.git_repo_for(relative)


# ---- review round: git environment, session repos, content blocks, stricter filter, loud failures ----

URL_A = "https://github.com/owner/repo-a.git"
URL_B = "https://github.com/owner/repo-b.git"


def init_repo(path, url):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", url], check=True)
    return path


def test_git_repo_for_ignores_git_environment_variables(tmp_path, monkeypatch):
    a, b = init_repo(tmp_path / "a", URL_A), init_repo(tmp_path / "b", URL_B)
    monkeypatch.setenv("GIT_DIR", str(b / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(b))
    # positive control: with this environment git itself answers for repo b whatever -C says
    same = subprocess.run(["git", "-C", str(a), "config", "--get", "remote.origin.url"], capture_output=True,
                          text=True).stdout.strip()
    assert same == URL_B
    C.git_repo_for.cache_clear()
    assert C.git_repo_for(str(a)) == URL_A


def test_missing_git_raises_instead_of_reading_as_no_remote(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()

    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)
    C.git_repo_for.cache_clear()
    with pytest.raises(C.ClaudeReadError, match="git_missing"):
        C.git_repo_for(str(work))
    put(tmp_path / "root" / "-w" / "s1.jsonl", [rec("assistant", IN, GOOD, cwd=str(work))])
    with pytest.raises(C.ClaudeReadError, match="git_missing"):
        C.scan_claude(DAY, tmp_path / "root", C.git_repo_for)


def test_a_git_timeout_is_counted_apart_from_no_remote(tmp_path, monkeypatch):
    slow, bare = tmp_path / "slow", tmp_path / "bare"
    slow.mkdir()
    bare.mkdir()
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if str(slow) in cmd:
            raise subprocess.TimeoutExpired(cmd, 10)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    C.git_repo_for.cache_clear()
    put(tmp_path / "root" / "-w" / "s1.jsonl", [
        rec("assistant", IN, GOOD, cwd=str(slow), uuid="a1"),
        rec("assistant", IN, LANE_SENTENCE, cwd=str(bare), uuid="a2"),
        rec("assistant", IN, LANE_SENTENCE + " Twice.", cwd=str(bare), uuid="a3")])
    got = C.scan_claude(DAY, tmp_path / "root", C.git_repo_for)
    assert [q.own_repo for q in got.quotes] == ["!unresolved", None, None]  # kept, each tagged with what git said
    assert (got.git_timeouts, got.unresolved, got.no_lane) == (1, 1, 3)  # no_lane: None or unresolved, per sentence


def test_an_empty_projects_root_raises(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(C.ClaudeReadError, match="empty_root"):
        C.scan_claude(DAY, empty, resolver)
    (empty / "stray-file").write_text("x")  # files alone are not projects
    with pytest.raises(C.ClaudeReadError, match="empty_root"):
        C.scan_claude(DAY, empty, resolver)
    put(empty / "-w" / "s1.jsonl", [rec("user", IN)])  # positive control: one project and it reads
    assert C.scan_claude(DAY, empty, resolver).sessions == 1


def test_only_text_blocks_are_ever_quoted(tmp_path):
    only = "The only quotable sentence in this record sits in the text block."
    junk = [{"type": "thinking", "thinking": GOOD, "text": GOOD},
            {"type": "redacted_thinking", "data": GOOD},
            {"type": "tool_use", "name": "Bash", "input": {"command": GOOD, "text": GOOD, "description": GOOD}},
            {"type": "tool_result", "content": [{"type": "text", "text": GOOD}]},
            {"type": "image", "text": GOOD}]
    a = rec("assistant", IN, uuid="a1")
    a["message"] = {"content": junk}
    b = rec("assistant", IN, uuid="a2")
    b["message"] = {"content": junk + [{"type": "text", "text": only}]}
    put(tmp_path / "-w" / "s1.jsonl", [a, b])
    got = C.scan_claude(DAY, tmp_path, resolver)
    assert [(q.text, q.record) for q in got.quotes] == [(only, "a2")]


def test_sentence_filter_rejects_non_ascii_letters_and_ip_addresses():
    assert C.sentences(GOOD) == [GOOD]  # positive control: the same shape with none of the below is kept
    for ok in ("Version 2.1.302 shipped today and every single test passed.",       # three parts is not an address
               "It" + chr(0x2019) + "s shipped today and every single test passed.",  # non-ASCII punctuation is fine
               "The build took 3.5 seconds on 1.2.3 and every test passed."):
        assert C.sentences(ok) == [ok], ok
    cyrillic_o, e_acute, fullwidth_one = chr(0x43E), chr(0xE9), chr(0xFF11)
    for bad in (GOOD.replace("browser", "br" + cyrillic_o + "wser"),                 # lookalike letter
                GOOD.replace("browser", "caf" + e_acute),                             # accented letter
                GOOD.replace("single", fullwidth_one + "x"),                          # lookalike digit
                "Connect to 10.0.0.1 before the long meeting begins today.",
                "The host at 203.0.113.24 answered every single probe today.",
                "The first address is 192.168.1.20, and the rest follow it."):
        assert C.sentences(bad) == [], repr(bad)


def test_session_repos_lists_every_in_day_directory_the_session_used(tmp_path):
    urls = {"/w/demo-tool": "https://github.com/owner/demo-tool.git", "/w/other": "git@github.com:owner/Other-Repo.git",
            "/w/yesterday": "https://github.com/owner/yesterday.git", "/w/sub-only": "https://github.com/o/sub-only.git"}
    put(tmp_path / "-w" / "s1.jsonl", [
        rec("user", OUT, cwd="/w/yesterday"),          # outside the day: not listed
        rec("user", IN),                               # demo-tool
        rec("system", IN, cwd="/w/other"),             # any record type counts
        rec("assistant", IN, cwd="/w/nowhere"),        # no remote: stands as None
        rec("assistant", IN, GOOD, uuid="a1")])
    put(tmp_path / "-w" / "s1" / "subagents" / "agent-1.jsonl", [rec("assistant", IN, cwd="/w/sub-only", sidechain=True)])
    put(tmp_path / "-w" / "s2.jsonl", [rec("assistant", IN, LANE_SENTENCE, uuid="b1")])
    got = C.scan_claude(DAY, tmp_path, urls.get)
    by = {os.path.basename(q.path): q for q in got.quotes}
    assert by["s1.jsonl"].session_repos == (None, "demo-tool", "other-repo")  # None first, then sorted names
    assert by["s2.jsonl"].session_repos == ("demo-tool",)  # a second session keeps its own list


def test_a_hand_built_candidate_defaults_to_the_unresolved_marker():
    q = C.QuoteCandidate("claude_code", "r", "/c", "t", "ts", "/p", 1, "u")
    assert q.session_repos == ("!unresolved",) == (C.UNRESOLVED,)  # a marker no lane list can contain as a name
    assert None not in q.session_repos  # "no remote" is a different answer from "not asked"


def test_session_cwds_lists_the_raw_in_day_directories_of_the_session_file(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [
        rec("user", OUT, cwd="/w/yesterday"),          # outside the day: not listed
        rec("user", IN),                               # /w/demo-tool
        rec("system", IN, cwd="/w/other"),             # any record type counts
        rec("assistant", IN, cwd="/w/nowhere"),        # no remote, still listed as the raw path
        rec("assistant", IN, cwd="/w/other"),          # distinct: listed once
        rec("assistant", IN, GOOD, uuid="a1")])
    put(tmp_path / "-w" / "s1" / "subagents" / "agent-1.jsonl", [rec("assistant", IN, cwd="/w/sub-only", sidechain=True)])
    put(tmp_path / "-w" / "s2.jsonl", [rec("assistant", IN, LANE_SENTENCE, uuid="b1")])
    got = C.scan_claude(DAY, tmp_path, resolver)
    by = {os.path.basename(q.path): q for q in got.quotes}
    assert by["s1.jsonl"].session_cwds == ("/w/demo-tool", "/w/nowhere", "/w/other")  # sorted, raw, in-day, top-level
    assert by["s2.jsonl"].session_cwds == ("/w/demo-tool",)  # a second session keeps its own list


def test_session_cwds_default_is_unknown_and_stays_empty():
    q = C.QuoteCandidate("claude_code", "r", "/c", "t", "ts", "/p", 1, "u")
    assert q.session_cwds == ()


# ---- review round 3: a directory git could not answer for is "unresolved", never "no remote" ----

def fake_git(code, out=""):
    def run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")
    return run


@pytest.mark.parametrize("code,out,want", [(0, "https://github.com/owner/x.git\n", "https://github.com/owner/x.git"),
                                           (1, "", None),   # the key is not set: a genuine "no remote"
                                           (0, "\n", None)])  # the key is set to nothing
def test_git_exit_codes_zero_and_one_are_answers(tmp_path, monkeypatch, code, out, want):
    monkeypatch.setattr(subprocess, "run", fake_git(code, out))
    C.git_repo_for.cache_clear()
    assert C.git_repo_for(str(tmp_path)) == want


@pytest.mark.parametrize("code", [2, 3, 128, 129, -9])
def test_any_other_git_exit_code_is_unresolved_not_no_remote(tmp_path, monkeypatch, code):
    monkeypatch.setattr(subprocess, "run", fake_git(code))
    C.git_repo_for.cache_clear()
    with pytest.raises(C.GitUnresolved):
        C.git_repo_for(str(tmp_path))


def test_a_git_that_cannot_run_is_unresolved(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("git")

    monkeypatch.setattr(subprocess, "run", denied)
    C.git_repo_for.cache_clear()
    with pytest.raises(C.GitUnresolved):
        C.git_repo_for(str(tmp_path))


def test_a_timeout_is_a_kind_of_unresolved():
    assert issubclass(C.GitTimeout, C.GitUnresolved)


def test_a_timed_out_directory_marks_the_session_unresolved_not_no_remote(tmp_path, monkeypatch):
    good, slow = init_repo(tmp_path / "good", URL_A), tmp_path / "slow"
    slow.mkdir()
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if str(slow) in cmd:
            raise subprocess.TimeoutExpired(cmd, 10)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    C.git_repo_for.cache_clear()
    put(tmp_path / "root" / "-w" / "s1.jsonl", [rec("system", IN, cwd=str(slow)),
                                                rec("assistant", IN, GOOD, cwd=str(good))])
    got = C.scan_claude(DAY, tmp_path / "root", C.git_repo_for)
    [q] = got.quotes
    assert q.session_repos == ("!unresolved", "repo-a")  # not (None, "repo-a")
    assert (got.unresolved, got.git_timeouts, got.no_lane) == (1, 1, 0)


def test_a_corrupt_repository_marks_the_session_unresolved(tmp_path):
    good, corrupt, bare = init_repo(tmp_path / "good", URL_A), tmp_path / "corrupt", tmp_path / "bare"
    subprocess.run(["git", "init", "-q", str(corrupt)], check=True)
    (corrupt / ".git" / "config").write_text("[[[ not a config file\n")
    subprocess.run(["git", "init", "-q", str(bare)], check=True)
    code = subprocess.run(["git", "-C", str(corrupt), "config", "--get", "remote.origin.url"],
                          capture_output=True).returncode
    assert code not in (0, 1)  # positive control: git really answers neither yes nor no here
    C.git_repo_for.cache_clear()
    put(tmp_path / "root" / "-w" / "s1.jsonl", [rec("system", IN, cwd=str(corrupt)), rec("system", IN, cwd=str(bare)),
                                                rec("assistant", IN, GOOD, cwd=str(good))])
    got = C.scan_claude(DAY, tmp_path / "root", C.git_repo_for)
    [q] = got.quotes
    # the corrupt repository is unresolved; the repository with no origin is a genuine None
    assert q.session_repos == (None, "!unresolved", "repo-a")
    assert (got.unresolved, got.git_timeouts, got.no_lane) == (1, 0, 0)


def test_a_quote_from_an_unresolved_directory_is_kept_tagged_unresolved(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_git(128))
    C.git_repo_for.cache_clear()
    put(tmp_path / "root" / "-w" / "s1.jsonl", [rec("assistant", IN, GOOD, cwd=str(tmp_path))])
    got = C.scan_claude(DAY, tmp_path / "root", C.git_repo_for)
    [q] = got.quotes
    assert (q.repo, q.own_repo, q.session_repos) == (None, "!unresolved", ("!unresolved",))  # never None
    assert (got.unresolved, got.no_lane) == (1, 1)


def test_a_deleted_directory_marks_the_session_unresolved(tmp_path):
    good, scratch, gone = init_repo(tmp_path / "good", URL_A), tmp_path / "scratch", tmp_path / "gone"
    scratch.mkdir()
    gone.mkdir()
    gone.rmdir()  # a worktree removed after the session ran
    C.git_repo_for.cache_clear()
    put(tmp_path / "root" / "-w" / "s1.jsonl", [rec("system", IN, cwd=str(gone)), rec("system", IN, cwd=str(scratch)),
                                                rec("assistant", IN, GOOD, cwd=str(good))])
    got = C.scan_claude(DAY, tmp_path / "root", C.git_repo_for)
    [q] = got.quotes
    # the deleted directory is unresolved; the scratch directory that exists with no remote is a genuine None
    assert q.session_repos == (None, "!unresolved", "repo-a")
    assert (got.unresolved, got.no_lane, got.git_timeouts) == (1, 0, 0)


def test_a_quote_from_a_deleted_directory_is_kept_tagged_unresolved(tmp_path):
    gone = tmp_path / "gone"
    put(tmp_path / "root" / "-w" / "s1.jsonl", [rec("assistant", IN, GOOD, cwd=str(gone))])
    C.git_repo_for.cache_clear()
    got = C.scan_claude(DAY, tmp_path / "root", C.git_repo_for)
    [q] = got.quotes
    assert (q.own_repo, q.session_cwds) == ("!unresolved", (str(gone),))
    assert (got.unresolved, got.no_lane) == (1, 1)


# ---- policy v2: every candidate kept and tagged, one note per session, sessions that touch an excluded path ----

THIRD = "A third sentence was said in a directory that has since been removed."
PRIVATE = "/home/x/diary-app/notes.md"
FRAG = ("diary-app",)


def ref_of(session_id):
    import hashlib
    return hashlib.sha256(session_id.encode()).hexdigest()[:8]


def title(kind, text, sid="s1"):
    return {"type": kind, {"ai-title": "aiTitle", "custom-title": "customTitle"}[kind]: text, "sessionId": sid}


def tool(ts, input_, uuid="t1", cwd="/w/demo-tool", sidechain=False, block="tool_use"):
    r = rec("assistant", ts, uuid=uuid, cwd=cwd, sidechain=sidechain)
    r["message"] = {"content": [{"type": block, "id": "toolu_1", "name": "Read", "input": input_}]}
    return r


def attachment(ts, att, cwd="/w/demo-tool"):
    return {"type": "attachment", "timestamp": ts, "cwd": cwd, "uuid": "at1", "attachment": att}


def test_candidates_are_kept_whatever_the_lane_and_tagged_with_own_repo(tmp_path):
    good, bare, gone = init_repo(tmp_path / "good", URL_A), tmp_path / "bare", tmp_path / "gone"
    bare.mkdir()
    C.git_repo_for.cache_clear()
    put(tmp_path / "root" / "-w" / "s1.jsonl", [rec("assistant", IN, GOOD, cwd=str(good), uuid="a1"),
                                                rec("assistant", IN, LANE_SENTENCE, cwd=str(bare), uuid="a2"),
                                                rec("assistant", IN, THIRD, cwd=str(gone), uuid="a3")])
    got = C.scan_claude(DAY, tmp_path / "root", C.git_repo_for)
    assert [(q.text, q.repo, q.own_repo) for q in got.quotes] == [
        (GOOD, URL_A, "repo-a"), (LANE_SENTENCE, None, None), (THIRD, None, "!unresolved")]
    assert {q.session_repos for q in got.quotes} == {(None, "!unresolved", "repo-a")}
    assert (got.no_lane, got.unresolved) == (2, 1)


def test_a_hand_built_candidate_puts_own_repo_after_session_cwds_with_safe_defaults():
    q = C.QuoteCandidate("claude_code", "r", "/c", "t", "ts", "/p", 1, "u")
    assert (q.own_repo, q.touches_excluded) == ("!unresolved", False)
    q = C.QuoteCandidate("claude_code", "r", "/c", "t", "ts", "/p", 1, "u", ("x",), ("/c",), "x", True)
    assert (q.session_repos, q.session_cwds, q.own_repo, q.touches_excluded) == (("x",), ("/c",), "x", True)


def test_every_active_main_session_gets_a_note_with_its_scope(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [title("ai-title", "Rewrite the parser"), rec("user", IN),
                                       rec("assistant", IN, GOOD, uuid="a1")])
    put(tmp_path / "-w" / "s2.jsonl", [rec("user", IN, cwd="/w/elsewhere")])  # active, nothing to quote, no title
    put(tmp_path / "-w" / "s3.jsonl", [title("ai-title", "Only yesterday", "s3"), rec("user", OUT)])  # not active
    put(tmp_path / "-w" / "s1" / "subagents" / "agent-1.jsonl", [rec("assistant", IN, GOOD, sidechain=True)])
    got = C.scan_claude(DAY, tmp_path, resolver)
    assert (got.sessions, got.subagents, len(got.quotes)) == (2, 1, 1)
    assert got.notes == (
        C.SessionNote("claude_code", ref_of("s1"), "Rewrite the parser", ("demo-tool",), ("/w/demo-tool",), False, IN),
        C.SessionNote("claude_code", ref_of("s2"), "", (None,), ("/w/elsewhere",), False, IN))
    assert got.notes[0].touches_excluded is False


def test_a_note_ref_hashes_the_session_id_the_records_carry(tmp_path):
    sid = "0b5c6e2a-1111-4222-8333-444455556666"
    r = rec("user", IN)
    r["sessionId"] = sid
    put(tmp_path / "-w" / "renamed.jsonl", [r])  # the records name the session, not the file
    put(tmp_path / "-w" / "s2.jsonl", [rec("user", IN)])  # no sessionId anywhere: the file name stands in
    a, b = C.scan_claude(DAY, tmp_path, resolver).notes
    assert (a.ref, b.ref) == (ref_of(sid), ref_of("s2"))
    assert len(a.ref) == 8 and sid[:8] not in a.ref + a.title


def test_a_note_title_is_the_latest_ai_or_custom_title(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [title("ai-title", "First guess"), rec("user", IN),
                                       title("custom-title", "Named by hand"),
                                       title("ai-title", "Latest machine title")])
    put(tmp_path / "-w" / "s2.jsonl", [title("ai-title", "Older machine title", "s2"),
                                       title("custom-title", "Named by hand last", "s2"), rec("user", IN),
                                       {"type": "ai-title", "aiTitle": ["not", "a", "string"], "sessionId": "s2"},
                                       title("ai-title", "‮​", "s2")])  # nothing left after sanitizing
    assert [n.title for n in C.scan_claude(DAY, tmp_path, resolver).notes] == ["Latest machine title",
                                                                               "Named by hand last"]


def test_a_note_title_is_sanitized_and_capped(tmp_path):
    nasty = "Fix\x07 the‮ bidi​  and\ttabs\nand lines " + "word " * 60
    put(tmp_path / "-w" / "s1.jsonl", [title("custom-title", nasty), rec("user", IN)])
    [note] = C.scan_claude(DAY, tmp_path, resolver).notes
    assert note.title.startswith("Fix the bidi and tabs and lines word word")
    assert len(note.title) == 120 and note.title == note.title.rstrip()
    assert not any(ord(ch) < 32 or ch in "‮​" for ch in note.title)


def test_a_session_whose_tool_call_reads_an_excluded_path_is_tainted(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", IN), tool(IN, {"file_path": PRIVATE}),
                                       rec("assistant", IN, GOOD, uuid="a1")])
    put(tmp_path / "-w" / "s2.jsonl", [rec("user", IN), tool(IN, {"file_path": "/home/x/demo-tool/notes.md"}),
                                       rec("assistant", IN, LANE_SENTENCE, uuid="b1")])  # positive control
    got = C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=FRAG)
    assert [(os.path.basename(q.path), q.touches_excluded) for q in got.quotes] == [("s1.jsonl", True),
                                                                                   ("s2.jsonl", False)]
    assert [(n.ref, n.touches_excluded) for n in got.notes] == [(ref_of("s1"), True), (ref_of("s2"), False)]


def test_without_fragments_nothing_is_tainted(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", IN, cwd="/home/x/diary-app"), tool(IN, {"file_path": PRIVATE}),
                                       rec("assistant", IN, GOOD, uuid="a1")])
    got = C.scan_claude(DAY, tmp_path, resolver)
    assert [q.touches_excluded for q in got.quotes] == [False]
    assert [n.touches_excluded for n in got.notes] == [False]


def test_a_subagent_touching_an_excluded_path_taints_its_parent_session(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", IN), rec("assistant", IN, GOOD, uuid="a1")])
    put(tmp_path / "-w" / "s1" / "subagents" / "agent-1.jsonl",
        [tool(IN, {"command": f"cat {PRIVATE}"}, sidechain=True)])
    put(tmp_path / "-w" / "s2.jsonl", [rec("user", IN), rec("assistant", IN, LANE_SENTENCE, uuid="b1")])
    put(tmp_path / "-w" / "s2" / "subagents" / "agent-1.jsonl",  # positive control: a sibling's clean subagent
        [tool(IN, {"command": "cat /home/x/demo-tool/notes.md"}, sidechain=True)])
    got = C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=FRAG)
    assert got.subagents == 2
    assert [(os.path.basename(q.path), q.touches_excluded) for q in got.quotes] == [("s1.jsonl", True),
                                                                                   ("s2.jsonl", False)]
    assert [n.touches_excluded for n in got.notes] == [True, False]


def test_a_subagent_in_a_working_directory_that_is_excluded_taints_its_parent(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", IN), rec("assistant", IN, GOOD, uuid="a1")])
    put(tmp_path / "-w" / "s1" / "subagents" / "agent-1.jsonl",
        [rec("assistant", IN, cwd="/home/x/diary-app", sidechain=True)])
    got = C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [True]
    assert got.notes[0].session_cwds == ("/w/demo-tool",)  # the subagent taints; it does not widen the scope


def test_exclusion_matching_ignores_case(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [tool(IN, {"file_path": "/home/x/Diary-App/Notes.md"}),
                                       rec("assistant", IN, GOOD, uuid="a1")])
    assert [n.touches_excluded for n in C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=FRAG).notes] == [True]
    assert [n.touches_excluded
            for n in C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=("DIARY-APP",)).notes] == [True]
    assert [n.touches_excluded  # positive control: an unrelated fragment matches nothing
            for n in C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=("other-thing",)).notes] == [False]


def test_an_excluded_working_directory_taints_the_session(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [rec("system", IN, cwd="/home/x/diary-app"),
                                       rec("assistant", IN, GOOD, uuid="a1")])
    put(tmp_path / "-w" / "s2.jsonl", [rec("assistant", IN, LANE_SENTENCE, uuid="b1")])  # positive control
    got = C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=FRAG)
    assert [(q.text, q.touches_excluded) for q in got.quotes] == [(GOOD, True), (LANE_SENTENCE, False)]


def test_every_kind_of_tool_use_block_and_any_field_of_its_input_counts(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [tool(IN, {"query": "notes", "paths": [{"p": "/home/x/diary-app"}]},
                                            block="server_tool_use"), rec("assistant", IN, GOOD, uuid="a1")])
    put(tmp_path / "-w" / "s2.jsonl", [tool(IN, {"command": "ls"}, sidechain=True),
                                       tool(IN, {"file_path": PRIVATE}, uuid="t2", sidechain=True),
                                       rec("assistant", IN, LANE_SENTENCE, uuid="b1")])
    got = C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [True, True]


def test_a_file_brought_in_without_a_tool_call_taints_the_session(tmp_path):
    # an @-mentioned file reaches the session as an attachment carrying its path, with no tool call at all
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", IN), attachment(IN, {"type": "file", "filename": PRIVATE,
                                                                        "displayPath": "notes.md", "content": "x"}),
                                       rec("assistant", IN, GOOD, uuid="a1")])
    # positive control: instructions that merely NAME the folder are not a touch of it
    put(tmp_path / "-w" / "s2.jsonl", [rec("user", IN), attachment(IN, {"type": "nested_memory",
                                                                        "path": "/w/demo-tool/CLAUDE.md",
                                                                        "content": "never read diary-app"}),
                                       rec("assistant", IN, LANE_SENTENCE, uuid="b1")])
    got = C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [True, False]


def test_text_that_names_the_folder_is_not_a_touch(tmp_path):
    named = "The diary-app folder stays private and nobody reads it today."
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", IN, "please look at diary-app"),
                                       rec("assistant", IN, named, uuid="a1")])
    got = C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=FRAG)
    assert [(q.text, q.touches_excluded) for q in got.quotes] == [(named, False)]  # the gate's names check owns this


def test_exclude_fragments_must_be_a_collection_of_strings(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", IN)])
    with pytest.raises(TypeError):
        C.scan_claude(DAY, tmp_path, resolver, exclude_fragments="diary-app")  # would match single letters
    with pytest.raises(TypeError):
        C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=(1,))
    assert C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=["diary-app"]).sessions == 1  # a list is fine


# ---- follow-up: taint from the whole session history, last_ts on notes, fragments_checked ----

BEFORE_DAY = dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc).timestamp()


def test_a_session_that_touched_the_excluded_path_the_day_before_is_tainted_today(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [tool(OUT, {"file_path": PRIVATE}),  # yesterday's read is still in context
                                       rec("assistant", OUT, THIRD, uuid="a0"),
                                       rec("assistant", IN, GOOD, uuid="a1")])
    put(tmp_path / "-w" / "s2.jsonl", [tool(OUT, {"file_path": "/home/x/demo-tool/notes.md"}),  # positive control
                                       rec("assistant", IN, LANE_SENTENCE, uuid="b1")])
    got = C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=FRAG)
    assert [(q.text, q.touches_excluded) for q in got.quotes] == [(GOOD, True), (LANE_SENTENCE, False)]  # in-day only
    assert [n.touches_excluded for n in got.notes] == [True, False]


def test_an_old_subagent_file_that_touched_the_excluded_path_taints_its_parent(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [rec("assistant", IN, GOOD, uuid="a1")])
    put(tmp_path / "-w" / "s1" / "subagents" / "agent-1.jsonl", [tool(OUT, {"file_path": PRIVATE}, sidechain=True)],
        stamp=BEFORE_DAY)  # last written before the day: not counted, still read for the taint
    put(tmp_path / "-w" / "s2.jsonl", [rec("assistant", IN, LANE_SENTENCE, uuid="b1")])
    put(tmp_path / "-w" / "s2" / "subagents" / "agent-1.jsonl",  # positive control
        [tool(OUT, {"file_path": "/home/x/demo-tool/notes.md"}, sidechain=True)], stamp=BEFORE_DAY)
    got = C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=FRAG)
    assert got.subagents == 0
    assert [n.touches_excluded for n in got.notes] == [True, False]
    assert C.scan_claude(DAY, tmp_path, resolver).subagents == 0  # positive control: still not counted


def test_a_note_carries_the_latest_in_day_record_time_in_utc(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", "2026-10-16T09:00:00.000Z"),
                                       rec("system", "2026-10-16T23:30:00+02:00"),       # 21:30 UTC, any record type
                                       rec("assistant", "2026-10-16T20:00:00.5Z", GOOD),
                                       rec("user", "2026-10-17T08:00:00.000Z")])           # the next local day
    put(tmp_path / "-w" / "s2.jsonl", [rec("user", "2026-10-16T08:00:00.000Z")])
    a, b = C.scan_claude(DAY, tmp_path, resolver).notes
    assert (a.last_ts, b.last_ts) == ("2026-10-16T21:30:00.000Z", "2026-10-16T08:00:00.000Z")
    assert sorted([a, b], key=lambda n: n.last_ts, reverse=True)[0] is a  # what the ledger sorts by


def test_the_number_of_fragments_applied_is_reported(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", IN)])
    assert C.scan_claude(DAY, tmp_path, resolver).fragments_checked == 0
    got = C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=("diary-app", " DIARY-APP ", "client-work"))
    assert got.fragments_checked == 2  # after stripping, lowercasing and dropping repeats
    from daily.sources.transcripts import digest_fragments
    assert C.scan_claude(DAY, tmp_path, resolver).fragments_digest == ""
    assert got.fragments_digest == digest_fragments(("client-work", "diary-app")) != ""  # the set, in any order


# ---- review round: workflow agents, tool output, more attachment paths, a remote with no usable name ----

def tool_result(ts, content, extra=None, cwd="/w/demo-tool"):
    r = rec("user", ts, cwd=cwd)
    r["message"] = {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": content}]}
    if extra is not None:
        r["toolUseResult"] = extra
    return r


def test_a_workflow_agent_that_touches_an_excluded_path_taints_its_parent(tmp_path):
    wf = tmp_path / "-w" / "s1" / "subagents" / "workflows" / "wf_abc"
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", IN), rec("assistant", IN, GOOD, uuid="a1")])
    put(wf / "agent-x.jsonl", [tool(IN, {"file_path": PRIVATE}, sidechain=True),
                               rec("assistant", IN, THIRD, uuid="w1", sidechain=True)])
    put(wf / "journal.jsonl", [{"type": "started", "timestamp": IN}])  # a run log, not a transcript
    put(tmp_path / "-w" / "s2.jsonl", [rec("user", IN), rec("assistant", IN, LANE_SENTENCE, uuid="b1")])
    put(tmp_path / "-w" / "s2" / "subagents" / "workflows" / "wf_def" / "agent-y.jsonl",  # positive control
        [tool(IN, {"file_path": "/home/x/demo-tool/notes.md"}, sidechain=True)])
    put(tmp_path / "-w" / "s3.jsonl", [rec("user", IN)])
    put(tmp_path / "-w" / "s3" / "subagents" / "workflows" / "wf_old" / "agent-z.jsonl",
        [tool(OUT, {"file_path": PRIVATE}, sidechain=True)], stamp=BEFORE_DAY)  # old: read for the taint only
    got = C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=FRAG)
    assert got.subagents == 2  # the two workflow agents of the day; never the journal, never the old one
    assert [q.text for q in got.quotes] == [GOOD, LANE_SENTENCE]  # a workflow agent is never quoted
    assert [n.touches_excluded for n in got.notes] == [True, False, True]


@pytest.mark.parametrize("content,extra", [
    ("/home/x/Diary-App/notes.md:3: tracker", None),                            # a grep that listed the folder
    ([{"type": "text", "text": "found /home/x/diary-app/notes.md"}], None),    # the list form of a result
    ("done", {"filenames": ["/home/x/diary-app/notes.md"]}),                    # the structured result
    ("done", "C:\\Users\\x\\diary-app\\notes.md")])                     # a Windows separator
def test_a_tool_output_listing_an_excluded_path_taints_the_session(tmp_path, content, extra):
    put(tmp_path / "-w" / "s1.jsonl", [tool(IN, {"command": "grep -rl tracker /home/x"}),
                                       tool_result(IN, content, extra), rec("assistant", IN, GOOD, uuid="a1")])
    got = C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [True]


def test_a_tool_output_naming_the_folder_bare_is_not_a_touch(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [tool(IN, {"command": "ls /home/x"}),
                                       tool_result(IN, "demo-tool\ndiary-app\nnotes", ["diary-app", "x"]),
                                       rec("assistant", IN, GOOD, uuid="a1")])
    got = C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [False]


@pytest.mark.parametrize("att,hit", [
    ({"type": "instructions", "files": [{"path": "/home/x/diary-app/CLAUDE.md", "content": "x", "type": "Project"}]},
     True),
    ({"type": "inlined_image_paths", "paths": ["/tmp/a.png", "/home/x/Diary-App/shot.png"]}, True),
    ({"type": "instructions", "files": [{"path": "/w/demo-tool/CLAUDE.md", "content": "never read diary-app/",
                                         "type": "Project"}]}, False)])  # what an instruction SAYS is not a touch
def test_instruction_files_and_inlined_image_paths_count(tmp_path, att, hit):
    put(tmp_path / "-w" / "s1.jsonl", [rec("user", IN), attachment(IN, att), rec("assistant", IN, GOOD, uuid="a1")])
    got = C.scan_claude(DAY, tmp_path, resolver, exclude_fragments=FRAG)
    assert [n.touches_excluded for n in got.notes] == [hit]


def test_a_remote_with_no_usable_name_is_unresolved_never_no_remote(tmp_path):
    put(tmp_path / "-w" / "s1.jsonl", [rec("assistant", IN, GOOD, uuid="a1")])
    got = C.scan_claude(DAY, tmp_path, lambda cwd: "/")  # a remote string that names no repository
    [q] = got.quotes
    assert (q.own_repo, q.session_repos, got.unresolved) == ("!unresolved", ("!unresolved",), 1)
    got = C.scan_claude(DAY, tmp_path, lambda cwd: "https://github.com/owner/demo-tool.git")  # positive control
    assert (got.quotes[0].own_repo, got.unresolved) == ("demo-tool", 0)



def test_the_sentence_filter_shares_the_ledger_path_rule_and_keeps_its_host_rule():
    for bad in ("We blocked javascript:void on the page and the run finished well today.",
                "We saw a mailto:admin@example.org link in the page footer today.",
                "Read more at example.com before the long meeting today."):  # a bare host: quotes only
        assert C.sentences(bad) == [], bad
    for good in ("We looked at the data: every run finished on time and the pool held up.",
                 "So one file: the config, and every single test passed today."):
        assert C.sentences(good) == [good], good  # positive controls: a colon in prose



def test_contains_fragment_reads_text_in_its_normal_form():  # pipeline finding #10
    from daily.sources.transcripts import contains_fragment
    frags = ("diary-app",)
    assert contains_fragment("/w/diary" + chr(0x200B) + "-app/x", frags)
    assert contains_fragment("/w/" + "".join(chr(ord(c) + 0xFEE0) for c in "DIARY-APP") + "/x", frags)
    assert contains_fragment("/W/Diary-App/x", frags)  # the ASCII fast path still lowercases
    assert not contains_fragment("/w/diary/app", frags) and not contains_fragment("/w/caf" + chr(0xE9), frags)
