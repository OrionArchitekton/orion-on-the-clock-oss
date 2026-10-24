import dataclasses
import datetime as dt
import json
import subprocess

import pytest

from daily.sources import github

DAY = dt.date(2026, 10, 16)


class FakeGh:
    def __init__(self, search, heads=None):
        self.search, self.heads, self.calls = search, heads or {}, []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:3] == ["gh", "search", "prs"]:
            return json.dumps(self.search)
        if argv[:3] == ["gh", "pr", "view"]:
            return json.dumps({"headRefName": self.heads[(argv[5], int(argv[3]))]})
        raise AssertionError(argv)


def pr(n, closed, repo="owner/demo-tool"):
    return {"repository": {"name": repo.split("/")[1], "nameWithOwner": repo}, "number": n,
            "title": f"t{n}", "body": f"b{n}", "url": f"https://github.com/{repo}/pull/{n}", "closedAt": closed}


def test_search_uses_the_local_range_form():
    gh = FakeGh([])
    github.merged_prs(DAY, gh, owner="demo-owner")
    assert "2026-10-16T00:00:00-07:00..2026-10-16T23:59:59-07:00" in gh.calls[0]
    assert gh.calls[0][:5] == ["gh", "search", "prs", "--owner", "demo-owner"]


def test_keeps_only_prs_closed_inside_the_local_day():
    gh = FakeGh([pr(1, "2026-10-16T21:00:00Z"), pr(2, "2026-10-16T05:00:00Z"), pr(3, "2026-10-17T06:59:00Z")])
    got = github.merged_prs(DAY, gh, owner="demo-owner")
    assert [p.number for p in got] == [1, 3]
    assert got[0].repo == "owner/demo-tool" and got[0].harness == "unknown"


def test_harness_from_ref():
    assert github.harness_from_ref("codex/fix-x") == "codex"
    assert github.harness_from_ref("claude/docs-v1") == "claude_code"
    assert github.harness_from_ref("sam/hotfix") == "unknown"
    assert github.harness_from_ref(None) == "unknown"


def test_attribute_only_asks_for_wanted_prs():
    gh = FakeGh([pr(1, "2026-10-16T21:00:00Z"), pr(2, "2026-10-16T22:00:00Z", "owner/garden-shed")],
                heads={("owner/demo-tool", 1): "codex/x"})
    prs = github.merged_prs(DAY, gh, owner="demo-owner")
    got = github.attribute(prs, gh, want=lambda p: p.repo == "owner/demo-tool")
    assert [(p.number, p.harness) for p in got] == [(1, "codex"), (2, "unknown")]
    assert sum(1 for c in gh.calls if c[:3] == ["gh", "pr", "view"]) == 1


def test_search_asks_for_the_max_limit():
    gh = FakeGh([])
    github.merged_prs(DAY, gh, owner="demo-owner")
    assert gh.calls[0][gh.calls[0].index("--limit") + 1] == "1000"


def test_a_capped_search_raises_instead_of_returning_a_partial_day():
    gh = FakeGh([pr(i, "2026-10-16T21:00:00Z") for i in range(1000)])
    with pytest.raises(github.SearchTruncated) as exc:
        github.merged_prs(DAY, gh, owner="demo-owner")
    assert "1000" in str(exc.value)


def test_one_under_the_cap_is_a_complete_day():
    gh = FakeGh([pr(i, "2026-10-16T21:00:00Z") for i in range(999)])
    assert len(github.merged_prs(DAY, gh, owner="demo-owner")) == 999


def prs_for(*numbers):
    return github.merged_prs(DAY, FakeGh([pr(n, "2026-10-16T21:00:00Z") for n in numbers]), owner="demo-owner")


def test_attribute_fails_soft_when_one_lookup_fails():
    def runner(argv):
        n = int(argv[3])
        if n == 2:
            raise subprocess.CalledProcessError(1, argv)
        return json.dumps({"headRefName": {1: "codex/a", 3: "claude/c"}[n]})

    got = github.attribute(prs_for(1, 2, 3), runner)
    assert [(p.number, p.harness) for p in got] == [(1, "codex"), (2, "unknown"), (3, "claude_code")]


@pytest.mark.parametrize("failure", [
    OSError("gh missing"),
    subprocess.TimeoutExpired("gh", 120),
    github.GitHubReadError("gh pr view exited 1: rate limit"),
    "not json",
    "[]",
    json.dumps({}),
    json.dumps({"headRefName": None}),
])
def test_attribute_lookup_failure_modes_become_unknown_and_the_rest_continue(failure):
    def runner(argv):
        if int(argv[3]) == 1:
            if isinstance(failure, Exception):
                raise failure
            return failure
        return json.dumps({"headRefName": "codex/ok"})

    got = github.attribute(prs_for(1, 2), runner)
    assert [(p.number, p.harness) for p in got] == [(1, "unknown"), (2, "codex")]
    assert (got.lookups, got.failed_lookups) == (2, 1)


@pytest.mark.parametrize("behavior, expected, match", [
    (subprocess.CalledProcessError(1, ["gh"]), subprocess.CalledProcessError, None),
    (OSError("gh missing"), OSError, None),
    (subprocess.TimeoutExpired("gh", 120), subprocess.TimeoutExpired, None),
    ("", github.GitHubReadError, "printed nothing"),
    ("  \n", github.GitHubReadError, "printed nothing"),
    ("{}", github.GitHubReadError, "not a list"),
    ("not json", github.GitHubReadError, "did not print JSON"),
])
def test_a_failed_search_is_loud_never_a_quiet_day(behavior, expected, match):
    def runner(argv):
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    with pytest.raises(expected, match=match):
        github.merged_prs(DAY, runner, owner="demo-owner")


