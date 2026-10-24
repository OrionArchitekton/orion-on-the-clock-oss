"""The documented entry point, as a user runs it: `python3 -m daily.run`, in a child process, against a synthetic config
folder and a synthetic Claude Code log. The only stand-in is a fake `claude` executable first on PATH, so the real
collect, write and gate stages run end to end (real transcript reader, real git lookup, built-in text checks) with no
network and no model call."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from daily import ledger, paths
from daily.sources.transcripts import note_ref
from daily_helpers import good_script

DAY = "2026-10-16"
SAID = "The demo tool now records its own walkthrough video from a short script."
SESSION = "11111111-2222-4333-8444-555555555555"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git for the repo lookup")


def run_cli(args, env, cwd=None):
    return subprocess.run([sys.executable, "-m", "daily.run", *args], env=env, cwd=cwd or paths.REPO,
                          capture_output=True, text=True, timeout=120)


def test_help_works_without_any_config(tmp_path):
    env = {**os.environ, "ORION_CLOCK_CONFIG": str(tmp_path / "none"), "ORION_CLOCK_STATE": str(tmp_path / "s")}
    done = run_cli(["--help"], env)
    assert done.returncode == 0 and "--day" in done.stdout and "--dry" in done.stdout and "notify" in done.stdout
    assert not (tmp_path / "s").exists()


def test_without_settings_the_cli_says_so_and_exits_1(tmp_path):
    env = {**os.environ, "ORION_CLOCK_CONFIG": str(tmp_path / "none"), "ORION_CLOCK_STATE": str(tmp_path / "s")}
    done = run_cli(["--day", DAY, "--until", "gate", "--dry"], env)
    assert done.returncode == 1 and done.stderr.startswith("settings error: settings_missing")
    assert not (tmp_path / "s").exists()


def world(tmp_path: Path) -> dict:
    """A config folder, a git repo for the public lane, one Claude Code session that worked in it on the day, and a
    fake `claude` that answers with a script built for exactly that session."""
    repo = tmp_path / "src" / "demo-tool"
    repo.mkdir(parents=True)
    git = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(tmp_path)}
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=git)
    subprocess.run(["git", "-C", str(repo), "config", "remote.origin.url", "https://github.com/owner/demo-tool.git"],
                   check=True, env=git)
    projects = tmp_path / "projects"
    log = projects / "-src-demo-tool" / f"{SESSION}.jsonl"
    log.parent.mkdir(parents=True)
    rows = [
        {"type": "ai-title", "aiTitle": "Recording the demo walkthrough", "sessionId": SESSION},
        {"type": "user", "timestamp": "2026-10-16T20:00:00.000Z", "sessionId": SESSION, "cwd": str(repo),
         "uuid": "u1", "message": {"role": "user", "content": [{"type": "text", "text": "Can it record itself?"}]}},
        {"type": "assistant", "timestamp": "2026-10-16T20:00:09.000Z", "sessionId": SESSION, "cwd": str(repo),
         "uuid": "u2", "message": {"role": "assistant", "content": [{"type": "text", "text": f"Done. {SAID}"}]}},
    ]
    log.write_text("".join(json.dumps(r) + "\n" for r in rows))
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "settings.toml").write_text(f'''timezone = "America/Los_Angeles"
[show]
series = "Demo Agents on the Clock"
host_name = "Sam"
disclosure = "Narrated with an AI voice; agent lines are verbatim from logs."
[sources]
codex = false
github = false
claude_projects = "{projects}"
''')
    (cfg / "lanes.json").write_text(json.dumps({"lanes": {"demo-tool": {
        "disclosure": "public", "label": "the demo tool", "why": "so every demo runs itself"}},
        "hard_exclude": ["secret-garden"], "blocked_names": ["Zephyr"]}))
    (cfg / "embargo.json").write_text(json.dumps({"active": False, "terms": []}))
    qid = ledger.quote_id("claude_code", SAID, str(log), 3)
    script = good_script(qid, story_item_id=f"session:{note_ref(SESSION)}")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "script.json").write_text(json.dumps(script))
    fake = bin_dir / "claude"
    fake.write_text(f"""#!{sys.executable}
import json, pathlib, sys
prompt = sys.stdin.read()
assert "<ledger>" in prompt and {SAID!r} in prompt, "the ledger never reached the writer"
assert sys.argv[sys.argv.index("--tools") + 1] == "", "tools must be off"
script = json.loads((pathlib.Path(__file__).parent / "script.json").read_text())
print(json.dumps({{"is_error": False, "session_id": "s1", "num_turns": 2, "total_cost_usd": 0.0, "usage": {{}},
                  "structured_output": script}}))
""")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", "ORION_CLOCK_CONFIG": str(cfg),
           "ORION_CLOCK_STATE": str(tmp_path / "state"), "PYTHONPATH": str(paths.REPO)}  # run from anywhere
    return {"env": env, "state": tmp_path / "state"}


def test_a_dry_run_through_the_gate_passes_on_synthetic_logs(tmp_path):
    w = world(tmp_path)
    done = run_cli(["--day", DAY, "--until", "gate", "--dry"], w["env"], cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "" and done.stderr == ""  # the terminal stays quiet
    dry = w["state"] / f"{DAY}-dry"
    status = json.loads((dry / "status.json").read_text())["stages"]
    assert {k: v["state"] for k, v in status.items()} == {"collect": "dry", "write": "dry", "gate": "dry"}
    led = json.loads((dry / "ledger.json").read_text())
    assert led["coverage"] == {"claude_code": "ok"} and led["scoreboard"]["sessions"] == {"claude_code": 1}
    assert [q["text"] for q in led["quotes"]] == [SAID] and led["quotes"][0]["lane"] == "demo-tool"
    verdict = json.loads((dry / "verdict.json").read_text())
    assert verdict["pass"] is True and verdict["dry"] is True, [c for c in verdict["checks"] if not c["pass"]]
    evidence = {c["name"]: c["evidence"] for c in verdict["checks"]}
    assert evidence["quote_verbatim"] == f"matched {SESSION}.jsonl:3" and evidence["redactor"] == "PUBLISH"
    assert not (w["state"] / DAY).exists()  # the real day was never touched


def test_a_full_dry_run_without_a_voices_file_stops_closed_at_the_voice_stage(tmp_path):
    w = world(tmp_path)
    done = run_cli(["--day", DAY, "--dry"], w["env"], cwd=tmp_path)
    assert done.returncode == 1
    status = json.loads((w["state"] / f"{DAY}-dry" / "status.json").read_text())["stages"]
    assert status["gate"]["state"] == "dry"
    assert (status["voice"]["state"], status["voice"]["reason"]) == ("failed", "voice_config:voices_file_unreadable")
    assert (status["notify"]["state"], status["notify"]["reason"]) == ("dry", "dry run: nothing sent")
    assert "render" not in status
