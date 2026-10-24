"""The example timer fires at 05:30 local time and catches up once after a missed run; the service is a oneshot that
reports failures, runs the due day in the foreground through an entry script with an explicit PATH that refuses to
start without node, and its timeout covers every stage's own. The files are only read here: nothing is installed or
enabled."""
import datetime as dt
import json
import os
import shutil
import subprocess

from daily import align, day, paths, publish, render, tts_remote, voice, writer

HERE = paths.REPO / "systemd"
SYSTEM_DIRS = "/usr/local/bin:/usr/bin:/bin"


def unit(path):
    out, sec = {}, None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            sec = out.setdefault(line[1:-1], {})
            continue
        k, _, v = line.partition("=")
        sec[k.strip()] = v.strip()
    return out


def test_timer():
    t = unit(HERE / "orion-on-the-clock.timer")
    assert t["Timer"]["OnCalendar"] == "*-*-* 05:30:00"
    assert t["Timer"]["Persistent"] == "true"
    assert t["Install"]["WantedBy"] == "timers.target"


def test_the_timer_time_is_the_time_the_due_day_counts_from():
    assert day.RUN_DUE == dt.time(5, 30)
    assert unit(HERE / "orion-on-the-clock.timer")["Timer"]["OnCalendar"].endswith(day.RUN_DUE.strftime("%H:%M:%S"))


def test_service():
    s = unit(HERE / "orion-on-the-clock.service")
    assert s["Unit"]["OnFailure"] == "orion-on-the-clock-failed.service"
    assert s["Service"]["Type"] == "oneshot"
    assert s["Service"]["ExecStart"].startswith("%h/") and s["Service"]["ExecStart"].endswith(
        "systemd/orion-on-the-clock-entry.sh")
    assert s["Service"]["SuccessExitStatus"] == "3 5"   # 3 lock held, 5 failed and reported by the notify stage


def worst_case_seconds():
    """Every stage at its own timeout, summed from the module constants: the writer (two attempts), one remote voicing
    attempt (preflight twice, two copies, the run with its kill margin and its 60 s step, the fetch, the prune) before
    the local fallback, local TTS, Whisper, the render (check, render, remux, four probes) and the upload child plus
    its release."""
    remote = (2 * tts_remote.PREFLIGHT_TIMEOUT_S + 2 * tts_remote.COPY_TIMEOUT_S + tts_remote.RUN_TIMEOUT_S
              + tts_remote.KILL_AFTER_S + 60 + tts_remote.COPY_TIMEOUT_S + tts_remote.PRUNE_TIMEOUT_S)
    rendering = render.CHECK_TIMEOUT + render.RENDER_TIMEOUT + render.REMUX_TIMEOUT + 4 * render.PROBE_TIMEOUT
    return (2 * writer.TIMEOUT_S + remote + voice.TTS_TIMEOUT_S + align.WHISPER_TIMEOUT_S + rendering
            + publish.UPLOAD_TIMEOUT_S + publish.RELEASE_TIMEOUT_S)


def test_timeout_covers_worst_case_from_module_constants():
    s = unit(HERE / "orion-on-the-clock.service")
    need = worst_case_seconds() + 1800
    assert worst_case_seconds() > 29000   # positive control: the sum really counts every stage
    assert int(s["Service"]["TimeoutStartSec"]) >= need
    assert s["Service"]["TimeoutStartSec"] == "32400"


def test_stop_is_graceful():
    s = unit(HERE / "orion-on-the-clock.service")["Service"]
    assert s["KillSignal"] == "SIGTERM"
    assert int(s["TimeoutStopSec"]) >= 300


def test_entry_script():
    p = HERE / "orion-on-the-clock-entry.sh"
    src = p.read_text()
    assert os.access(p, os.X_OK)
    assert "exec python3 -u -m daily.run --day due" in src
    assert "$HOME/.local/bin" in src and ".nvm/versions/node" in src
    assert "&" not in src.replace("&&", "")   # nothing is put in the background
    assert "unset DISPLAY WAYLAND_DISPLAY" in src
    assert "ORION_CLOCK_CONFIG=" not in src and "ORION_CLOCK_STATE=" not in src
    assert src.index("exit 1") < src.index("exec python3")
    assert "HYPERFRAMES_BROWSER_PATH" in src


def test_scripts_syntax_and_shellcheck():
    for name in ("orion-on-the-clock-entry.sh", "orion-on-the-clock-failed.sh"):
        p = str(HERE / name)
        assert subprocess.run(["bash", "-n", p]).returncode == 0, name
        if shutil.which("shellcheck"):
            assert subprocess.run(["shellcheck", p]).returncode == 0, name


