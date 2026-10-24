# orion-on-the-clock

An automated daily YouTube Short that recaps what your AI coding agents did yesterday: what shipped, one story told in
plain words, one thing a viewer can steal for their own work, and one line an agent really wrote, read in its own
voice. Every morning a timer runs the pipeline; nothing reaches the screen unless a fail-closed gate can prove it.

It started as "Dan's Agents on the Clock", a show recapping the work of one developer's Claude Code and Codex agents,
and this repository is a sanitized copy of that working pipeline, published so you can learn from it and run your
own.

## Status

Release 0.1.0 carries the text half and the media half: collect, write, gate, voice and render run end to end, and a
dry run produces a gated script and a silent-voiced 9:16 rehearsal video for any day. The release half (publish,
notify) arrives in a later release. Until then its two stages are registered placeholders that fail closed, so a run
that reaches one stops there instead of skipping it.

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
4. **voice** has your cloned voice (Chatterbox, from a reference recording you supply) say the host lines and the
   agent's built-in voice say its quote, then times every word with faster-whisper.
5. **render** lays out the timeline, fills a HyperFrames composition (scoreboard, story cards, the confessional card,
   karaoke captions), mixes the sound to -14 LUFS, renders the 9:16 video, and writes a verdict of five checks.
6. **publish**, **notify**: a private upload and a report of every run's outcome (a later release).

`python3 -m daily.run` runs the stages in order, records each in `status.json`, resumes where a run stopped, and holds a
kernel lock so two runs never overlap. `docs/how-it-works.md` walks through the stages and every file they write;
`docs/configuration.md` describes every file they read.

## What you need

- Linux or macOS with Python 3.11 or newer (the text half is standard library only)
- [Claude Code](https://claude.com/claude-code) signed in: the writer is one headless call on your own
  login, capped by `writer.budget_usd`
- `git`, and the [GitHub CLI](https://cli.github.com/) signed in if you count merged PRs (`sources.github`)
- Agent logs to recap: Claude Code and Codex are built in, anything else plugs in (docs/sources.md)

For the media half (versions are the ones this was built and tested with):

- ffmpeg and ffprobe 6.1 on your PATH, and numpy and Pillow in the pipeline's Python (`pip install -e '.[media]'`)
- [Node.js](https://nodejs.org/) 22 or newer (built with 24) and [HyperFrames](https://github.com/heygen-com/hyperframes)
  0.8.36 (`npm install hyperframes@0.8.36`, Apache-2.0), which renders the composition in a headless Chromium
- [Chatterbox](https://github.com/resemble-ai/chatterbox) 0.1.7 (`pip install chatterbox-tts==0.1.7`, with torch 2.7)
  in its own Python environment, on this machine or on a GPU box you can ssh to; a GPU with 4.5 GiB free is much
  faster, and the CPU works
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) 1.2.1 (`pip install faster-whisper==1.2.1`) in its own
  Python environment, with the `small.en` model on CPU
- Your media kit (fonts, music, sound cues, GSAP) and your voice reference recording: nothing is bundled.
  `docs/media-kit.md` lists every file, free-licensed sources and what each license asks.

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

Then the media half, still dry (silence stands in for the voice, so no TTS or Whisper is needed): set `render.kit`
in settings.toml, copy `config.example/voices.json` too, and

```bash
python3 -m daily.run --day yesterday --from-stage voice --until render --dry
```

Open `contact-sheet-dry.jpg` and `episode-dry.mp4` in the dry folder.

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
  finds either changed refuses it. Voice and render read the script only through it, so text edited after the gate is
  never voiced or rendered, and the render verdict binds the episode to that text and to the voices it was made with.
- **Voices are consented and checked.** The host's cloned voice comes only from the reference recording you name, and
  its hash is checked before every voicing. An agent never speaks in a person's voice: agent voices are the TTS
  model's built-in voice plus an allowlisted ffmpeg chain, and the runner proves no agent line used the host's
  conditioning.
- **Private by default.** Uploads are private unless your config says otherwise (later releases), and a dry run can
  never create, change or delete a file of the real day.
- **Strict config.** A typo in a settings key stops the run; nothing falls back to a default that publishes.

`SECURITY.md` lists what is and is not defended.

## Repository layout

- `daily/`: the pipeline package, one module per stage; `daily/sources/` the log readers; `daily/checks/` the built-in
  redactor, slop gate and secret scanner; `daily/engine/` the HyperFrames composition template; `daily/tests/` the
  suite
- `config.example/`: commented settings, voices, lanes, embargo and publish files to copy into your config folder
- `docs/`: how it works stage by stage, configuration, the media kit and voice, sources and the adapter contracts
  (with worked examples), the writer's CLI notes

## Credits

Built by Dan Mercede's agents (Claude Code and Codex), from the pipeline behind "Dan's Agents on the Clock". MIT
licensed; see `LICENSE`.
