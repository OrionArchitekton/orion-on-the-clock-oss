# Changelog

## 0.1.0

First public MIT release: the whole pipeline, from agent logs to a verified upload, ported as a sanitized copy of a
working private one.

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
  checks, writer, the voice and render toolchain, the channel, the browser and the notify command), commented examples;
  a missing setting is named as settings.toml spells it (`missing:channel.id`).
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
- Publish: both verdicts read once and bound to each other (the render verdict's script and ledger hashes must be the
  accepted text's), a staging copy of the exact rendered bytes, the disclosure line checked and appended exactly once,
  one video per day through the receipt, the driver's exit codes mapped to short reasons and needs-you codes, and
  public mode only from `publish.json` saying `"visibility": "public"` (anything else is private, never an error).
- Studio driver (`release/studio.py`, `release/pinned.py`): private or public only, from its command line; a new tab of
  its own, marked at creation and re-checked in the same batch as every action; pinned click targets with a static
  test over its source; exact done labels per visibility (Save, Publish) proved in the script that presses; the save
  attempt recorded before the press; the "Video published" dialog closed by its own button; verification by a fresh
  read and oEmbed (refused for private, an embed of this video within a bound for public); receipts and an uploads
  ledger outside every day's folder; a channel switch in, an optional switch back to `channel.home_id`, one re-open on
  an empty channel read and a private stop screenshot; a signed-out browser is a stop for you. The browser is a
  loopback DevTools endpoint from settings (or a leased one, `browser.lease` and `browser.release`), proved to be a
  headed Chrome that is not marked automated, and never closed: the driver stops its own agent-browser session.
- Notify: one item per (day, outcome) with revisions, superseding and closed-outside, through `notify.command` (a JSON
  message on stdin) or the default sink (the state folder and stdout); run.py exits 5 for a failure it reported.
- Timer: `daily/unit_failed.py` (the item for a run that died before it could report, keyed by the day the run chose,
  under the lock notify uses), and example systemd units: a 05:30 local timer with catch-up, the service, the failure
  unit and an entry script that builds PATH and refuses to start without node.
- Docs: the runbook, the safety design, the spec and lessons learned.

Not bundled: Claude Code, the GitHub CLI, git, ffmpeg, Node, HyperFrames, Chatterbox, faster-whisper, Chrome,
agent-browser, media, fonts, music or a voice reference. Bring your own (docs/media-kit.md, docs/runbook.md).