def test_an_empty_json_list_is_a_genuine_empty_day():
    assert github.merged_prs(DAY, lambda argv: "[]", owner="demo-owner") == []


class FakeRun:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.kwargs = result, error, None

    def __call__(self, argv, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.result


def test_run_cmd_reads_utf8_leniently_and_never_waits_on_stdin(monkeypatch):
    fake = FakeRun(subprocess.CompletedProcess(["gh"], 0, stdout="[]", stderr=""))
    monkeypatch.setattr(github.subprocess, "run", fake)
    assert github.run_cmd(["gh", "x"]) == "[]"
    assert fake.kwargs["encoding"] == "utf-8" and fake.kwargs["errors"] == "replace"
    assert fake.kwargs["stdin"] is subprocess.DEVNULL
    assert fake.kwargs["check"] is True and fake.kwargs["timeout"] == 120


def test_run_cmd_failure_carries_the_stderr_tail(monkeypatch):
    err = subprocess.CalledProcessError(1, ["gh", "search", "prs"], stderr="HEADLINE" + "x" * 300 + "API rate limit exceeded\n")
    monkeypatch.setattr(github.subprocess, "run", FakeRun(error=err))
    with pytest.raises(github.GitHubReadError) as exc:
        github.run_cmd(["gh", "search", "prs"])
    msg = str(exc.value)
    assert "API rate limit exceeded" in msg and "HEADLINE" not in msg
    assert exc.value.__cause__ is err


def test_run_cmd_failure_without_stderr_still_raises_the_typed_error(monkeypatch):
    err = subprocess.CalledProcessError(4, ["gh", "search", "prs"], stderr=None)
    monkeypatch.setattr(github.subprocess, "run", FakeRun(error=err))
    with pytest.raises(github.GitHubReadError) as exc:
        github.run_cmd(["gh", "search", "prs"])
    assert "4" in str(exc.value)


def test_attribute_counts_lookups_and_failures_so_a_mass_failure_is_visible():
    def runner(argv):
        n = int(argv[3])
        if n == 2:
            raise subprocess.CalledProcessError(1, argv)
        return json.dumps({"headRefName": {1: "codex/a", 3: "sam/hotfix"}[n]})

    got = github.attribute(prs_for(1, 2, 3), runner)
    assert got == [dataclasses.replace(p, harness=h) for p, h in zip(prs_for(1, 2, 3), ["codex", "unknown", "unknown"])]
    assert (got.lookups, got.failed_lookups) == (3, 1)  # "sam/hotfix" is unknown but NOT a failure

    def down(argv):
        raise github.GitHubReadError("gh pr view exited 1: API rate limit exceeded")

    got = github.attribute(prs_for(1, 2), down)
    assert [p.harness for p in got] == ["unknown", "unknown"]
    assert (got.lookups, got.failed_lookups) == (2, 2)


def test_attribute_counts_only_the_prs_it_looked_up():
    got = github.attribute(prs_for(1, 2), lambda argv: json.dumps({"headRefName": "codex/a"}),
                           want=lambda p: p.number == 1)
    assert (got.lookups, got.failed_lookups) == (1, 0)


def test_attribute_counts_a_missing_head_ref_as_a_failed_lookup():
    got = github.attribute(prs_for(1), lambda argv: json.dumps({}))
    assert [p.harness for p in got] == ["unknown"] and got.failed_lookups == 1



@pytest.mark.parametrize("closed", [None, "", "yesterday", "2026-10-16T21:00:00", 7.5e20, True],
                         ids=["missing", "empty", "words", "no_zone", "overflow", "bool"])
def test_a_row_with_an_unreadable_merge_time_is_dropped_and_counted(closed):
    rows = [pr(1, "2026-10-16T21:00:00Z"), pr(2, closed), pr(3, "2026-10-16T05:00:00Z")]  # 3: another day
    got = github.merged_prs(DAY, FakeGh(rows), owner="demo-owner")
    assert [p.number for p in got] == [1] and got.bad_time == 1  # another day is no bad time


def test_a_clean_search_has_no_bad_time():  # positive control
    got = github.merged_prs(DAY, FakeGh([pr(1, "2026-10-16T21:00:00Z")]), owner="demo-owner")
    assert got.bad_time == 0 and [p.number for p in got] == [1]


@pytest.mark.parametrize("owner", ["", "  ", "--json", None])
def test_a_bad_owner_never_reaches_gh(owner):
    calls = []
    with pytest.raises(github.GitHubReadError, match="^bad_owner$"):
        github.merged_prs(DAY, lambda argv: calls.append(argv) or "[]", owner=owner)
    assert calls == []


def test_harness_prefixes_are_configurable():
    mine = (("bot/", "codex"), ("ai/", "claude_code"))
    assert github.harness_from_ref("bot/fix", mine) == "codex" and github.harness_from_ref("ai/x", mine) == "claude_code"
    assert github.harness_from_ref("codex/fix", mine) == "unknown"  # the defaults are replaced, not added to
    gh = FakeGh([pr(1, "2026-10-16T21:00:00Z")], heads={("owner/demo-tool", 1): "bot/x"})
    got = github.attribute(github.merged_prs(DAY, gh, owner="demo-owner"), gh, prefixes=mine)
    assert got[0].harness == "codex"
