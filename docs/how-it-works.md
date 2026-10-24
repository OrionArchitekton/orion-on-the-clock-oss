# How it works

One run makes one episode about one day. `python3 -m daily.run --day <day>` runs seven stages in order, records each
in `<state>/<day>/status.json`, and resumes where a run stopped:

```
collect -> write -> gate -> voice -> render -> publish -> notify
ledger     script   verdict  voice/   episode.mp4
                             words    render.json
```

Every stage reads the files the stage before it wrote, and every stage after the gate reads the script only through
the gate's verdict. A stage fails closed: it passes only on positive evidence, and its reason in `status.json` is a
short code, never text from a log, a script or an exception. The release half (publish, notify) arrives in a later
release; until then those two stages are placeholders that fail closed.

## collect, write, gate

- **collect** reads one calendar day of agent logs and merged PRs and writes `ledger.json`, the only thing the writer
  sees. `docs/sources.md` covers the sources and the lane rules.
- **write** turns the ledger into `script.json` with one headless `claude -p` call. `docs/writer-cli.md` has the
  details.
- **gate** runs fourteen checks on the script and writes `verdict.json`, which carries the sha256 of the script and of
  the ledger it judged. `docs/configuration.md` lists what each check reads.

## voice

The host's voice says the host lines, the confessional quote plays in its agent's voice, and every line gets word
times for the captions.

**Reads:** `verdict.json`, `script.json` and `ledger.json` in one read (each file once, every verdict check made on the
very bytes that are then parsed); `voices.json` from your config folder, once, hashed; settings.toml (`tts.*`,
`whisper.*`, `writer.avoid_words`).

