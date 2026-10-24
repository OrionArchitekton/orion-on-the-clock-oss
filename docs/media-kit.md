# Media kit and voice: bring your own

This repository ships no media: no fonts, music, sound effects, images, video or voice recordings. The render stage
reads them from your media kit folder (settings.toml `render.kit`) and the voice stage reads your voice reference from
`voices.json`. This page lists exactly what each expects, where to get free-licensed files, and what each license asks
of you. Check every license yourself before you publish: terms change, and you are the one publishing.

## The kit folder

```
<render.kit>/
  gsap.min.js                  the animation library the composition loads
  fonts/Anton.ttf              display face: the big words, numbers and cards
  fonts/InterVariable.ttf      body face: labels, captions (also used for the contact sheet's labels)
  sound/theme.wav              the show theme, under the open and the out (loops; 3 s or more)
  music/bed.wav                the bed under the story, Steal-this and confessional (loops; any length)
  sound/scoreboard_jingle.wav  plays as the scoreboard opens
  sound/section_break.wav      plays as the story starts
  sound/word_pop.wav           plays as each story card and the Steal-this card lands
  sound/cta_pop.wav            plays with the subscribe button and the like sticker
  sound/entrant_<agent>.wav    optional: an agent's own sting at its confessional (entrant_codex.wav, ...)
```

Every file but the stings is required. The render stage checks the kit before it does any work and stops with
`mix:FileNotFoundError` when one is missing; HyperFrames' own check stops a render whose fonts do not resolve. An agent
with no sting file simply gets none.

Sounds can be any format ffmpeg reads (the names end in `.wav`, so save them as WAV). The mix converts everything to
48 kHz stereo, ducks the music under speech, and normalizes the whole episode to -14 LUFS, so you do not need to level
the files yourself. Short, dry cues (under half a second) work best; the beds loop with a 0.6 s fade at each end.

## Where to get each piece

| Piece | Free source | License | What it asks |
| --- | --- | --- | --- |
| `fonts/Anton.ttf` | Anton on Google Fonts (fonts.google.com/specimen/Anton) | SIL Open Font License 1.1 | Keep the license with the font if you redistribute it; do not sell the font by itself. Using it in a video is fine. |
| `fonts/InterVariable.ttf` | Inter (rsms.me/inter, or its GitHub releases); the release zip holds `InterVariable.ttf` | SIL Open Font License 1.1 | As above. |
| `gsap.min.js` | The `gsap` package on npm: `npm pack gsap@3.14.2`, then take `package/dist/gsap.min.js` from the tarball | GSAP Standard License (no charge; read it at gsap.com/standard-license) | Read the terms; they cover use in videos you make. |
| theme and bed | Your own music, a CC0 track (freesound.org, filter by Creative Commons 0), or the YouTube Audio Library | CC0 needs nothing; YouTube Audio Library tracks are free for YouTube videos, some only with attribution | Attribution, when a track asks for it, goes in your description. Never use music you do not have the rights to: a claim can block a Short. |
| sound cues and stings | CC0 effects on freesound.org, or make your own with a synthesizer such as sfxr or jsfxr | CC0, or yours | Nothing for CC0. Keep a note of each file's source and license next to the kit. |

The composition was built and checked with GSAP 3.14.2, Anton and Inter (variable). Another GSAP 3 release will most
likely work; another typeface will render but may not fit the boxes the layout was measured with.

## A placeholder kit in one minute

To try a dry run before you have chosen real sounds, generate tones with ffmpeg (they sound like test tones, which is
what they are; never publish with them). You still need the two fonts and `gsap.min.js` from the table above.

```bash
KIT=~/.local/share/orion-on-the-clock/kit
mkdir -p "$KIT/fonts" "$KIT/sound" "$KIT/music"
tone() { ffmpeg -nostdin -v error -y -f lavfi -i "sine=f=$2:d=$3:r=48000" -ac 2 "$KIT/$1"; }
tone sound/theme.wav 220 4
tone music/bed.wav 330 4
tone sound/scoreboard_jingle.wav 660 0.4
tone sound/section_break.wav 550 0.4
tone sound/word_pop.wav 880 0.15
tone sound/cta_pop.wav 990 0.15
```

## The voice reference

The host lines are read by a voice clone made from one reference recording: `voices.json` `host.ref`.

- **Consent first.** Clone only your own voice, or the voice of someone who has agreed in writing to this use. Say in
  every description that the narration is synthetic (settings.toml `show.disclosure` is appended to each one).
- **The recording.** A WAV of 10 to 20 seconds of clear, natural speech by one person, no music, no room echo. Talk
  the way you want the host to sound; Chatterbox copies delivery as well as timbre.
- **Where it lives.** On the machine that runs the text to speech, outside this repository (`~` expands there). With
  a remote TTS host (`tts.host`) the file must exist at that path on the remote machine; the runner checks it there.
- **The hash.** Put the start of its sha256 in `host.sha256_prefix` (`sha256sum host-reference.wav | cut -c1-16`).
  Every real run checks it before any audio is made, and stops with `voice_ref:missing` or `voice_ref:mismatch`
  rather than voice the day with another recording. The render verdict records the digest, so every episode says which
  recording it was voiced from.
- **The agents.** An agent never gets a reference recording: its lines (the confessional quote) are read by
  Chatterbox's built-in voice made distinct by its `post` ffmpeg chain. A harness entry with a `ref` is refused, so no
  agent can speak in a person's voice, and the runner proves by content that no agent line was voiced with the host's
  conditioning.

## Tools the media half runs

| Tool | Version this was built with | Where it runs | Settings key |
| --- | --- | --- | --- |
| ffmpeg and ffprobe | 6.1 | the pipeline's machine (on `PATH`) | none |
| Chatterbox (`pip install chatterbox-tts==0.1.7`, with torch 2.7) | 0.1.7 | its own Python environment, here or on the remote TTS host | `tts.python`, `tts.remote_python` |
| faster-whisper (`pip install faster-whisper==1.2.1`) | 1.2.1, model `small.en` on CPU | its own Python environment | `whisper.python`, `whisper.model` |
| HyperFrames (npm package `hyperframes`, github.com/heygen-com/hyperframes) | 0.8.36, with Node 24 (22 or newer) | the pipeline's machine | `render.hyperframes` |
| numpy and Pillow | numpy 2, Pillow 12 | the pipeline's own Python (`pip install -e '.[media]'`) | none |

Chatterbox downloads its model weights from Hugging Face on first use; run it once by hand on each machine that voices,
because a remote run sets the hub offline and a first unattended run would otherwise stall on the download.
HyperFrames drives a headless Chromium; if it cannot find one, set `HYPERFRAMES_BROWSER_PATH` in the environment the
timer runs in.
