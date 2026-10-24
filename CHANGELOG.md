# Changelog

## Unreleased (0.1.0)

First public MIT release: the text half of the pipeline, ported as a sanitized copy of a working private one.

- Collect: Claude Code and Codex session log readers (counts, session notes, quote candidates, exclusion taint on any
  day), merged PRs through the GitHub CLI, counts-only agent adapters and a tracker adapter loaded from the operator's
  own files (a tracker must prove its record reaches the day, or its arcs stay unknown), and a ledger built from an
  allowlist of lanes with hard excludes, blocked names and an embargo.
- Write: one headless `claude -p` call with tools, settings and MCP servers off, the ledger fenced as data, one repair
  attempt for a bad shape, an out-of-budget draft or a voice tell, and a cost record per attempt. The show's names
  and the host's voice notes come from settings.
- Gate: fourteen fail-closed checks with evidence, including a word-for-word re-read of the quote from its source
  line, the built-in redactor, slop gate and secret scanner, names, embargo, word budget, no code and grounded
  numbers; verdicts bound to the script and ledger hashes.
- Orchestration: stage order, resumable `status.json`, a kernel lock, quiet days, dry runs in their own `<day>-dry`
  folder (seeded by copy for a later-stage rehearsal), sanitized reasons and documented exit codes.
- Config layer: `ORION_CLOCK_CONFIG` and `ORION_CLOCK_STATE`, a strict `settings.toml` (time zone, show, sources,
  checks, writer, and the channel, browser, notify and TTS keys later stages read), commented examples.
- Voice: the host lines in a cloned voice from the operator's own reference recording (hash checked before every
  run), the confessional quote in its agent's built-in voice made distinct by an allowlisted ffmpeg chain (an agent
  never gets a reference recording, and the plan proves by content that no agent line used the host's conditioning),
  one Chatterbox run proved by its results file, local by default or on an optional GPU box over ssh (read-only
  preflight, nothing remote ever stopped, one local fallback, run folders removed and pruned), faster-whisper word
  times mapped onto the lines' own words with a coverage floor, a voice manifest, and an advisory host-line lint.
  A dry run voices silence so the render can be rehearsed without a GPU.
- Render: the timeline, karaoke captions and a HyperFrames composition (scoreboard with unknown counts left out, story
  cards, Steal-this and confessional cards) whose text comes only from gated sources; a ducked music and cue mix at
  -14 LUFS; HyperFrames from a configurable command with an optional sandbox prefix and its telemetry off; five render
  checks, a contact sheet and a render verdict bound to the episode, the script, the ledger and the voices; failed
  renders kept under rejected names and earlier passing renders archived (three kept).
- Config: `voices.json`, and settings for the TTS and Whisper environments, the remote TTS host, the media kit, the
  HyperFrames command and sandbox, and the channel handle.
- The release stages (publish, notify) are registered placeholders that fail closed.

Not bundled: Claude Code, the GitHub CLI, git, ffmpeg, Node, HyperFrames, Chatterbox, faster-whisper, media, fonts,
music or a voice reference. Bring your own (docs/media-kit.md).