**Writes** (in the day's folder):

| File | What it is |
| --- | --- |
| `voice/lines.json` | every line in speaking order: id, scene, voice, text and Chatterbox settings |
| `voice/voice-manifest.json` | what this run used: the sha256 of the voices.json bytes, the host reference digest, each line's voice, settings and ffmpeg chain, and where voicing ran |
| `voice/<id>.wav` | the final take of each line (24 kHz mono) |
| `voice/<id>.raw.wav` | the untouched take of a line that has an ffmpeg chain (every agent line) |
| `voice/<id>.words.json` | the line's own words with times, its length, its coverage and its source (`whisper`, or `dry`) |
| `voice/tts-results.json`, `voice/tts.log` | the runner's proof of work and its output (`tts-remote.log` after a remote attempt that fell back) |
| `voice/whisper.log` | the recognizer's output |
| `hostlint.json` | the advisory host-line lint: tells that make a line sound written rather than said |

**Steps:**

1. Remove this stage's outputs from an earlier run (a symbolic link is unlinked, never followed).
2. The text verdict must accept the current script and ledger, else `gate_not_accepted`.
3. Build the lines. Host lines come from the script; the confessional's words come from the ledger by `quote_id`
   (the quote the gate re-read word for word from its source), never from the writer. A quote without an introduction,
   an introduction without a quote, or an empty line is an error, not a skipped beat. Every agent in the ledger's
   coverage needs an entry in voices.json (`voice_missing:<agents>`), because the scoreboard labels it.
4. Write the lint record (advisory: it never blocks).
5. A dry run stops here: each line becomes silence at 0.3 s a word with evenly spaced word times, so compose and render
   run without a GPU.
6. The host reference must exist and its sha256 must start with `host.sha256_prefix` (`voice_ref:missing`,
   `voice_ref:mismatch`).
7. Chatterbox voices every line in one run of `tts_runner.py`. The runner takes the shared TTS lock itself, checks its
   inputs before taking it, loads the model once, snapshots the built-in voice before it reads the host reference, and
   sets a fresh copy of each line's own conditioning before every sentence, so an agent line can never inherit the
   host's voice; it also proves by content that no agent's conditioning matches the host's. Its exit code is not the
   proof: `tts-results.json` must list exactly these line ids in order, carry the host reference digest, and every
   wav must exist and hold bytes.
8. If voices.json changed while the runner ran, stop (`voice_config_changed`).
9. Apply each line's ffmpeg chain (agent voices: a pitch step and a tempo step, perhaps an equalizer or a light echo),
   keeping the untouched take. The chain is checked against an allowlist of audio filters, so it cannot read or write
   a file.
10. faster-whisper listens to each line (an agent line on its clean take; its length must agree with the final take at
    the chain's speed, else `voice:raw_length`). Its timed words are mapped onto the line's own words, so captions never
    show a recognizer's spelling, and coverage is the share of the line's words it heard. A line under 60% fails the
    stage (`align_coverage:<line>=<coverage>`).

**Where voicing runs** (settings.toml `tts.target`): `local` runs the runner on this machine with `tts.python`;
`remote` runs it on `tts.host` over ssh; `auto` (the default) tries the remote host first when one is set and falls
back to this machine exactly once. A remote run is read only until it starts: nvidia-smi must show at least 4.5 GiB
free and no GPU process except the names in `tts.remote_gpu_allow`; anything else is someone else's job, and the run
voices here instead (nothing remote is ever paused or stopped). It uses a fresh dated folder under `tts.remote_dir`,
runs under `timeout` with CUDA required, fetches only the wavs and the results file, and removes its folder on every
outcome; dated folders older than a week are pruned. A fallback is recorded as `remote_skip` (`unreachable`, `busy`,
`low_memory`, `rsync_failed`, `runner_failed`, `bad_results`, `no_cuda`) in `tts-results.json` and the manifest.

**Reason codes:** `settings_missing`, `stale_cleanup`, `gate_not_accepted`, `quiet_script_at_voice`,
`voice_config:<code>` (voices.json missing or invalid; the code names the key, such as `missing:host.ref`),
`voice_missing:<agents>`, `voice_lines:<class>`, `missing:whisper_python`, `missing:tts_python`, `voice_ref:<code>`,
`voice_config_changed`, `align_coverage:...`, and `voice:<code>` (`tts_runner_failed`, `tts_results_unreadable`,
`tts_results_ids`, `tts_results_ref`, `tts_wav_missing`, `raw_length`, `tts_remote_<code>` when the target is
`remote`) or `voice:<class>` for anything else (such as `voice:TimeoutExpired`).

## render

The voiced lines become a 9:16 video with karaoke captions, a scoreboard, story cards and the confessional card.

**Reads:** the verdict, script and ledger in one read; `voice/lines.json`, `voice/voice-manifest.json` and the words
files; `voices.json`, once (its sha256 must equal the manifest's); your media kit (`render.kit`, docs/media-kit.md).

**Writes:**

| File | What it is |
| --- | --- |
| `build/comps/daily/index.html` | the filled HyperFrames composition (template: `daily/engine/template-daily.html`), with the kit's fonts and GSAP and the mix beside it |
| `build/timeline.json` | every scene and line start, the cues and the caption groups |
| `build/logs/` | HyperFrames' check and render logs, and the problems found in them |
| `episode.mp4` | the episode: HyperFrames' video with the master mix as AAC |
| `render.json` | the render verdict: five checks with evidence, the episode's sha256, and the hashes of the script, ledger and voices.json it was made from, plus the host reference digest |
| `contact-sheet.jpg` | a frame every 2 s and around every scene change, for a person's review (`contact-sheet-2.jpg` for a long render) |
| `episode-rejected.mp4`, `contact-sheet-rejected.jpg` | a failed render's output, kept for inspection under names nothing publishes |
| `previous/<UTC time>/` | earlier passing renders, archived (three kept, dry runs apart) |

A dry run writes `episode-dry.mp4`, `render-dry.json` and `contact-sheet-dry.jpg` instead, and never touches the real
files, so a rehearsal can never be published.

**Steps:**

1. Archive and clear: an earlier passing render (its verdict passes and binds its episode) moves to `previous/`; an
   episode nothing vouches for is renamed to the rejected names; the build folder goes. Nothing ready-looking stays at
   the top level while this run works, and a failed rerun never loses the last good episode.
2. The text verdict must accept the current script and ledger (`gate_not_accepted`).
3. voices.json must hash to what the voice stage recorded (`voice_config_changed`), and `voice/lines.json` must be
   exactly the lines `voice.build_lines` gives for this script under those voices, with the settings the manifest
   recorded (`voice_mismatch`).
4. Compose: each scene is a lead, its lines and a hold; the Steal-this card stays at least 3 s; a day under 60 s gets
   its holds stretched (at most 2 s each); a long day is never trimmed. Captions are the lines' own words, five at a
   time. On-screen text comes only from gate-scanned script fields, the gate-verified quote, voices.json labels, your
   series name and channel handle, the day and fixed words; every string is refused if it holds a long dash, and every
   value reaches the page as escaped JSON inserted with text-only DOM calls. A PR or task count the ledger holds as
   unknown (GitHub off, no tracker) is left off the scoreboard, never drawn as a zero; an agent the day could not read
   shows NO DATA TODAY.
5. Mix: voices at their starts, the theme under the open and the out, the bed under the talk, both ducked under speech,
   the cues, then two-pass loudnorm to -14 LUFS at exactly the timeline's length.
6. `hyperframes check`: any warning or error except two advisory code-organization lints stops the render
   (`hf_check`). Then `hyperframes render`; a log line about an unresolved font, a page error, a failed load or a render
   that went on despite problems stops it too (`hf_render`).
7. Remux the master mix in, untouched video stream, and probe the result.
8. The five checks: duration 60 to 75 s and within one frame of the timeline; h264 1080x1920 yuv420p 30 fps; -15 to
   -13 LUFS integrated; an audio stream; every captioned line shows exactly its own words, in order, and every line was
   heard at 60% or more. All five pass, or the episode is renamed to the rejected name and the verdict says it failed
   (`render_checks:<names>`).
9. The contact sheet, then `render.json`.

**The verdict binding.** Nothing after the gate trusts a file name. Voice and render read the script and ledger only
through the text verdict, in one read, so a script edited after the gate (text nobody reviewed) is refused, not voiced
or rendered. Render also holds voices.json and the voiced lines to the voice manifest, so the episode is always the
reviewed text in the recorded voices. `render.json` records the hashes of the script and ledger it was made from and
the episode's own hash, for the publish stage (the release half) to hold against the accepted text before an upload:
a render of other text, or an episode swapped on disk, does not match and does not ship. A new collect archives a passing render and removes the
voice outputs, so media made from an old ledger never sits beside a new one.

**HyperFrames** runs as `render.hyperframes` (default `hyperframes` on your PATH) from `build/comps`, with no display
variables, its telemetry, update check and skill install switched off, in a session of its own that is killed whole on
a timeout (10 minutes for the check, an hour for the render). `render.sandbox` puts a command in front of it, with
`{timeout}` replaced by the step's timeout in seconds. A sandbox is recommended: the render opens a headless browser on
a page built from your data. On Linux with unprivileged user namespaces, this script runs it with no network and in a
PID namespace of its own, so a timeout kills every process inside, a detached browser included:

```bash
#!/usr/bin/env bash
# render-sandbox.sh <timeout-seconds> <command> [args...]
set -euo pipefail
T="$1"; shift
exec timeout -k 15 "$T" unshare -r -n -p -f --mount-proc bash -c 'ip link set lo up; exec "$@"' sandbox "$@"
```

```toml
[render]
sandbox = ["~/bin/render-sandbox.sh", "{timeout}"]
```

**Reason codes:** `stale_cleanup`, `settings_missing`, `missing:render_kit`, `gate_not_accepted`, `no_voice_lines`,
`no_voice_manifest`, `voice_config_changed`, `voice_mismatch`, `compose:<class>`, `mix:<class>`, `hf_check_failed`,
`hf_check`, `hf_render_failed`, `hf_render`, `hf_timeout`, `hf:<class>`, `render_probe:<code or class>` (`probe_timeout`,
`probe_failed`, `probe_unreadable`), `render_checks:<check names>` (read `render.json` for the evidence).

## Rehearsing the media half

`python3 -m daily.run --day yesterday --until render --dry` runs every stage up to render in `<state>/<day>-dry/`:
the writer is called, the voice is silence, and the render is real (HyperFrames, the mix and the five checks) with the
duration window applied to the silent episode. `--dry --from-stage voice` copies a gated real day's text into the dry
folder first, so you can rehearse the media for a day whose script already passed, without calling the writer again.
Open `contact-sheet-dry.jpg` to see the layout, and `episode-dry.mp4` to hear the mix around the silence.
