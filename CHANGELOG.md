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
- The media and release stages are registered placeholders that fail closed.

Not bundled: Claude Code, the GitHub CLI, git, media, fonts, music or a voice reference. Bring your own.
