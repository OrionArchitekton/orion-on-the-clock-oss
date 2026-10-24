# Sources: what the show reads, and how to plug in your own

Every episode starts from one day of logs. The collect stage reads each source for one calendar day in your time zone
(settings.toml `timezone`), keeps only what your lanes allow, and writes the ledger: the only thing the writer ever
sees.

A source that cannot be read is never a quiet day. A transcript reader or an adapter that fails drops its agent out of
that day with `coverage: unavailable:<code>`, and the writer is told the agent was unread, not idle. GitHub and the
tracker are the main story, so their failures stop the collect stage outright.

## Built in

| Source | Settings switch | What it reads | What it gives |
| --- | --- | --- | --- |
| Claude Code | `sources.claude_code` | `~/.claude/projects/*/<session>.jsonl` and subagent files (`sources.claude_projects`) | session and subagent counts, session notes, quote candidates |
| Codex | `sources.codex` | `~/.codex/state_5.sqlite` (read only) and the rollout files it names (`sources.codex_db`) | session and subagent counts, session notes, quote candidates |
| GitHub | `sources.github` | PRs merged that day under `github.owner`, through `gh search prs` and `gh pr view` | merged PR items, PR body quotes |

Quotes come only from the two transcript readers and from PR bodies, because those are the only texts the gate can
open again and prove word for word (`quote_verbatim`). Each quote keeps the file and line it came from; the gate
re-reads that line before anything ships.

A PR is credited to an agent by its branch name prefix (`github.harness_prefixes`, by default `claude/` and `codex/`).
An uncredited PR still counts and can still be the story; it just gives no quote.

### What the transcript readers refuse

- A session that touched a `hard_exclude` entry, on any day, in its own records or in any helper session it spawned,
  gives no quotes and no note. Most sessions run from the home folder, so the readers look at every tool call input,
  every attached file and every path in a tool's output, not just the working directory.
- A sentence is a candidate only when it is 6 to 30 words, a complete sentence, with no code, path, URL, host name,
  address or invisible character. The ledger then applies the lanes, the blocked names, the embargo and the text
  checks before the writer can see it.

## Your own agents: counts-only adapters

Any other agent can appear on the scoreboard and in the narration through an adapter: one Python file of yours that
exposes one function.

```python
def scan(day, start, end) -> dict:
    """day: the local date being recapped (datetime.date).
    start, end: aware UTC datetimes; the day is [start, end).
    Return {"sessions": n, "subagents": m}: two integers, zero or more.
    Raise on any failure to read your logs. Never return zeros for logs you could not read."""
```

Register it in settings.toml:

```toml
[[sources.adapters]]
harness = "my_agent"          # the agent's id in the ledger: lower case letters, digits, underscores
display = "My Agent"          # how the narration and the screen name it
module = "~/.config/orion-on-the-clock/adapters/my_agent.py"
```

The file is loaded by its path under a private module name, so nothing on `sys.path` can stand in for it, and a file
linked from outside its own folder is refused. An exception becomes the coverage reason: a short lower-case code such
as `my_agent_logs_missing` is kept as written, and any other message is reduced to the exception's class name, because a
free-text message can spell a private path.

Adapters are counts only, by design. Their agents give no quotes and no session notes: the gate could not verify a
quote it cannot re-read from a known log format, and an unverified quote is exactly what this pipeline exists to keep
off screen. If you want quotes from another agent, port it as a transcript reader with a matching re-reader in
`daily/gate.py`, and test it the way `daily/tests/test_claude.py` and `test_gate.py` test the built-in ones.

Worked example: [docs/examples/counts_adapter.py](examples/counts_adapter.py) counts sessions and spawned helpers from
an imaginary agent's JSONL logs. `daily/tests/test_adapters.py` runs it against synthetic logs, so it stays in step
with the contract.

## Your tracker: closed tasks and work in progress

A tracker adapter adds tasks ("arcs") beside merged PRs: a task closed that day can be the episode's story, and an open
task that moved gives the episode's out its teaser. It is one Python file with three functions:

```python
def newest(day, start, end) -> str | None:
    """The time of the newest event your tracker holds at all (ISO with a zone), or None when it holds nothing.
    This is the proof that its record reaches the day."""

def closed(day, start, end) -> list:
    """Tasks closed inside [start, end). Each row:
    {"id": str, "title": str, "repo": str or None, "harness": str, "ts": ISO time with a zone}"""

def active(day, start, end) -> list:
    """Tasks still open whose latest activity is inside [start, end). Each row:
    {"id": str, "title": str, "repo": str or None, "harness": str, "last_ts": ISO time with a zone}"""
```

```toml
[sources]
tracker = "~/.config/orion-on-the-clock/adapters/my_tracker.py"
```

What the collect stage does with your answers:

- When `newest` is None or earlier than the day's start, the tracker's record stops before the day and says nothing
  about it: the day's arcs are left unknown (the scoreboard gets `null`, diagnostics say `stale`), and `closed` and
  `active` are not called. A busy tracker covers every day; an idle one simply leaves its count unknown, never zero.
- A row that does not fit (not an object, a time that is not an ISO string with a zone, a time outside the day, an id
  that is empty, padded, longer than 200 characters or holds a path separator) is dropped and counted in the ledger's
  diagnostics.
- A task closed twice in the day counts once, at its latest close.
- Titles are sanitized (control and invisible characters dropped, whitespace collapsed, capped) and then checked like
  every other text bound for the writer.
- `repo` decides the lane: a public lane's task can be the story by title; a headline lane's task shows only in its
  lane's count; an unlisted repo's task shows only in the day's total; a hard-excluded repo's task is not even counted.
- `harness` must be one of your configured agent ids, or it becomes `unknown`.
- An ID is never shown: the ledger replaces it with `arc:` and the first 8 hex of its sha256, because a free-text id
  can spell a private name.
- An exception, a reply that is not a list, or a `newest` that is not an ISO time stops the collect stage
  (`TrackerReadError`): an unreadable tracker must never look like a day on which nothing closed.

Without a tracker the day has no arcs, and the scoreboard records the closed-task count as unknown (`null`), never
zero.

Worked example: [docs/examples/tracker_jsonl.py](examples/tracker_jsonl.py) reads a JSONL event log (`opened`,
`moved`, `closed`), answers `newest` from its last line, and credits agents by an actor prefix. `daily/tests/test_adapters.py` runs it end to end through
the collect stage and the ledger.

## Adding a source to this repository instead

If an agent's logs are common enough to ship (another coding agent CLI, say), add a module under `daily/sources/` that
returns a `HarnessDay` (or a subclass with its own counters), wire it into `daily/collect.py` behind a settings switch,
and keep the same rules: synthetic fixtures only, a read failure raises, and nothing in a test reads a real log.
