"""PRs merged on the local day under one GitHub owner (settings.toml `github.owner`), from `gh`. The runner is
injectable so tests never call GitHub. `gh` must already be signed in; it is called with an argv list, no shell."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from dataclasses import dataclass, replace
from typing import Callable

from daily.day import gh_range, in_day, to_utc
from daily.settings import DEFAULT_PREFIXES

Runner = Callable[[list], str]

SEARCH_LIMIT = 1000  # gh search's maximum


STDERR_TAIL = 200


class SearchTruncated(Exception):
    """The search hit its cap, so the day may be missing PRs."""


class GitHubReadError(Exception):
    """gh failed or answered with something that is not a PR list; never read as a quiet day."""


def run_cmd(argv: list) -> str:
    try:
        return subprocess.run(argv, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, timeout=120).stdout
    except subprocess.CalledProcessError as exc:
        # gh's stderr carries no tokens; its tail tells an auth failure from a rate limit
        tail = (exc.stderr or "").strip()[-STDERR_TAIL:]
        raise GitHubReadError(f"{' '.join(argv[:3])} exited {exc.returncode}: {tail}") from exc


def harness_from_ref(ref: str | None, prefixes=DEFAULT_PREFIXES) -> str:
    """The agent behind a branch (or a tracker actor), by name prefix: settings.toml `github.harness_prefixes`, by
    default claude/... and codex/.... The first matching prefix wins; anything else is "unknown"."""
    ref = ref or ""
    for prefix, harness in prefixes:
        if ref.startswith(prefix):
            return harness
    return "unknown"


@dataclass(frozen=True)
class PR:
    repo: str
    number: int
    title: str
    body: str
    url: str
    closed_at: str
    harness: str = "unknown"


class MergedPRs(list):
    """The day's merged PRs, plus `bad_time`: how many search rows were dropped because their merge time (closedAt)
    is missing or cannot be read, so such a row is counted instead of silently read as another day's PR."""

    bad_time: int = 0


def merged_prs(day: dt.date, runner: Runner = run_cmd, *, owner: str) -> MergedPRs:
    if not isinstance(owner, str) or not owner.strip() or owner.startswith("-"):
        raise GitHubReadError("bad_owner")
    out = runner(["gh", "search", "prs", "--owner", owner, "--merged-at", gh_range(day),
                  "--json", "repository,number,title,body,url,closedAt", "--limit", str(SEARCH_LIMIT)])
    if not out.strip():
        raise GitHubReadError("gh search printed nothing; an empty day is the JSON list []")
    try:
        rows = json.loads(out)
    except ValueError as exc:
        raise GitHubReadError("gh search did not print JSON") from exc
    if not isinstance(rows, list):
        raise GitHubReadError(f"gh search printed {type(rows).__name__}, not a list")
    if len(rows) >= SEARCH_LIMIT:
        raise SearchTruncated(f"gh search returned {len(rows)} PRs, its cap; the day may be incomplete")
    prs = MergedPRs()
    for r in rows:
        if to_utc(r.get("closedAt")) is None:
            prs.bad_time += 1
            continue
        if not in_day(r.get("closedAt"), day):
            continue
        prs.append(PR(repo=r["repository"]["nameWithOwner"], number=int(r["number"]), title=r.get("title") or "",
                      body=r.get("body") or "", url=r.get("url") or "", closed_at=r["closedAt"]))
    return prs


class AttributedPRs(list):
    """The attributed PRs, plus how many lookups ran and how many failed (a rate limit shows as all of them)."""

    lookups: int = 0
    failed_lookups: int = 0


def attribute(prs: list, runner: Runner = run_cmd, want: Callable = lambda pr: True,
              prefixes=DEFAULT_PREFIXES) -> AttributedPRs:
    out = AttributedPRs()
    for pr in prs:
        if want(pr):
            ref = _head_ref(pr, runner)
            out.lookups += 1
            out.failed_lookups += ref is None
            pr = replace(pr, harness=harness_from_ref(ref, prefixes))
        out.append(pr)
    return out


def _head_ref(pr: PR, runner: Runner) -> str | None:
    """Attribution never decides privacy, so any failed lookup leaves the PR unattributed."""
    try:
        raw = runner(["gh", "pr", "view", str(pr.number), "--repo", pr.repo, "--json", "headRefName"])
        ref = json.loads(raw).get("headRefName")
    except (GitHubReadError, subprocess.SubprocessError, OSError, ValueError, AttributeError):
        return None
    return ref if isinstance(ref, str) else None
