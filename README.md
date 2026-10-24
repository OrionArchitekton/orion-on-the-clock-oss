# orion-on-the-clock

An automated daily YouTube Short that recaps what your AI coding agents did yesterday: what shipped, one story told in
plain words, one thing a viewer can steal for their own work, and one line an agent really wrote, read in its own
voice. Every morning a timer runs the pipeline; nothing reaches the screen unless a fail-closed gate can prove it.

It started as "Dan's Agents on the Clock", a show recapping the work of one developer's Claude Code and Codex agents,
and this repository is a sanitized copy of that working pipeline, published so you can learn from it and run your
own.

## Status

Release 0.1.0 carries the text half: collect, write and gate run end to end, and a dry run produces a gated script
for any day. The media half (voice, render) and the release half (publish, notify) arrive in later releases. Until
then they are registered placeholders that fail closed, so a run that reaches one stops there instead of skipping it.

## How it works

```
agent logs + merged PRs
        |
     collect -> write -> gate -> voice -> render -> publish -> notify
        |          |        |
     ledger.json  script   verdict.json
```

1. **collect** reads one calendar day of Claude Code and Codex session logs, the PRs merged on GitHub, and any sources
   you plug in, then writes the ledger: the allowed items, quote candidates and counts, and nothing else.
2. **write** sends the ledger to one headless `claude -p` call (tools off, settings and MCP servers off) and gets back
   a structured script: cold open, scoreboard, story, steal this, an optional agent confessional, and the out.
3. **gate** runs fourteen checks on the script and writes a verdict; every later stage reads the script only through
   that verdict.
4. **voice**, **render**, **publish**, **notify**: text to speech, the 9:16 video, a private upload, and a report of
   every run's outcome (later releases).

`python3 -m daily.run` runs the stages in order, records each in `status.json`, resumes where a run stopped, and holds a
kernel lock so two runs never overlap. `docs/configuration.md` describes every file it reads and writes.

## What you need

- Linux or macOS with Python 3.11 or newer (the text half is standard library only)
- [Claude Code](https://claude.com/claude-code) signed in: the writer is one headless call on your own
  login, capped by `writer.budget_usd`
- `git`, and the [GitHub CLI](https://cli.github.com/) signed in if you count merged PRs (`sources.github`)
- Agent logs to recap: Claude Code and Codex are built in, anything else plugs in (docs/sources.md)

## Quickstart: a dry run

```bash
git clone <this repository> && cd orion-on-the-clock-oss
python3 -m pip install -e '.[dev]'      # or run from the checkout without installing
python3 -m pytest -q                    # fakes only, no network

mkdir -p ~/.config/orion-on-the-clock
cp config.example/* ~/.config/orion-on-the-clock/
# edit settings.toml (time zone, show names, your GitHub owner) and lanes.json (which repos may appear)

python3 -m daily.run --day yesterday --until gate --dry
```

A dry run works in its own folder, `~/.local/state/orion-on-the-clock/<day>-dry/`, and never touches the real day's
folder. Read `ledger.json` (what the writer was allowed to see), `script.json` (what it wrote) and `verdict.json` (each
gate check with its evidence). Exit code 0 means the script passed the gate, or that the day was quiet;
`status.json` says which.

## Safety design, in brief

- **The ledger is the only thing the writer sees.** Repos are allowlisted by lane: a public lane may be the story, a
  headline lane shows only as a label and a count, anything unlisted never appears in text, and a hard-excluded repo
  is left out even of the merged PR and closed task counts. A session that ever touched an excluded folder gives no
  quotes and no notes.
- **Untrusted text stays data.** Agent logs and PR bodies are fenced inside the prompt so no ledger text can close the
  fence or pass as an instruction, and the writer has no tools.
- **The gate fails closed.** Fourteen checks (shape, story and quote in the ledger, the quote re-read word for word
  from its source line, redactor, AI-tell phrases and long dashes, secret shapes, blocked names, embargo, word budget,
  no code, no invented numbers) must each pass on positive evidence. A check that crashes fails. Every text check reads the
  raw text and a normalized form, so invisible characters, look-alike letters and look-alike dashes cannot hide a hit.
- **Receipts bind everything.** The verdict carries the hashes of the script and the ledger it judged; a consumer that
  finds either changed refuses it.
- **Private by default.** Uploads are private unless your config says otherwise (later releases), and a dry run can
  never create, change or delete a file of the real day.
- **Strict config.** A typo in a settings key stops the run; nothing falls back to a default that publishes.

`SECURITY.md` lists what is and is not defended.

## Repository layout

- `daily/`: the pipeline package, one module per stage; `daily/sources/` the log readers; `daily/checks/` the built-in
  redactor, slop gate and secret scanner; `daily/tests/` the suite
- `config.example/`: commented settings, lanes, embargo and publish files to copy into your config folder
- `docs/`: configuration, sources and the adapter contracts (with worked examples), the writer's CLI notes

## Credits

Built by Dan Mercede's agents (Claude Code and Codex), from the pipeline behind "Dan's Agents on the Clock". MIT
licensed; see `LICENSE`.
