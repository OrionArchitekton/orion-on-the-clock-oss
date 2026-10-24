# Configuration

Your configuration and the pipeline's state live outside this repository, always:

| What | Default | Override |
| --- | --- | --- |
| Config folder | `~/.config/orion-on-the-clock/` | `ORION_CLOCK_CONFIG` |
| State folder | `~/.local/state/orion-on-the-clock/` | `ORION_CLOCK_STATE` |

Start from the commented examples: `cp config.example/* ~/.config/orion-on-the-clock/`, then edit every file. The
examples load as they are (a test checks that), which also means nothing stops a run that still carries the example
values: replace them before your first run.

Every file is read strictly. A missing file, an unknown key, a value of the wrong type or a required key left out stops
the run with a short code that names the key (`missing:show.series`, `unknown:writer.modle`, `bad:timezone`), never
the value. Nothing falls back to a default that publishes.

## settings.toml

The pluggable pieces. Loaded first, before the day is chosen, because the day is a day in your time zone; a bad
settings file prints one line to stderr and exits 1 before any stage runs.

| Key | Required | Meaning |
| --- | --- | --- |
| `timezone` | yes | IANA zone name. Every source is cut to the calendar day in this zone. |
| `show.series` | yes | The series name, in every description. |
| `show.host_name` | yes | Who narrates the host lines, in the first person. |
| `show.disclosure` | yes | The last line of every description (say so if the voice is synthetic). |
| `sources.claude_code`, `sources.codex`, `sources.github` | no (all on) | Switch a built-in source off. At least one agent source stays on. |
| `sources.claude_projects`, `sources.codex_db` | no | Where those logs live, when not in the default place. Absolute paths (or `~`). |
| `sources.tracker` | no | Your tracker adapter file (docs/sources.md). |
| `[[sources.adapters]]` `harness`, `display`, `module` | no | Your counts-only agent adapters (docs/sources.md). |
| `github.owner` | while GitHub is on | The user or organization whose merged PRs count. |
| `github.harness_prefixes` | no | Branch prefix to agent id; only `claude_code` and `codex` may be credited. |
| `checks.public_hosts` | no | Hosts a shipped line may name in a URL (default: YouTube only). |
| `checks.redact_patterns` | no | Your own private words as regular expressions (`name`, `regex`); a hit stops the line. |
| `checks.dir` | no | A folder with your own `redactor.py`, `slop_gate.py` and `secret_scan.py` in place of `daily/checks/`. |
| `writer.model`, `writer.budget_usd` | no | The headless Claude Code call (default `sonnet`, 2 USD cap per call). |
| `writer.voice_style`, `writer.voice_samples`, `writer.voice_tags`, `writer.avoid_words` | no | How the host talks; see below. |
| `channel.id`, `channel.name`, `channel.home_id` | for publishing | The YouTube channel to upload to, and an optional channel to switch back to. |
| `browser.endpoint` | no | The Chrome DevTools endpoint the publish stage drives (default `http://127.0.0.1:9222`). |
| `notify.command` | no | A command that receives each run's outcome; empty writes it to the state folder and prints it. |
| `tts.host` | no | An ssh destination that runs the text to speech; empty runs it here. |

The `channel`, `browser`, `notify` and `tts` keys are read by stages that are not ported yet; they are validated now so
a typo fails today rather than on the first upload. A stage that needs a key you left out (the channel id, say) stops
with `missing:<key>` instead of choosing one.

### The host's voice

The writer is told how the host talks. With no `writer.*` voice keys it uses built-in notes that read as a person
talking: connectors such as "So" and "And", long sentences chained with and, so and but, a person (I, you, we) in
every line that can take one, and the so-what in plain words after every technical point.

- `voice_style`: your own notes, one per line, in place of the built-in ones.
- `voice_samples`: a few lines you really said, shown as a style reference the writer must never copy.
- `voice_tags`: short tags the host may close one beat with, at most once an episode.
- `avoid_words`: words the host never says. The draft review flags them and spends its one repair on a draft that
  uses one. The built-in list is in `daily/voicelint.py` (`WRITERLY`).

## lanes.json

Which repos may appear, and how. See the comments in `config.example/lanes.json`.

- `lanes`: repo name to `{"disclosure": "public" | "headline", "label", "why"}`. A public lane's merged PRs, tasks and
  agent quotes may be the story, named by its label. A headline lane is private work: only its label, its why and a
  count reach the writer, under an opaque id.
- `hard_exclude`: repos or folder names that never appear in any text and are left out of the merged PR and closed
  task counts. Matched as substrings of paths (3 characters minimum): a session that ever touched one gives no quotes
  and no notes, though it still counts as a session.
- `blocked_names`: words that must never ship (machine names, client names, code names), matched with separators
  ignored and from a word start.

A repo that is not listed never appears in any text; it still counts toward the day's PR total.

## embargo.json

`{"active": true | false, "terms": [...]}`. While active, a term blocks any text that holds it, from a word start,
ignoring case and separators. An active embargo needs at least one term.

## publish.json

`{"visibility": "private"}`. Read by the publish stage (not ported yet). Private is the default and the only safe
place to start: you watch each upload before you make it public yourself.

## The state folder

One folder per recapped day (`2026-10-16/`), and a sibling for dry runs (`2026-10-16-dry/`) that a dry run never
leaves. Inside a day's folder: `status.json` (one entry per stage), `run.lock`, `ledger.json`, `sources/github.jsonl`
(the PR bodies quotes point at), `writer-prompt.txt` and `writer-run.json` (what the writer was sent and what it cost),
`script.json` and `verdict.json` (the gate's checks, each with short evidence). `last-run.json` at the top records the
day the last real run chose.
