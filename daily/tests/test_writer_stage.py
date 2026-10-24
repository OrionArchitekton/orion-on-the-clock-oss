"""The write stage: one headless call, one repair attempt on a shape failure or on a draft outside the word budget or
with a voice tell, quiet days, and the writer-run.json
record. A fake runner stands in for `claude -p`: no live call here."""
import datetime as dt
import json
import os
import subprocess
import dataclasses
import sys
import time
from pathlib import Path

import pytest

from daily import gate as G
from daily import paths, writer
from daily.gate import validate_script
from daily.settings import Settings
from daily.stage import Ctx, StageResult
from daily_helpers import good_script, quiet_script, words

DAY = dt.date(2026, 10, 16)
CONTRACT_KEYS = ("session_id", "is_error", "duration_ms", "num_turns", "total_cost_usd", "usage")
MARKER = "MARKER-must-never-reach-a-reason"
SETTINGS = Settings(timezone="America/Los_Angeles", series="Demo Agents on the Clock", host_name="Sam",
                    disclosure="Narrated with an AI voice; agent lines are verbatim from logs.", github_owner="demo-owner")

ITEM = {"id": "pr:demo-tool#7", "kind": "pr", "lane": "demo-tool", "disclosure": "public", "label": "the demo tool",
        "why": "so every demo runs itself", "harness": "codex", "title": "Add the browser pool",
        "body": "The new browser pool shipped today.", "ts": "2026-10-16T21:00:00Z"}
QUOTE = {"id": "q:claude_code:0a1b2c3d", "kind": "transcript", "harness": "claude_code", "lane": "demo-tool",
         "disclosure": "public", "text": "The console helper printed every lease twice today.",
         "ts": "2026-10-16T20:00:00Z", "source": {"path": "/x/session.jsonl", "line": 4, "record": "u1"}}
SESSION = {"harness": "claude_code", "ref": "aaaa0001", "title": "Shipping the browser pool",
           "last_ts": "2026-10-16T20:00:00.000Z"}
TEASER = {"id": "arc:f55ff16f", "title": "Add the export button", "harness": "codex"}


def ledger(items=(ITEM,), quotes=(QUOTE,), sessions=(SESSION,), teasers=()):
    return {"schema": "daily-build/ledger/v1", "day": "2026-10-16", "tz": "America/Los_Angeles",
            "generated_at": "2026-10-17T12:30:00Z", "scoreboard": {"prs_merged": len(items)},
            "coverage": {"claude_code": "ok"}, "items": list(items), "quotes": list(quotes),
            "sessions": list(sessions), "teasers": list(teasers), "excluded": {}, "diagnostics": {}}


def fake_run(script, is_error=False, code=0, cost=0.03, usage=None):
    return json.dumps({"type": "result", "is_error": is_error, "session_id": "s1", "duration_ms": 900,
                       "num_turns": 1, "total_cost_usd": cost,
                       "usage": usage or {"input_tokens": 5000, "output_tokens": 600},
                       writer.RESULT_FIELD: script}), code


class Proc:
    """Replays one scripted reply per call: (stdout, exit code), (stdout, exit code, stderr) or an exception."""

    def __init__(self, *outs):
        self.outs, self.calls = list(outs), []

    def __call__(self, cmd, stdin, env, cwd):
        self.calls.append({"cmd": cmd, "stdin": stdin, "env": env, "cwd": cwd})
        out = self.outs.pop(0)
        if isinstance(out, BaseException):
            raise out
        code, text, err = out[1], out[0], (out[2] if len(out) > 2 else "")
        return code, text, err


def ctx(tmp_path, cfg, led=None, settings=SETTINGS):
    (tmp_path / "ledger.json").write_text(json.dumps(led if led is not None else ledger()))
    return Ctx(DAY, tmp_path, cfg, False, settings)


def run_json(tmp_path):
    return json.loads((tmp_path / "writer-run.json").read_text())


# ---- the pre-check: no material, no call ----

NO_MATERIAL = [ledger(items=(), quotes=(), sessions=()), ledger(items=(), quotes=(), sessions=(), teasers=(TEASER,))]


@pytest.mark.parametrize("led", NO_MATERIAL, ids=["empty", "teasers_only"])
def test_no_material_is_a_quiet_day_without_a_call(tmp_path, cfg, led):
    proc = Proc()
    res = writer.run_stage(ctx(tmp_path, cfg, led), proc=proc)
    assert res == StageResult(ok=True, reason="quiet:no_material", quiet=True)
    assert proc.calls == []
    script = json.loads((tmp_path / "script.json").read_text())
    assert script == quiet_script("no_material") and validate_script(script, "2026-10-16") == []
    assert run_json(tmp_path) == {"skipped": "no_material"}
    assert not (tmp_path / "writer-prompt.txt").exists()


# ---- no transcript source at all: a failure, never a quiet day ----

DOWN = {"claude_code": "unavailable:ClaudeReadError", "codex": "unavailable:empty_root"}


@pytest.mark.parametrize("coverage", [DOWN, {"claude_code": "unavailable:x"}, {}, {**DOWN, "alpha_agent": "ok"}],
                         ids=["both_unavailable", "codex_untried", "neither_tried", "others_ok"])