def test_entry_script_fails_closed_without_node(tmp_path):
    """Positive control: a HOME with no nvm node and no node in the system folders exits non-zero with a message and
    never reaches the pipeline. The script runs as a copy whose system folders are an empty one, so a node installed
    on this machine cannot hide the refusal (that path uses only shell builtins)."""
    src = (HERE / "orion-on-the-clock-entry.sh").read_text()
    assert src.count(SYSTEM_DIRS) == 1
    empty = tmp_path / "sysbin"
    empty.mkdir()
    copy = tmp_path / "entry.sh"
    copy.write_text(src.replace(SYSTEM_DIRS, str(empty)))
    r = subprocess.run([shutil.which("bash"), str(copy)], env={"HOME": str(tmp_path / "home"), "PATH": str(empty)},
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "no node on PATH" in r.stderr
    assert "daily.run" not in r.stderr and "Traceback" not in r.stderr


def test_entry_script_unsets_display_and_reaches_run(tmp_path):
    """With a fake nvm node and a fake python3 on PATH, the script clears the display vars and execs the pipeline from
    the repository root."""
    node_bin = tmp_path / ".nvm/versions/node/v1.0.0/bin"
    node_bin.mkdir(parents=True)
    (node_bin / "node").write_text("#!/bin/sh\nexit 0\n")
    (node_bin / "node").chmod(0o755)
    fake = tmp_path / ".local/bin"
    fake.mkdir(parents=True)
    py = fake / "python3"
    py.write_text('#!/bin/sh\necho "ARGS:$*"\necho "CWD:$(pwd)"\n'
                  'echo "DISPLAY=[${DISPLAY-unset}] WAYLAND=[${WAYLAND_DISPLAY-unset}]"\necho "PATH:$PATH"\n')
    py.chmod(0o755)
    r = subprocess.run(["bash", str(HERE / "orion-on-the-clock-entry.sh")],
                       env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "DISPLAY": ":0", "WAYLAND_DISPLAY": "w-0"},
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "ARGS:-u -m daily.run --day due" in r.stdout
    assert f"CWD:{paths.REPO}" in r.stdout
    assert "DISPLAY=[unset] WAYLAND=[unset]" in r.stdout
    path = next(line for line in r.stdout.splitlines() if line.startswith("PATH:"))[5:].split(":")
    assert path[:2] == [str(fake), str(node_bin)] and path[-3:] == SYSTEM_DIRS.split(":")


def test_failed_unit_and_script():
    u = unit(HERE / "orion-on-the-clock-failed.service")
    assert u["Service"]["Type"] == "oneshot"
    assert u["Service"]["ExecStart"].endswith("systemd/orion-on-the-clock-failed.sh")
    assert "OnFailure" not in u.get("Unit", {})
    p = HERE / "orion-on-the-clock-failed.sh"
    assert os.access(p, os.X_OK)
    assert "daily/unit_failed.py" in p.read_text()


def test_the_unit_name_matches_the_failure_items_pointer():
    from daily import unit_failed
    assert unit_failed.UNIT == "orion-on-the-clock" and (HERE / f"{unit_failed.UNIT}.service").is_file()


def _run_failed(tmp_path, hook_rc):
    """Run the failure script for real with a settings file whose notify command is a stub that records its stdin."""
    cfg, state = tmp_path / "cfg", tmp_path / "state"
    cfg.mkdir()
    state.mkdir()
    hook = tmp_path / "hook.sh"
    hook.write_text('#!/bin/sh\ncat > "$HOOK_OUT"\necho \'{"item_id": "i-0123456789ab", "state": "open"}\'\n'
                    f"exit {hook_rc}\n")
    hook.chmod(0o755)
    (cfg / "settings.toml").write_text(
        'timezone = "America/Los_Angeles"\n\n[show]\nseries = "Demo"\nhost_name = "Sam"\ndisclosure = "Synthetic."\n\n'
        f'[sources]\ncodex = false\ngithub = false\n\n[notify]\ncommand = ["{hook}"]\n')
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "ORION_CLOCK_CONFIG": str(cfg),
           "ORION_CLOCK_STATE": str(state), "HOOK_OUT": str(tmp_path / "message.json")}
    r = subprocess.run(["bash", str(HERE / "orion-on-the-clock-failed.sh")], env=env, capture_output=True, text=True)
    return r, tmp_path / "message.json", state


def test_failed_script_sends_the_item_through_the_notify_command(tmp_path):
    r, message, state = _run_failed(tmp_path, 0)
    assert r.returncode == 0, r.stderr
    msg = json.loads(message.read_text())
    assert msg["op"] == "add" and msg["item"]["kind"] == "approval" and msg["item"]["outcome"] == "unit-failed"
    assert msg["item"]["key"].startswith("daily:unit-failed:")
    assert msg["item"]["title"].endswith("[day guessed from the clock: no usable last-run record]")   # no record here
    day_dirs = [p for p in state.iterdir() if p.is_dir()]
    assert len(day_dirs) == 1 and json.loads((day_dirs[0] / "notify.json").read_text())["unit-failed"]["item_id"] == \
        "i-0123456789ab"


def test_failed_script_propagates_a_failed_send(tmp_path):
    r, _, _ = _run_failed(tmp_path, 7)
    assert r.returncode != 0
