# Headless writer behavior (verified)

What the write stage (`daily/writer.py`) relies on in the `claude` CLI, checked against a live call. Re-check it
when you upgrade Claude Code.

- Verified on: 2026-10-17 with `claude --version` = 2.1.302 (Claude Code)
- Flags: `-p --model <writer.model> --tools "" --setting-sources "" --strict-mcp-config --output-format json
  --json-schema <schema text> --no-session-persistence --max-budget-usd <writer.budget_usd>`, prompt on stdin.
- `--json-schema` takes the schema TEXT (not a path).
- Structured result field: `structured_output` (writer.RESULT_FIELD). `result` also carries the same object as a
  JSON string; the writer reads `structured_output` only.
- Other keys seen: api_error_status, duration_api_ms, duration_ms, fast_mode_state, first_content_frame_ms,
  input_attachments_detail, is_error, modelUsage, num_turns, permission_denials, process_turn_index,
  queued_turn_count, result, result_index, safety_stops, session_id, stop_reason, subagent_stats, subtype,
  terminal_reason, time_to_request_*, total_cost_usd, ttft_ms, ttft_stream_ms, turn_start_resume_kind, type,
  usage, uuid. Usage keys: cache_creation, cache_creation_input_tokens, cache_read_input_tokens, fallback_credit,
  inference_geo, input_tokens, iterations, output_tokens, output_tokens_details, server_tool_use, service_tier,
  speed. num_turns on a no-tool call: 2. `modelUsage` lists a small helper model next to the requested one.
- `total_cost_usd` is reported even on a subscription login (notional; the smoke call showed 0.0149).
- Provider variables (every `ANTHROPIC_*`, `CLAUDE_CODE_USE_*` and `AWS_*` name, and the proxy variables) are dropped
  from the writer's environment (`writer.writer_env`), so the call runs on your own Claude Code login or fails.
- Full schema check (2026-10-17, same flags): `daily/script_schema.json` (nested objects, additionalProperties
  false, maxLength, maxItems, no const or pattern) is accepted; `structured_output` carried all 13 top-level keys
  including `steal_this`, with num_turns 2 and no error.
- No instruction files load (2026-10-17): run from a directory under the home folder (where the work dir lives),
  the call reported no CLAUDE.md, AGENTS.md or project instructions, and its context was about 3.6k tokens (system
  prompt only). `--setting-sources ""` keeps ~/CLAUDE.md and its imports out of the writer.