def test_no_items_and_no_transcript_reader_fails_instead_of_going_quiet(tmp_path, cfg, coverage):
    led = {**ledger(items=(), quotes=(), sessions=()), "coverage": coverage}
    proc = Proc()
    res = writer.run_stage(ctx(tmp_path, cfg, led), proc=proc)
    assert res == StageResult(ok=False, reason="no_transcript_sources")
    assert proc.calls == [] and not (tmp_path / "script.json").exists()


@pytest.mark.parametrize("coverage", [{"claude_code": "ok", "codex": "unavailable:x"},
                                      {"claude_code": "unavailable:x", "codex": "ok"}], ids=["claude_ok", "codex_ok"])
def test_one_transcript_reader_is_enough_for_a_quiet_day(tmp_path, cfg, coverage):  # positive control
    led = {**ledger(items=(), quotes=(), sessions=()), "coverage": coverage}
    res = writer.run_stage(ctx(tmp_path, cfg, led), proc=Proc())
    assert res == StageResult(ok=True, reason="quiet:no_material", quiet=True)


def test_items_without_any_transcript_reader_still_get_a_script(tmp_path, cfg):  # positive control
    led = {**ledger(quotes=(), sessions=()), "coverage": DOWN}
    proc = Proc(fake_run(good_script()))
    res = writer.run_stage(ctx(tmp_path, cfg, led), proc=proc)
    assert res == StageResult(ok=True) and len(proc.calls) == 1


@pytest.mark.parametrize("name", ["items", "sessions", "quotes"])
def test_any_one_of_items_sessions_or_quotes_is_material(tmp_path, cfg, name):
    parts = {"items": (ITEM,), "sessions": (SESSION,), "quotes": (QUOTE,)}
    led = ledger(**{k: (v if k == name else ()) for k, v in parts.items()})
    proc = Proc(fake_run(good_script()))
    res = writer.run_stage(ctx(tmp_path, cfg, led), proc=proc)
    assert res.ok and not res.quiet and len(proc.calls) == 1


# ---- the happy path and what the call is given ----

