"""Agent log sources, one module per source.

Built in: `claude` (Claude Code session logs) and `codex` (Codex session logs), the two transcript readers whose quotes
the gate can re-read and verify, and `github` (PRs merged that day, through the `gh` CLI). `transcripts` holds what the
two transcript readers share. Everything else plugs in from the operator's own files: `adapters` loads counts-only
agent sources and `tracker` loads a task tracker (docs/sources.md has both contracts and worked examples).
"""
