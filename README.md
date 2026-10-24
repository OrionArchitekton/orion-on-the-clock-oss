# orion-on-the-clock

An automated daily YouTube Short that recaps what your AI coding agents did yesterday: what shipped, one story told in
plain words, one thing a viewer can steal for their own work, and one line an agent really wrote, read in its own
voice. Every morning a timer runs the pipeline; nothing reaches the screen unless a fail-closed gate can prove it, and
nothing reaches your channel as anything but a private upload unless you flip a switch.

It started as "Dan's Agents on the Clock", a show recapping the work of one developer's Claude Code and Codex agents,
and this repository is a sanitized copy of that working pipeline, published so you can learn from it and run your
own.

## How it works

```
 agent logs ----+                        your config folder (never in the repo)
 merged PRs ----+                        settings.toml  lanes.json  embargo.json
 your adapters -+                        voices.json    publish.json
                |                                 |
                v                                 v
   +---------+   +-------+   +------+   +-------+   +--------+   +---------+   +--------+
   | collect |-->| write |-->| gate |-->| voice |-->| render |-->| publish |-->| notify |
   +---------+   +-------+   +------+   +-------+   +--------+   +---------+   +--------+
        |            |           |          |            |             |            |
   ledger.json  script.json verdict.json  voice/     episode.mp4   upload.json   notify.json
   (the only    (one        (14 checks, (Chatterbox render.json   (Studio in    (your command,
    thing the    headless    bound to    + Whisper   (bound to     your Chrome,  or the state
    writer       claude -p,  script and  word times)  the verdict) private by    folder)
    sees)        no tools)   ledger)                               default)
                                                                         |
                                                    <state>/uploads/ledger.jsonl: one video per day, ever
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
6. **publish** re-checks both verdicts against each other, stages the exact rendered bytes, and has the Studio driver
   (`release/studio.py`) upload them through YouTube Studio in your own signed-in Chrome: private by default, public
   only when `publish.json` says so, read back and verified either way.
7. **notify** sends one item per day and outcome (ready for review, published, quiet, stopped, needs you) to a command
   you choose, or writes it to the state folder and prints it.

`python3 -m daily.run` runs the stages in order, records each in `status.json`, resumes where a run stopped, and holds a
kernel lock so two runs never overlap. `docs/how-it-works.md` walks through the stages and every file they write;
`docs/configuration.md` describes every file they read; `docs/runbook.md` covers the timer, the browser and what to do
when a run stops.

## What you need

- Linux with Python 3.11 or newer (the text half is standard library only and also runs on macOS; the example timer
  is a systemd user unit)
- [Claude Code](https://claude.com/claude-code) signed in: the writer is one headless call on your own login, capped
  by `writer.budget_usd`
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

For the release half:

- Google Chrome (or Chromium) with a profile of its own, signed in to the Google account that holds your channel and
  started with a DevTools port on the loopback (docs/runbook.md, "The browser")
- [agent-browser](https://github.com/vercel-labs/agent-browser) 0.25 (`npm install -g agent-browser@0.25.4`,
  Apache-2.0), which the Studio driver drives that Chrome with
- A YouTube channel. Uploads go through the Studio web page, not the Data API (an unaudited API project can only
  upload private videos)

## Quickstart

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
gate check with its evidence).

Then the media half, still dry (silence stands in for the voice, so no TTS or Whisper is needed): set `render.kit`
in settings.toml, fill in `voices.json`, and run the rest of the rehearsal:

```bash
python3 -m daily.run --day yesterday --from-stage voice --dry
```

It renders `episode-dry.mp4` and `contact-sheet-dry.jpg`, writes the `upload-item.json` the Studio driver would get,
and reports nothing. When the rehearsal looks right, set `channel.id`, start the browser, and go live, private:

```bash
python3 release/studio.py preflight          # proves the browser reaches your channel's Studio
python3 -m daily.run --day yesterday         # a real run: a private upload and one item
```

Install the timer from `systemd/` (docs/runbook.md, "Install the timer") once a few attended days look right.

## Configuration

Everything you configure lives in `~/.config/orion-on-the-clock/` (`ORION_CLOCK_CONFIG`); everything the pipeline
writes lives in `~/.local/state/orion-on-the-clock/` (`ORION_CLOCK_STATE`). Neither is ever in the repository.

| File | What it holds |
| --- | --- |
| `settings.toml` | time zone, show names and disclosure line, sources, the writer, the voice and render toolchain, the channel, the browser endpoint (or a pool lease), the notify command |
| `lanes.json` | which repos may appear and how (public or headline), hard excludes, blocked names |
| `embargo.json` | terms that must not appear until you lift the embargo |
| `voices.json` | the host's reference recording and every agent's voice, color and glyph |
| `publish.json` | private (the default) or public uploads |

Loading is strict: an unknown key, a wrong type or a missing required key stops the run with a short code that names
the key. Nothing falls back to a default that publishes. `config.example/` has every file with comments, and
docs/configuration.md every key.

## What it costs to run

- **Money:** your Claude Code plan covers the writer. Each call is capped by `writer.budget_usd` (default 2 USD, at most
  two attempts a day) and `writer-run.json` records what each attempt reported; one pilot day of the original pipeline
  (two writer attempts) came to about 0.21 USD of notional usage. Everything else is open source and runs on your
  machine. YouTube uploads are free.
- **Time:** collect, write and gate take a minute or two. Voicing an episode takes a few minutes with a GPU and about
  an hour on a CPU (Chatterbox there needs roughly 55 seconds of computing per second of speech); Whisper and the
  render add a few minutes more. The upload and its checks take a few minutes, and a public release up to five more
  while YouTube's embed lookup catches up.
- **Attention:** in private mode, one look and one tap a day in YouTube Studio, plus whatever a stop asks of you
  (docs/runbook.md lists every needs-you code and what it means).

## Safety design, in brief

- **The ledger is the only thing the writer sees.** Repos are allowlisted by lane, hard excludes are left out even of
  the counts, and a session that touched an excluded folder gives no quotes or notes.
- **Untrusted text stays data.** Agent logs and PR bodies are fenced as data, and the writer has no tools.
- **The gate fails closed**, on raw and normalized text, and its verdict binds the script and ledger: text edited
  after the gate is never voiced, rendered or uploaded.
- **One video per day.** A receipt resumes a half-finished upload and an uploads ledger outside every day's folder
  survives a lost receipt; the day is claimed in both before the bytes go up, and a save is recorded before it is
  pressed, so a rerun never presses twice.
- **One marked tab, pinned targets, a loopback browser.** The Studio driver acts only in a tab it opened and marked,
  presses only fixed targets (a static test pins them), connects only to a DevTools endpoint on the loopback, proves the
  browser is a headed Chrome first, and never closes your browser.
- **Private by default; public is a switch you flip.** Only `"visibility": "public"` in publish.json turns public mode
  on, the driver takes the visibility only from its command line, can save only private or public (never unlisted or
  scheduled), and proves a public release live before it says so.
- **Voices are consented and checked**: the host's clone only from the recording you name, checked by hash; an agent
  never speaks in a person's voice.

docs/safety.md explains each mechanism and why it is there; SECURITY.md lists what is and is not defended;
docs/lessons.md is what building it taught.

## Repository layout

- `daily/`: the pipeline package, one module per stage; `daily/sources/` the log readers; `daily/checks/` the built-in
  redactor, slop gate and secret scanner; `daily/engine/` the HyperFrames composition template; `daily/unit_failed.py`
  the timer's failure handler; `daily/tests/` the suite
- `release/`: the YouTube Studio driver (`studio.py`) and its tab-pinned browser primitives (`pinned.py`)
- `systemd/`: an example timer, service, failure unit and entry script
- `config.example/`: commented settings, voices, lanes, embargo and publish files to copy into your config folder
- `docs/`: how it works, configuration, the runbook, the safety design, the spec, lessons learned, the media kit and
  voice, sources and the adapter contracts (with worked examples), the writer's CLI notes

## Credits

Built by Dan Mercede's agents (Claude Code and Codex), from the pipeline behind "Dan's Agents on the Clock" ([@DanMercedeAI](https://www.youtube.com/@DanMercedeAI) on YouTube). Companion projects: [orion-browser](https://github.com/OrionArchitekton/orion-browser-oss) for a shared, leased browser pool and [orion-pm](https://github.com/OrionArchitekton/orion-pm-oss) for a coordination board. The Studio
driver drives Chrome through [agent-browser](https://github.com/vercel-labs/agent-browser); the episodes are rendered
with [HyperFrames](https://github.com/heygen-com/hyperframes), voiced with
[Chatterbox](https://github.com/resemble-ai/chatterbox) and timed with
[faster-whisper](https://github.com/SYSTRAN/faster-whisper). MIT licensed; see `LICENSE`.