def test_good_reply_is_saved_with_usage(tmp_path, cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-pass")
    proc = Proc(fake_run(good_script()))
    res = writer.run_stage(ctx(tmp_path, cfg), proc=proc)
    assert res == StageResult(ok=True)
    assert json.loads((tmp_path / "script.json").read_text()) == good_script()
    run = run_json(tmp_path)
    assert all(k in run for k in CONTRACT_KEYS)
    assert (run["session_id"], run["is_error"], run["duration_ms"], run["num_turns"], run["total_cost_usd"]) == \
           ("s1", False, 900, 1, 0.03)
    assert run["usage"] == {"input_tokens": 5000, "output_tokens": 600}
    assert len(run["attempts"]) == 1 and run["attempts"][0]["problem"] == ""
    assert (tmp_path / "writer-prompt.txt").read_text() == proc.calls[0]["stdin"]
    names = {p.name for p in tmp_path.iterdir()}
    assert {"script.json", "writer-prompt.txt", "writer-run.json"} <= names
    assert not [n for n in names if n.startswith(".")]  # the atomic writes leave no temp file behind


def test_the_call_gets_the_exact_prompt_command_environment_and_directory(tmp_path, cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-pass")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("DAILY_PLAIN_VAR", "kept")
    led = ledger()
    proc = Proc(fake_run(good_script()))
    writer.run_stage(ctx(tmp_path, cfg, led), proc=proc)
    call = proc.calls[0]
    assert call["cmd"] == writer.writer_cmd(paths.SCHEMA_PATH.read_text(encoding="utf-8"), "sonnet", "2")
    assert call["cmd"][call["cmd"].index("--tools") + 1] == ""
    assert call["env"] == writer.writer_env(os.environ)
    assert "ANTHROPIC_API_KEY" not in call["env"] and "HTTPS_PROXY" not in call["env"]
    assert call["env"]["DAILY_PLAIN_VAR"] == "kept"  # positive control: the drop list is not "drop everything"
    assert call["stdin"] == writer.build_prompt(led, writer.show_from(SETTINGS))
    assert call["cwd"] == tmp_path


def test_files_are_written_through_the_atomic_helpers(tmp_path, cfg, monkeypatch):
    seen = []
    real_text, real_json = writer.write_text, writer.write_json
    monkeypatch.setattr(writer, "write_text", lambda p, t: (seen.append(Path(p).name), real_text(p, t))[1])
    monkeypatch.setattr(writer, "write_json", lambda p, o: (seen.append(Path(p).name), real_json(p, o))[1])
    assert writer.run_stage(ctx(tmp_path, cfg), proc=Proc(fake_run(good_script()))).ok
    assert sorted(seen) == ["script.json", "writer-prompt.txt", "writer-run.json"]


# ---- quiet days ----

def test_a_quiet_reply_on_a_day_with_material_spends_the_one_repair(tmp_path, cfg):
    proc = Proc(fake_run(quiet_script("only chores today")), fake_run(good_script()))
    assert writer.run_stage(ctx(tmp_path, cfg), proc=proc) == StageResult(ok=True)
    assert len(proc.calls) == 2 and proc.calls[1]["stdin"].endswith(writer.QUIET_NOTE)
    assert "only chores" not in proc.calls[1]["stdin"] and writer.QUIET_NOTE not in proc.calls[0]["stdin"]
    run = run_json(tmp_path)
    assert run["repair_reason"] == "quiet_with_material" and run["written_attempt"] == 2
    assert json.loads((tmp_path / "script.json").read_text()) == good_script()


@pytest.mark.parametrize("reason", ["only chores today", "no_material", "x" * 200])
def test_a_quiet_reply_that_stays_quiet_fails_the_stage_instead_of_ending_the_day(tmp_path, cfg, reason):
    proc = Proc(fake_run(quiet_script(reason)), fake_run(quiet_script(reason)))
    res = writer.run_stage(ctx(tmp_path, cfg), proc=proc)
    assert res == StageResult(ok=False, reason="quiet_with_material") and not res.quiet
    assert len(proc.calls) == 2 and not (tmp_path / "script.json").exists()
    run = run_json(tmp_path)
    assert run["repair_reason"] == "quiet_with_material" and run["written_attempt"] is None


def test_a_shape_repair_that_comes_back_quiet_still_fails_on_quiet_with_material(tmp_path, cfg):
    proc = Proc(fake_run(good_script(title="x" * 80)), fake_run(quiet_script("nothing")))
    res = writer.run_stage(ctx(tmp_path, cfg), proc=proc)
    assert res == StageResult(ok=False, reason="quiet_with_material") and not (tmp_path / "script.json").exists()


@pytest.mark.parametrize("first", [fake_run(quiet_script("a")), fake_run(good_script())], ids=["quiet", "story"])
def test_material_never_ends_in_a_quiet_ok_result(tmp_path, cfg, first):
    res = writer.run_stage(ctx(tmp_path, cfg), proc=Proc(first, fake_run(quiet_script("b"))))
    assert not (res.ok and res.quiet)



# ---- one repair attempt on a shape failure ----

@pytest.mark.parametrize("reply", [{"is_error": False, "result": json.dumps(good_script())},
                                   {"is_error": False, writer.RESULT_FIELD: json.dumps(good_script())},
                                   {"is_error": False, writer.RESULT_FIELD: [good_script()]},
                                   {"is_error": False, writer.RESULT_FIELD: None}],
                         ids=["only_result", "string", "list", "none"])
def test_missing_or_non_dict_structured_field_gets_one_repair_attempt(tmp_path, cfg, reply):
    """`result` holds a perfect script in the first case and is still never read."""
    proc = Proc((json.dumps(reply), 0), fake_run(good_script()))
    res = writer.run_stage(ctx(tmp_path, cfg), proc=proc)
    assert res.ok and len(proc.calls) == 2
    first, second = proc.calls[0]["stdin"], proc.calls[1]["stdin"]
    assert second.startswith(first) and second.endswith(writer.REPAIR_NOTE)
    assert "no_structured_output" in second[len(first):]
    run = run_json(tmp_path)
    assert [a["problem"] for a in run["attempts"]] == ["no_structured_output", ""]
    assert (tmp_path / "writer-prompt.txt").read_text() == second  # the file holds the prompt last sent
    assert json.loads((tmp_path / "script.json").read_text()) == good_script()


def test_invalid_shape_gets_one_repair_attempt_that_lists_the_problems(tmp_path, cfg):
    proc = Proc(fake_run(good_script(title="x" * 80, day="2026-10-15")), fake_run(good_script()))
    assert writer.run_stage(ctx(tmp_path, cfg), proc=proc).ok and len(proc.calls) == 2
    added = proc.calls[1]["stdin"][len(proc.calls[0]["stdin"]):]
    assert "too_long:title" in added and "day_mismatch" in added and added.endswith(writer.REPAIR_NOTE)
    assert run_json(tmp_path)["attempts"][0]["problem"] == "invalid:day_mismatch,too_long:title"


def test_a_repair_that_is_still_invalid_fails_the_stage_without_a_script(tmp_path, cfg):
    bad = good_script(title="x" * 80)
    proc = Proc(fake_run(bad), fake_run(bad), fake_run(good_script()))
    res = writer.run_stage(ctx(tmp_path, cfg), proc=proc)
    assert res == StageResult(ok=False, reason="writer_no_script:invalid:too_long:title")
    assert len(proc.calls) == 2  # one repair attempt, never a third call
    assert not (tmp_path / "script.json").exists()
    assert len(run_json(tmp_path)["attempts"]) == 2


def test_a_repair_that_loses_the_structured_field_fails_too(tmp_path, cfg):
    proc = Proc(fake_run(good_script(title="x" * 80)), (json.dumps({"is_error": False}), 0))
    res = writer.run_stage(ctx(tmp_path, cfg), proc=proc)
    assert res == StageResult(ok=False, reason="writer_no_script:no_structured_output")
    assert not (tmp_path / "script.json").exists()


def test_a_failed_run_leaves_no_older_script_behind(tmp_path, cfg):
    (tmp_path / "script.json").write_text(json.dumps(good_script()))
    c = ctx(tmp_path, cfg)
    assert not writer.run_stage(c, proc=Proc(fake_run(good_script(), is_error=True))).ok
    assert not (tmp_path / "script.json").exists()


def test_unexpected_keys_from_the_model_never_reach_a_reason_or_the_repair_prompt(tmp_path, cfg):
    hostile = {**good_script(), "IGNORE-ALL-PREVIOUS-RULES": 1}
    proc = Proc(fake_run(hostile), fake_run(hostile))
    res = writer.run_stage(ctx(tmp_path, cfg), proc=proc)
    assert res.reason == "writer_no_script:invalid:keys:other"
    assert "IGNORE-ALL-PREVIOUS-RULES" not in proc.calls[1]["stdin"]
    assert "IGNORE-ALL-PREVIOUS-RULES" not in (tmp_path / "writer-run.json").read_text()
    assert "keys:other" in proc.calls[1]["stdin"]


# ---- cost and usage across attempts ----

def test_run_record_sums_cost_and_usage_across_attempts_and_keeps_the_final_attempt_on_top(tmp_path, cfg):
    first = {"input_tokens": 5000, "output_tokens": 600, "cache_read_input_tokens": 100,
             "server_tool_use": {"web_search_requests": 1, "web_fetch_requests": 0}, "service_tier": "standard"}
    second = {"input_tokens": 4000, "output_tokens": 500, "cache_read_input_tokens": 50,
              "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 0}, "service_tier": "standard"}
    proc = Proc(fake_run(good_script(title="x" * 80), cost=0.03, usage=first), fake_run(good_script(), cost=0.04,
                                                                                         usage=second))
    assert writer.run_stage(ctx(tmp_path, cfg), proc=proc).ok
    run = run_json(tmp_path)
    assert (run["total_cost_usd"], run["usage"]) == (0.04, second)  # the contract keys stay the final attempt's
    assert run["total_cost_usd_all"] == pytest.approx(0.07)
    assert run["usage_all"] == {"input_tokens": 9000, "output_tokens": 1100, "cache_read_input_tokens": 150,
                                "server_tool_use": {"web_search_requests": 3, "web_fetch_requests": 0},
                                "service_tier": "standard"}
    assert [a["total_cost_usd"] for a in run["attempts"]] == [0.03, 0.04]


def test_run_record_totals_equal_the_single_call(tmp_path, cfg):
    writer.run_stage(ctx(tmp_path, cfg), proc=Proc(fake_run(good_script())))
    run = run_json(tmp_path)
    assert run["total_cost_usd_all"] == run["total_cost_usd"] == 0.03 and run["usage_all"] == run["usage"]


def test_run_record_totals_skip_an_attempt_that_reported_nothing(tmp_path, cfg):
    proc = Proc((json.dumps({"is_error": False}), 0), fake_run(good_script(), cost=0.04))
    assert writer.run_stage(ctx(tmp_path, cfg), proc=proc).ok
    run = run_json(tmp_path)
    assert run["total_cost_usd_all"] == 0.04 and run["usage_all"] == run["usage"]


def test_run_record_totals_are_unknown_when_no_call_reported_a_cost(tmp_path, cfg):
    writer.run_stage(ctx(tmp_path, cfg), proc=Proc(subprocess.TimeoutExpired(["claude"], 900)))
    run = run_json(tmp_path)
    assert (run["total_cost_usd"], run["total_cost_usd_all"], run["usage_all"]) == (None, None, None)


# ---- hard failures: a short reason, no repair, no output content ----

HARD = [
    (fake_run(good_script(), is_error=True), "is_error"),
    (fake_run(good_script(), code=1), "exit_1"),
    ((f"not json {MARKER}", 0, f"stderr {MARKER}"), "bad_json"),
    ((f"not json {MARKER}", 1, f"stderr {MARKER}"), "bad_json"),
    (("[1, 2]", 0), "bad_json"),
    (("[" * 2000 + "]" * 2000, 0), "bad_json"),  # nested past the recursion limit: json.loads raises RecursionError
    (('{"a":' * 2000 + "1" + "}" * 2000, 0), "bad_json"),
    (subprocess.TimeoutExpired(["claude", "-p", MARKER], 900), "timeout"),
    (FileNotFoundError(MARKER), "spawn_failed"),
]


@pytest.mark.parametrize("reply,problem", HARD, ids=["is_error", "exit_1", "text", "text_exit_1", "array", "deep_array",
                                                       "deep_object", "timeout", "no_claude"])
def test_a_hard_failure_fails_the_stage_with_a_short_reason(tmp_path, cfg, reply, problem):
    proc = Proc(reply, fake_run(good_script()))
    res = writer.run_stage(ctx(tmp_path, cfg), proc=proc)
    assert res == StageResult(ok=False, reason="writer_no_script:" + problem)
    assert len(proc.calls) == 1  # a hard failure is not a shape failure: no repair attempt
    assert not (tmp_path / "script.json").exists()
    run = run_json(tmp_path)
    assert [a["problem"] for a in run["attempts"]] == [problem] and all(k in run for k in CONTRACT_KEYS)
    assert MARKER not in (tmp_path / "writer-run.json").read_text()


def test_a_real_timeout_kills_the_call_and_fails_the_stage(tmp_path, cfg, monkeypatch):
    """The real runner, with a stand-in command that outlives a short timeout (no claude call)."""
    monkeypatch.setattr(writer, "TIMEOUT_S", 0.5)
    monkeypatch.setattr(writer, "writer_cmd", lambda *a: [sys.executable, "-c", "import time; time.sleep(30)"])
    res = writer.run_stage(ctx(tmp_path, cfg))
    assert res == StageResult(ok=False, reason="writer_no_script:timeout")


def test_run_proc_is_a_blocking_call_with_stdin_environment_and_directory(tmp_path):
    code = "import os, sys; print(sys.stdin.read() + '|' + os.getcwd() + '|' + os.environ['DAILY_X'])"
    rc, out, err = writer.run_proc([sys.executable, "-c", code], "hello", {**os.environ, "DAILY_X": "y"}, tmp_path)
    assert (rc, out.strip(), err) == (0, f"hello|{tmp_path.resolve()}|y", "")


CHILD = """
import os, subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
with open(sys.argv[1], "w") as fh:
    fh.write(f"{os.getpid()} {grandchild.pid}")
time.sleep(60)
"""


def alive(pid: int) -> bool:
    """Running (a zombie is dead for this purpose: init may be slow to reap it)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def test_a_timeout_kills_the_whole_process_group(tmp_path, monkeypatch):
    """The child starts its own sleeping grandchild; both must be gone once run_proc has raised."""
    monkeypatch.setattr(writer, "TIMEOUT_S", 2)
    pidfile = tmp_path / "pids"
    assert alive(os.getpid())  # positive control for the helper
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        writer.run_proc([sys.executable, "-c", CHILD, str(pidfile)], "", dict(os.environ), tmp_path)
    # the grandchild holds the child's output pipe, so a kill of the child alone leaves run_proc waiting for it
    assert time.monotonic() - started < 20
    child, grandchild = map(int, pidfile.read_text().split())
    assert child != grandchild
    deadline = time.monotonic() + 5
    while (alive(child) or alive(grandchild)) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(child) and not alive(grandchild)


# ---- the content repair: word budget and voice tells spend the same single repair ----

OVER = {"story.host": [words(60), words(60), words(60)]}  # 15 + 12 + 180 + 10 + 8 + 15 = 240 host words
UNDER = {"story.host": [words(10)]}  # 15 + 12 + 10 + 10 + 8 + 15 = 70 host words
TELLS = {"story.host": [words(45), "nobody " + words(44), words(45)], "out.host": words(14) + " catch"}  # still 195
# Sentences of 12 words: one with a person in it, and two with none (the lint's impersonal rule). The zqxmarker one's
# words must never go back to the model or into a record.
PERSONAL = "So we built the demo tool so a video makes itself today."
IMPERSONAL = "The tool reads every single line again and checks each recorded take."
SECRET = "The zqxmarker tool reads every single line again and checks each take."


FILLER = "We kept going all day. "  # five words: never one of the long sentences, and no tell


def voice_script(impersonal: int, sentence: str = IMPERSONAL, swap: tuple = ()):
    """A script with exactly 10 host sentences of 6 or more words (8 in four story lines, one each in steal_this and
    out), the first `impersonal` of them person-less, and short lines elsewhere: 171 host words. swap = (index, text)
    replaces one of the 10."""
    long = [sentence] * impersonal + [PERSONAL] * (10 - impersonal)
    if swap:
        long[swap[0]] = swap[1]
    return good_script(**{"cold_open.host": "We shipped it. " + FILLER * 3, "scoreboard.host": "I could barely keep up. "
                          + FILLER * 3,
                          "story.host": [" ".join(long[i:i + 2]) for i in range(0, 8, 2)],
                          "steal_this.host": long[8], "out.host": long[9],
                          "confessional.host_intro": "Codex wrote this. " + FILLER * 2})


def added(proc):
    """What the repair call added after the first prompt."""
    return proc.calls[1]["stdin"][len(proc.calls[0]["stdin"]):]


def test_an_in_budget_tell_free_draft_makes_no_repair(tmp_path, cfg):  # positive control
    proc = Proc(fake_run(good_script()), fake_run(good_script(title="never used")))
    res = writer.run_stage(ctx(tmp_path, cfg), proc=proc)
    assert res == StageResult(ok=True) and len(proc.calls) == 1
    run = run_json(tmp_path)
    assert run["repair_reason"] is None and run["lint"] == "ok" and run["written_attempt"] == 1
    assert run["attempts"][0]["host_words"] == 195 == G.host_words(good_script())
    assert run["attempts"][0]["tells"] == {}


@pytest.mark.parametrize("over,total", [(OVER, 240), (UNDER, 70)], ids=["over", "under"])
def test_a_draft_outside_the_word_budget_spends_the_repair_with_the_counts(tmp_path, cfg, over, total):
    draft = good_script(**over)
    assert G.host_words(draft) == total  # the gate's own counter
    proc = Proc(fake_run(draft), fake_run(good_script()))
    res = writer.run_stage(ctx(tmp_path, cfg), proc=proc)
    assert res == StageResult(ok=True) and len(proc.calls) == 2
    assert proc.calls[1]["stdin"].startswith(proc.calls[0]["stdin"])
    note = added(proc)
    assert f"{total} host words" in note and "165 to 210" in note and f"about {writer.WORDS_AIM}" in note
    story = sum(map(G.line_words, draft["story"]["host"]))
    assert f"story.host {story} of at most {writer.BEAT_MAX['story']}" in note
    assert f"cold_open.host 15 of at most {writer.BEAT_MAX['cold_open']}" in note
    assert f"scoreboard.host 12 of at most {writer.BEAT_MAX['scoreboard']}" in note
    assert f"steal_this.host 10 of at most {writer.BEAT_MAX['steal_this']}" in note
    assert f"confessional.host_intro 8 of at most {writer.BEAT_MAX['confessional']}" in note
    assert f"out.host 15 of at most {writer.BEAT_MAX['out']}" in note
    assert "Voice tells" not in note  # the draft has none
    run = run_json(tmp_path)
    assert run["repair_reason"] == f"word_budget:{total}" and run["written_attempt"] == 2
    assert [a["problem"] for a in run["attempts"]] == ["", ""]
    assert [a["host_words"] for a in run["attempts"]] == [total, 195]
    assert json.loads((tmp_path / "script.json").read_text()) == good_script()
    assert (tmp_path / "writer-prompt.txt").read_text() == proc.calls[1]["stdin"]


def test_a_voice_tell_spends_the_repair_naming_each_tell_by_beat(tmp_path, cfg):
    draft = good_script(**TELLS)
    assert G.host_words(draft) == 195  # in budget: only the tells call for the repair
    proc = Proc(fake_run(draft), fake_run(good_script()))
    assert writer.run_stage(ctx(tmp_path, cfg), proc=proc).ok and len(proc.calls) == 2
    note = added(proc)
    assert "story.host line 2: nobody" in note and "out.host: catch" in note
    assert "story.host line 1" not in note and "story.host line 3" not in note  # positive control: clean lines
    assert "195 host words" in note  # the counts ride along even when the budget is met
    run = run_json(tmp_path)
    assert run["repair_reason"] == "tells:nobody,writerly"
    assert run["attempts"][0]["tells"] == {"nobody": 1, "writerly": 1} and run["attempts"][1]["tells"] == {}


def test_budget_and_tells_together_are_one_repair_with_both_reasons(tmp_path, cfg):
    draft = good_script(**{**OVER, "out.host": words(14) + " catch"})
    proc = Proc(fake_run(draft), fake_run(good_script()))
    assert writer.run_stage(ctx(tmp_path, cfg), proc=proc).ok and len(proc.calls) == 2
    assert run_json(tmp_path)["repair_reason"] == "word_budget:240;tells:writerly"


def test_impersonal_sentences_are_named_by_beat_but_their_words_never_leave(tmp_path, cfg):
    draft = voice_script(4, SECRET)
    proc = Proc(fake_run(draft), fake_run(good_script()))
    assert writer.run_stage(ctx(tmp_path, cfg), proc=proc).ok and len(proc.calls) == 2
    note = added(proc)
    assert "story.host line 1: impersonal" in note and "story.host line 2: impersonal" in note
    assert "story.host line 3" not in note  # positive control: a line with no tell is not named
    assert "zqxmarker" not in note and "zqxmarker" not in (tmp_path / "writer-run.json").read_text()
    assert run_json(tmp_path)["repair_reason"] == "tells:impersonal"


# ---- the impersonal rule triggers only above a real speaker's rate ----

def test_voice_script_has_ten_long_sentences_and_stays_in_budget():  # the fixture the threshold tests rely on
    lint = writer.load_lint()
    for n in (0, 2, 3, 4):
        s = voice_script(n)
        lines = [line for lines in G.host_beats(s).values() for line in lines if line]
        assert sum(map(lint.long_sentences, lines)) == 10 and G.host_words(s) == 171
        assert sum(rule == "impersonal" for line in lines for rule, _ in lint.tells(line)) == n


@pytest.mark.parametrize("n", [0, 2, 3], ids=["none", "two_of_ten", "three_of_ten"])
def test_impersonal_sentences_at_or_under_thirty_percent_make_no_repair(tmp_path, cfg, n):
    proc = Proc(fake_run(voice_script(n)), fake_run(good_script()))
    assert writer.run_stage(ctx(tmp_path, cfg), proc=proc) == StageResult(ok=True) and len(proc.calls) == 1
    run = run_json(tmp_path)
    assert run["repair_reason"] is None and run["attempts"][0]["long_sentences"] == 10
    assert run["attempts"][0]["tells"] == ({"impersonal": n} if n else {})  # still counted for the record


def test_impersonal_sentences_over_thirty_percent_spend_the_repair_with_the_share(tmp_path, cfg):
    proc = Proc(fake_run(voice_script(4)), fake_run(good_script()))
    assert writer.run_stage(ctx(tmp_path, cfg), proc=proc).ok and len(proc.calls) == 2
    note = added(proc)
    assert "4 of 10 host sentences of six words or more have no I, you or we" in note and "30%" in note
    run = run_json(tmp_path)
    assert run["repair_reason"] == "tells:impersonal" and run["attempts"][0]["tells"] == {"impersonal": 4}


def test_a_word_tell_always_spends_the_repair(tmp_path, cfg):
    nobody = "So nobody built the demo tool and we made a video today."
    draft = voice_script(0, swap=(3, nobody))
    proc = Proc(fake_run(draft), fake_run(good_script()))
    assert writer.run_stage(ctx(tmp_path, cfg), proc=proc).ok and len(proc.calls) == 2
    note = added(proc)
    assert "story.host line 2: nobody" in note and "impersonal" not in note
    assert run_json(tmp_path)["repair_reason"] == "tells:nobody"


def test_a_word_tell_with_impersonal_under_the_share_lists_only_the_word_tell(tmp_path, cfg):
    nobody = "So nobody built the demo tool and we made a video today."
    proc = Proc(fake_run(voice_script(2, swap=(9, nobody))), fake_run(good_script()))
    assert writer.run_stage(ctx(tmp_path, cfg), proc=proc).ok and len(proc.calls) == 2
    assert "impersonal" not in added(proc) and "out.host: nobody" in added(proc)
    run = run_json(tmp_path)
    assert run["repair_reason"] == "tells:nobody" and run["attempts"][0]["tells"] == {"impersonal": 2, "nobody": 1}


def test_never_more_than_two_attempts_and_a_repair_still_over_budget_is_written_anyway(tmp_path, cfg):
    first, second = good_script(**OVER), good_script(**OVER, title="The second draft")
    proc = Proc(fake_run(first), fake_run(second), fake_run(good_script()))
    res = writer.run_stage(ctx(tmp_path, cfg), proc=proc)
    assert res == StageResult(ok=True) and len(proc.calls) == 2  # never a third call
    assert json.loads((tmp_path / "script.json").read_text()) == second  # the gate fails it: fail closed
    run = run_json(tmp_path)
    assert run["repair_reason"] == "word_budget:240" and run["written_attempt"] == 2 and len(run["attempts"]) == 2


def test_a_shape_repair_is_the_one_repair_so_an_over_budget_reply_after_it_is_written(tmp_path, cfg):
    proc = Proc(fake_run(good_script(title="x" * 80)), fake_run(good_script(**OVER)), fake_run(good_script()))
    assert writer.run_stage(ctx(tmp_path, cfg), proc=proc).ok and len(proc.calls) == 2
    assert json.loads((tmp_path / "script.json").read_text()) == good_script(**OVER)
    assert run_json(tmp_path)["repair_reason"] == "invalid:too_long:title"


def test_a_tells_only_repair_that_breaks_the_budget_keeps_the_first_draft(tmp_path, cfg):
    """The lint is advisory: a repair spent on tells never trades a draft the gate can pass for one it cannot."""
    draft = good_script(**TELLS)
    proc = Proc(fake_run(draft), fake_run(good_script(**OVER)))
    assert writer.run_stage(ctx(tmp_path, cfg), proc=proc) == StageResult(ok=True)
    assert json.loads((tmp_path / "script.json").read_text()) == draft
    assert run_json(tmp_path)["written_attempt"] == 1


def test_a_tell_left_after_the_repair_is_accepted(tmp_path, cfg):
    second = good_script(**{"out.host": words(14) + " catch"})
    proc = Proc(fake_run(good_script(**TELLS)), fake_run(second))
    assert writer.run_stage(ctx(tmp_path, cfg), proc=proc) == StageResult(ok=True)
    assert json.loads((tmp_path / "script.json").read_text()) == second


@pytest.mark.parametrize("reply", [subprocess.TimeoutExpired(["claude"], 900), fake_run(good_script(title="x" * 80)),
                                   fake_run(quiet_script("nothing after all"))], ids=["timeout", "invalid", "quiet"])
def test_a_content_repair_that_brings_back_no_usable_script_keeps_the_first_draft(tmp_path, cfg, reply):
    draft = good_script(**OVER)
    proc = Proc(fake_run(draft), reply)
    assert writer.run_stage(ctx(tmp_path, cfg), proc=proc) == StageResult(ok=True) and len(proc.calls) == 2
    assert json.loads((tmp_path / "script.json").read_text()) == draft  # the gate still judges it
    run = run_json(tmp_path)
    assert run["written_attempt"] == 1 and run["repair_reason"] == "word_budget:240"


def test_without_the_lint_the_budget_still_repairs_and_tells_do_not(tmp_path, cfg):
    proc = Proc(fake_run(good_script(**TELLS)), fake_run(good_script()))
    assert writer.run_stage(ctx(tmp_path, cfg), proc=proc, lint_loader=lambda: None).ok and len(proc.calls) == 1
    run = run_json(tmp_path)
    assert run["lint"] == "unavailable" and run["attempts"][0]["tells"] is None and run["repair_reason"] is None
    proc = Proc(fake_run(good_script(**OVER)), fake_run(good_script()))
    assert writer.run_stage(ctx(tmp_path, cfg), proc=proc, lint_loader=lambda: None).ok and len(proc.calls) == 2
    assert run_json(tmp_path)["repair_reason"] == "word_budget:240"


def test_a_lint_that_crashes_on_a_line_is_skipped_like_a_missing_one(tmp_path, cfg):
    def broken(line):
        raise RuntimeError("lint broke")
    real = writer.load_lint()
    for lint in (writer.Lint(broken, real.long_sentences), writer.Lint(real.tells, broken)):
        proc = Proc(fake_run(good_script(**TELLS)), fake_run(good_script()))
        assert writer.run_stage(ctx(tmp_path, cfg), proc=proc, lint_loader=lambda: lint).ok and len(proc.calls) == 1
        run = run_json(tmp_path)
        assert run["attempts"][0]["tells"] is None and run["attempts"][0]["long_sentences"] is None
        assert run["lint"] == "error"


# ---- the lint ----

def test_the_built_in_lint_finds_the_word_tells_and_counts_long_sentences():
    lint = writer.load_lint()
    assert lint.tells("So nobody saw it coming, I think.") == [("nobody", "nobody")]
    assert lint.tells("And no one minded, so we shipped it.") == [("nobody", "no one")]
    assert lint.tells("We built the demo tool so a video makes itself.") == []  # positive control
    assert lint.long_sentences("We shipped it. So we built the demo tool for it. The tool reads each line twice.") == 2


def test_the_lint_returns_only_its_fixed_words_never_a_sentence():
    lint = writer.load_lint()
    assert lint.tells("The zqxmarker tool reads every single line again.") == [("impersonal", "")]
    assert lint.tells("I was being clever about it.") == [("writerly", "clever")]


def test_the_impersonal_share_is_a_measured_rate_rounded_up():
    assert writer.IMPERSONAL_SHARE_MAX == 0.30


def test_the_settings_avoid_words_drive_the_lint_and_the_repair(tmp_path, cfg):
    mine = dataclasses.replace(SETTINGS, avoid_words=("demo",))  # "demo" is in every fixture line
    proc = Proc(fake_run(good_script()), fake_run(good_script()))
    assert writer.run_stage(ctx(tmp_path, cfg, settings=mine), proc=proc).ok and len(proc.calls) == 2
    assert run_json(tmp_path)["repair_reason"] == "tells:writerly" and ": demo" in added(proc)
    assert writer.load_lint(("demo",)).tells("I fixed the demo.") == [("writerly", "demo")]
    assert writer.load_lint(()).tells("That was clever of me.") == []  # an empty list avoids nothing


def test_without_settings_the_stage_fails_closed_before_any_call(tmp_path, cfg):
    proc = Proc(fake_run(good_script()))
    res = writer.run_stage(ctx(tmp_path, cfg, settings=None), proc=proc)
    assert res == StageResult(ok=False, reason="settings_missing") and proc.calls == []
    assert not (tmp_path / "script.json").exists() and not (tmp_path / "writer-run.json").exists()


# ---- a reply too deep or too large never reaches json.loads (pipeline finding #1) ----

def deep_reply(n):
    return '{"is_error": false, "' + writer.RESULT_FIELD + '": ' + '{"a": ' * n + "1" + "}" * n + "}"


@pytest.mark.parametrize("stdout", [deep_reply(2000), deep_reply(64), "[" * 66 + "]" * 66,
                                    '{"pad": "' + "x" * (2 * 1024 * 1024) + '"}'],
                         ids=["2000_dicts", "65_deep", "66_arrays", "over_2mb"])
def test_a_deep_or_huge_reply_is_bad_json_without_parsing_or_repair(tmp_path, cfg, monkeypatch, stdout):
    real_loads = json.loads

    def loads(text, *a, **kw):
        assert text is not stdout, "json.loads must not see a reply the scan refuses"
        return real_loads(text, *a, **kw)
    monkeypatch.setattr(writer.json, "loads", loads)
    proc = Proc((stdout, 0), fake_run(good_script()))
    res = writer.run_stage(ctx(tmp_path, cfg), proc=proc)
    assert res == StageResult(ok=False, reason="writer_no_script:bad_json")
    assert len(proc.calls) == 1 and len(run_json(tmp_path)["attempts"]) == 1
    assert not (tmp_path / "script.json").exists()


def test_a_normal_reply_with_brackets_inside_strings_is_parsed(tmp_path, cfg):  # positive control
    title = '[[[{{{ "quoted" \\ end'
    res = writer.run_stage(ctx(tmp_path, cfg), proc=Proc(fake_run(good_script(title=title))))
    assert res.ok and not res.quiet and json.loads((tmp_path / "script.json").read_text())["title"] == title


@pytest.mark.parametrize("text,deep", [("{" * 64 + "}" * 64, False), ("{" * 65 + "}" * 65, True),
                                       ('{"a": "' + "{" * 100 + '"}', False), ('{"a": "\\\\", ' + "[" * 65, True),
                                       ('{"a": "x"}' + "[" * 10, False), ('"\\\"' + "{" * 70 + '"', False)],
                         ids=["64", "65", "in_string", "escaped_backslash_ends_string", "shallow", "escaped_quote"])
def test_the_depth_scan_counts_only_brackets_outside_strings(text, deep):
    assert writer.too_deep(text) is deep
