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
| `show.handle` | no | Your channel handle (`@YourChannel`), shown on the closing card; leave out to show none. |
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
| `channel.id` | for a real publish | The YouTube channel id (`UC` and 22 characters) every upload goes to and the driver proves before it acts. |
| `channel.name` | no | How messages name that channel (default: the id). |
| `channel.home_id` | no | A channel to put the browser back on after an upload (same Google account, several channels); leave out to skip. |
| `browser.endpoint` | no | The Chrome DevTools endpoint the Studio driver connects to, loopback only (default `http://127.0.0.1:9222`). |
| `browser.lease`, `browser.release` | no (both or neither) | Commands that lease the browser from a pool and give it back (docs/runbook.md). |
| `notify.command` | no | A command that receives each run's outcome as JSON on stdin (docs/runbook.md); empty writes it to the state folder and prints it. |
| `tts.python` | for a real run voiced here | The Python of the environment Chatterbox is installed in. |
| `tts.lock` | no | The lock file that keeps one Chatterbox at a time on this machine (default `~/.local/state/orion-on-the-clock/tts.lock`). |
| `tts.target` | no | `auto` (default: the remote host when set, falling back here once), `local` or `remote`. |
| `tts.host` | no | An ssh destination (alias, host or user@host) that voices on its GPU; empty voices here. |
| `tts.remote_python`, `tts.remote_dir` | with `tts.host` | The Chatterbox Python and a scratch folder on that host (absolute, plain characters only). |
| `tts.remote_gpu_allow` | no | GPU process names that may share the remote GPU (default none: any other process means busy). |
| `whisper.python` | for a real run | The Python of the environment faster-whisper is installed in. |
| `whisper.model` | no | The Whisper model (default `small.en`). |
| `render.kit` | for render, dry runs too | Your media kit folder (docs/media-kit.md). |
| `render.hyperframes` | no | The HyperFrames command as a list (default `["hyperframes"]`). |
| `render.sandbox` | no | A command put in front of HyperFrames; `{timeout}` becomes the step's timeout in seconds (docs/how-it-works.md). |

A stage that needs a key you left out stops with `missing:<key>` instead of choosing one, naming the key as
settings.toml spells it (`missing:tts.python`, `missing:render.kit`, `missing:channel.id`).

### The host's voice

The writer is told how the host talks. With no `writer.*` voice keys it uses built-in notes that read as a person
talking: connectors such as "So" and "And", long sentences chained with and, so and but, a person (I, you, we) in
every line that can take one, and the so-what in plain words after every technical point.

- `voice_style`: your own notes, one per line, in place of the built-in ones.
- `voice_samples`: a few lines you really said, shown as a style reference the writer must never copy.
- `voice_tags`: short tags the host may close one beat with, at most once an episode.
- `avoid_words`: words the host never says. The draft review flags them and spends its one repair on a draft that
  uses one. The built-in list is in `daily/voicelint.py` (`WRITERLY`).

## voices.json

Who speaks each line and how; see the comments in `config.example/voices.json` and the voice section of
docs/media-kit.md.

- `host`: the narrator. `ref` is your reference recording (a path on the machine that voices; `~` expands there) and
  `sha256_prefix` the start of its sha256, checked before every real voicing. `params` and `params_cold_open` are
  Chatterbox's `exaggeration`, `cfg_weight` and `temperature`; `post` an optional ffmpeg chain.
- One entry per agent id that can appear in a day's ledger (`claude_code`, `codex`, each adapter's `harness`):
  `display`, `glyph` (up to 4 characters) and `color` (`#RRGGBB`) label it on screen; `active: false` keeps its quotes
  off the air; `params`; and `post`, an ffmpeg audio chain that must start with `asetrate=24000*` and may use only the
  allowed audio filters. An agent never has a reference recording.
- Keys starting with an underscore are notes. Anything else unknown, a missing key or a wrong type stops the voice
  stage with `voice_config:<code>` naming the key.

The file's sha256 is recorded when a day is voiced. Editing it afterwards makes that day's render refuse
(`voice_config_changed`) until the day is voiced again.

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

Any other key, at the top level or inside a lane entry, stops the run: a misspelled `hard_excludes` next to an empty
`hard_exclude` must never load as no exclusion. Keys starting with an underscore are notes and are allowed anywhere.
`status.json` records `config:unknown_key`; the error itself names the key and its level, never a value
(`unknown_key:lanes.json:hard_excludes`, `unknown_key:lanes.json:lane.Label`). To see it:
`python3 -c "from daily import config, paths; config.load_config(paths.config_dir())"` from the repository root.

## embargo.json

`{"active": true | false, "terms": [...]}`. While active, a term blocks any text that holds it, from a word start,
ignoring case and separators. An active embargo needs at least one term. Any other key stops the run
(`unknown_key:embargo.json:<key>`), so a misspelled `actve` never leaves the embargo off; keys starting with an
underscore are notes.

## publish.json

`{"visibility": "private"}`, your off switch for public mode. Read once per run by the publish stage, never by the
Studio driver (the stage passes the mode on the command line). Only a JSON object whose `visibility` is exactly
`"public"` turns public mode on (keys starting with an underscore are notes); a missing, unreadable or malformed file,
`"Public"`, `"unlisted"` or anything else means private, never an error. Private is the default and the place to start:
you watch each upload in Studio before you make it public yourself. A day already uploaded keeps its visibility when
you flip the switch, and a half-finished upload keeps the one it began with (docs/runbook.md, "Public mode").

## The state folder

One folder per recapped day (`2026-10-16/`), and a sibling for dry runs (`2026-10-16-dry/`) that a dry run never
leaves. Inside a day's folder: `status.json` (one entry per stage), `run.lock`, `ledger.json`, `sources/github.jsonl`
(the PR bodies quotes point at), `writer-prompt.txt` and `writer-run.json` (what the writer was sent and what it cost),
`script.json` and `verdict.json` (the gate's checks, each with short evidence), then the media: `hostlint.json`,
`voice/` (the takes, word times, the voice manifest and the TTS and Whisper logs), `build/` (the composition, the mix
and the HyperFrames logs), `episode.mp4`, `render.json` and `contact-sheet.jpg`, the rejected copies of a failed
render, and `previous/` (earlier passing renders); then the release: `upload-episode.mp4` and `upload-item.json` (what
the driver uploads), `upload.json` (its receipt), `upload.lock`, `publish.log`, and `notify.json` with `notify.lock`.

At the top of the state folder: `last-run.json` (the day the last real run chose), `uploads/ledger.jsonl` (every
upload ever started and verified; never edit or delete it: it is what stops a second video for a day whose receipt is
lost), `notify/` (the default notify sink's items), `_studio-stops/` (private screenshots of a stop at the channel
check, newest 20 kept) and `studio.lock` (one driver command at a time). The driver's agent-browser sessions live in
`$XDG_RUNTIME_DIR/orion-on-the-clock-browser/` (or `browser/` here without a runtime folder). docs/how-it-works.md
describes each file.
