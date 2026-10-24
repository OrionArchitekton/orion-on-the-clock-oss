"""The orchestrator: stage order from paths.STAGE_ORDER, resumable status.json,
a kernel (flock) lock, quiet and dry runs, notify after every outcome, and reasons that are sanitized before they are written.
Fake stage modules stand in through the load_stage seam; the subprocess tests use fake stages or stop before any."""
import datetime as dt
import fcntl
import importlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from daily import ledger, paths, run
from daily.config import Config, ConfigError, load_config
from daily.stage import Ctx, StageResult

TODAY = dt.date(2026, 10, 17)
DAY = dt.date(2026, 10, 16)
RUN_PY = Path(run.__file__).resolve()


@pytest.fixture
def env(monkeypatch, tmp_path, cfg_dir):
    monkeypatch.setenv("ORION_CLOCK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(cfg_dir))
    calls, behaviour, ctxs = [], {}, {}

    def make(name):
        def run_stage(ctx):
            calls.append(name)
            ctxs[name] = ctx
            b = behaviour.get(name)
            if callable(b):
                return b(ctx)
            if isinstance(b, BaseException):
                raise b
            return b or StageResult(ok=True)
        return SimpleNamespace(run_stage=run_stage)

    installed = {"daily.collect": make("collect"), "daily.writer": make("write"), "daily.gate": make("gate")}

    def loader(module_name):
        mod = installed.get(module_name)
        if isinstance(mod, BaseException):
            raise mod  # a stage module that fails while it is imported
        return mod

    monkeypatch.setattr(run, "load_stage", loader)
    return SimpleNamespace(calls=calls, behaviour=behaviour, ctxs=ctxs, installed=installed, make=make,
                           work=tmp_path / "state" / "2026-10-16", dry_work=tmp_path / "state" / "2026-10-16-dry",
                           tmp=tmp_path, cfg_dir=cfg_dir)


def go(*extra):
    return run.main(["--day", "yesterday", *extra], today=TODAY)


def status_text(env, dry=False):
    return ((env.dry_work if dry else env.work) / "status.json").read_text(encoding="utf-8")


def stages(env, dry=False):
    data = json.loads(status_text(env, dry))
    assert data["day"] == "2026-10-16"
    return {k: (v["state"], v["reason"]) for k, v in data["stages"].items()}


def settings_only(env):
    """A config folder with valid settings and nothing else: lanes.json and embargo.json are missing."""
    d = env.tmp / "empty"
    d.mkdir()
    (d / "settings.toml").write_text((env.cfg_dir / "settings.toml").read_text())
    return d


# --- the task's behaviours -----------------------------------------------------------------------------------------

def test_runs_until_gate_in_order(env):
    assert go("--until", "gate") == 0
    assert env.calls == ["collect", "write", "gate"]
    assert stages(env) == {"collect": ("done", ""), "write": ("done", ""), "gate": ("done", "")}
    assert lock_is_free(env.work / "run.lock")


def test_every_stage_gets_the_day_the_work_dir_the_config_and_the_dry_flag(env):
    assert go("--until", "gate") == 0
    ctx = env.ctxs["gate"]
    assert isinstance(ctx, Ctx)
    assert (ctx.day, ctx.work, ctx.dry) == (DAY, env.work, False)
    assert isinstance(ctx.cfg, Config) and "demo-tool" in ctx.cfg.lanes
    assert env.ctxs["collect"].cfg is ctx.cfg  # one load per run


def test_status_records_a_utc_timestamp_per_stage(env):
    go("--until", "collect")
    at = json.loads(status_text(env))["stages"]["collect"]["at"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", at)


def test_rerun_skips_done_stages(env):
    go("--until", "gate")
    go("--until", "gate")
    assert env.calls == ["collect", "write", "gate"]


def test_rerun_picks_up_where_it_stopped(env):
    assert go("--until", "write") == 0
    assert env.calls == ["collect", "write"]
    assert go("--until", "gate") == 0
    assert env.calls == ["collect", "write", "gate"]


def test_a_dry_run_is_recorded_dry_and_a_real_run_redoes_it(env):
    assert go("--until", "gate", "--dry") == 0
    assert all(c.dry for c in env.ctxs.values())
    assert all(c.work == env.dry_work for c in env.ctxs.values())  # a dry run works in its own directory
    assert stages(env, dry=True) == {"collect": ("dry", ""), "write": ("dry", ""), "gate": ("dry", "")}
    assert not env.work.exists()  # and the real day's directory is never even created
    assert go("--until", "gate") == 0
    assert env.calls == ["collect", "write", "gate", "collect", "write", "gate"]
    assert stages(env) == {"collect": ("done", ""), "write": ("done", ""), "gate": ("done", "")}
    assert stages(env, dry=True)["gate"] == ("dry", "")  # the real run left the dry folder alone


def test_a_dry_run_after_a_real_one_leaves_the_real_status_done(env):
    go("--until", "gate")
    real_status = status_text(env)
    env.calls.clear()
    assert go("--until", "gate", "--dry") == 0
    assert env.calls == ["collect", "write", "gate"]  # the dry folder has its own status: nothing there is done yet
    assert stages(env)["gate"] == ("done", "") and status_text(env) == real_status  # the real one is untouched


def test_from_stage_reruns_that_stage_and_later(env):
    go("--until", "gate")
    env.calls.clear()
    assert go("--until", "gate", "--from-stage", "write") == 0
    assert env.calls == ["write", "gate"]


def test_from_stage_forgets_a_quiet_day_so_it_can_be_redone(env):
    env.behaviour["write"] = StageResult(ok=True, reason="no_items", quiet=True)
    go("--until", "gate")
    env.behaviour.clear()
    env.calls.clear()
    assert go("--until", "gate", "--from-stage", "write") == 0
    assert env.calls == ["write", "gate"]
    assert stages(env) == {"collect": ("done", ""), "write": ("done", ""), "gate": ("done", "")}


def test_quiet_stops_the_run_and_stays_quiet(env):
    env.behaviour["write"] = StageResult(ok=True, reason="no_items", quiet=True)
    assert go("--until", "gate") == 0
    assert env.calls == ["collect", "write"]
    assert stages(env)["write"] == ("quiet", "no_items")
    assert go("--until", "gate") == 0
    assert env.calls == ["collect", "write"]


def test_a_dry_quiet_day_is_recorded_dry_so_a_real_run_decides_again(env):
    env.behaviour["write"] = StageResult(ok=True, reason="no_items", quiet=True)
    assert go("--until", "gate", "--dry") == 0
    assert env.calls == ["collect", "write"]
    assert stages(env, dry=True)["write"] == ("dry", "no_items")
    env.behaviour.clear()
    assert go("--until", "gate") == 0
    assert env.calls == ["collect", "write", "collect", "write", "gate"]


def test_failure_stops_the_run_and_retries_next_time(env):
    env.behaviour["collect"] = StageResult(ok=False, reason="boom")
    assert go("--until", "gate") == 1
    assert stages(env) == {"collect": ("failed", "boom")}
    env.behaviour.clear()
    assert go("--until", "gate") == 0
    assert env.calls == ["collect", "collect", "write", "gate"]


def test_a_later_failure_keeps_the_earlier_stages_done(env):
    env.behaviour["gate"] = StageResult(ok=False, reason="redactor")
    assert go("--until", "gate") == 1
    assert stages(env) == {"collect": ("done", ""), "write": ("done", ""), "gate": ("failed", "redactor")}
    env.behaviour.clear()
    env.calls.clear()
    assert go("--until", "gate") == 0
    assert env.calls == ["gate"]


def test_a_result_that_is_both_failed_and_quiet_is_a_failure(env):
    env.behaviour["write"] = StageResult(ok=False, reason="odd", quiet=True)
    assert go("--until", "gate") == 1
    assert stages(env)["write"] == ("failed", "odd")
    assert env.calls == ["collect", "write"]


def test_a_stage_that_returns_nothing_is_a_failed_stage(env):
    env.behaviour["write"] = lambda ctx: None
    assert go("--until", "gate") == 1
    assert stages(env)["write"] == ("failed", "bad_result")
    assert env.calls == ["collect", "write"]


def test_a_crashing_stage_is_a_failed_stage(env):
    env.behaviour["collect"] = RuntimeError("disk gone")
    assert go("--until", "gate") == 1
    assert stages(env)["collect"] == ("failed", "exception:RuntimeError")
    assert env.calls == ["collect"]


def test_a_stage_module_that_fails_to_import_is_a_failed_stage(env):
    env.installed["daily.writer"] = ImportError("cannot import name x from /home/someone/private/mod.py")
    assert go("--until", "gate") == 1
    assert stages(env) == {"collect": ("done", ""), "write": ("failed", "exception:ImportError")}
    assert "private" not in status_text(env)
    assert env.calls == ["collect"]


def test_a_missing_later_stage_fails_closed(env):
    assert go() == 1
    assert env.calls == ["collect", "write", "gate"]
    assert stages(env)["voice"] == ("skipped", "not_installed")
    assert stages(env)["notify"] == ("skipped", "not_installed")  # the notify module is missing too, and says so
    assert "render" not in stages(env)


def test_a_skipped_stage_is_tried_again_once_it_is_installed(env):
    assert go() == 1
    env.installed["daily.voice"] = env.make("voice")
    assert go("--until", "voice") == 0
    assert env.calls == ["collect", "write", "gate", "voice"]
    assert stages(env)["voice"] == ("done", "")


# --- notify ----------------------------------------------------------------------------------------------------------

def test_a_missing_ping_is_recorded_skipped_and_does_not_fail_a_done_run(env):
    for stage in ("voice", "render", "publish"):
        env.installed[f"daily.{stage}"] = env.make(stage)
    assert go() == 0  # a missing notify module is recorded, not a failure
    assert stages(env)["notify"] == ("skipped", "not_installed")
    assert stages(env)["publish"] == ("done", "")
    env.installed["daily.notify"] = env.make("notify")
    assert go() == 0  # control: once installed, the next run notifies
    assert stages(env)["notify"] == ("done", "")
    assert env.calls[-1] == "notify"


def test_ping_runs_even_after_a_failure(env):
    env.installed["daily.notify"] = env.make("notify")
    env.behaviour["collect"] = StageResult(ok=False, reason="boom")
    assert go() == 1
    assert env.calls == ["collect", "notify"]
    assert stages(env)["notify"] == ("done", "")


def test_ping_runs_after_a_quiet_day_and_after_a_clean_run(env):
    env.installed["daily.notify"] = env.make("notify")
    env.installed["daily.voice"] = env.make("voice")
    env.installed["daily.render"] = env.make("render")
    env.installed["daily.publish"] = env.make("publish")
    assert go() == 0
    assert env.calls == ["collect", "write", "gate", "voice", "render", "publish", "notify"]
    env.calls.clear()
    env.behaviour["write"] = StageResult(ok=True, reason="no_items", quiet=True)
    assert run.main(["--day", "2026-10-15"], today=TODAY) == 0  # another day is its own run, here a quiet one
    assert env.calls == ["collect", "write", "notify"]


def test_ping_sees_the_final_status_and_the_failure_reason(env):
    seen = {}

    def notify(ctx):
        seen.update(json.loads((ctx.work / "status.json").read_text()))
        return StageResult(ok=True)

    env.installed["daily.notify"] = env.make("notify")
    env.behaviour["notify"] = notify
    env.behaviour["write"] = StageResult(ok=False, reason="writer_no_script:invalid:schema")
    assert go() == 1
    assert seen["stages"]["write"]["state"] == "failed"
    assert seen["stages"]["write"]["reason"] == "writer_no_script:invalid:schema"


def test_ping_runs_again_on_every_invocation(env):
    env.installed["daily.notify"] = env.make("notify")
    env.installed["daily.voice"] = env.make("voice")
    env.installed["daily.render"] = env.make("render")
    env.installed["daily.publish"] = env.make("publish")
    go()
    go()
    assert env.calls.count("notify") == 2 and env.calls.count("collect") == 1


def test_ping_only_runs_when_the_run_goes_all_the_way_to_it(env):
    env.installed["daily.notify"] = env.make("notify")
    assert go("--until", "gate") == 0
    assert "notify" not in env.calls and "notify" not in stages(env)


def test_a_dry_ping_is_recorded_dry(env):
    env.installed["daily.notify"] = env.make("notify")
    env.behaviour["collect"] = StageResult(ok=False, reason="boom")
    assert go("--dry") == 1
    assert env.ctxs["notify"].dry is True
    assert stages(env, dry=True)["notify"] == ("dry", "")


def test_a_ping_that_fails_or_crashes_is_recorded_and_fails_the_run(env):
    env.installed["daily.notify"] = env.make("notify")
    for stage in ("voice", "render", "publish"):
        env.installed[f"daily.{stage}"] = env.make(stage)
    env.behaviour["notify"] = StageResult(ok=False, reason="inbox_unreachable")
    assert go() == 1
    assert stages(env)["notify"] == ("failed", "inbox_unreachable")
    env.behaviour["notify"] = OSError("cannot write /home/someone/.notify/inbox")
    assert go() == 1
    assert stages(env)["notify"] == ("failed", "exception:OSError")
    env.behaviour.clear()
    assert go() == 0
    assert stages(env)["notify"] == ("done", "")


# --- later stages are forgotten when an earlier one runs ------------------------------------------------------------

def seed_status(env, **states):
    env.work.mkdir(parents=True, exist_ok=True)
    stages_ = {k: {"state": v, "reason": "", "at": "2026-10-16T10:00:00Z"} for k, v in states.items()}
    (env.work / "status.json").write_text(json.dumps({"day": "2026-10-16", "stages": stages_}))


def test_running_a_stage_removes_every_later_entry_before_it_runs(env):
    seed_status(env, collect="failed", write="done", gate="done", notify="done")
    seen = {}

    def collect(ctx):
        seen["on_disk"] = sorted(json.loads((ctx.work / "status.json").read_text())["stages"])
        return StageResult(ok=True)

    env.behaviour["collect"] = collect
    assert go("--until", "gate") == 0
    assert seen["on_disk"] == ["collect"]  # write, gate and notify were gone from disk before collect started
    assert env.calls == ["collect", "write", "gate"]  # so write and gate were not skipped as done


def test_only_later_entries_go_earlier_ones_stay_done(env):
    seed_status(env, collect="done", write="failed", gate="done")
    assert go("--until", "gate") == 0
    assert env.calls == ["write", "gate"] and stages(env)["collect"] == ("done", "")


def test_a_run_cut_short_in_a_stage_leaves_no_later_entry_standing(env):
    seed_status(env, collect="failed", write="done", gate="done")
    env.behaviour["collect"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        go("--until", "gate")
    assert stages(env) == {"collect": ("failed", "")}


def test_a_config_error_then_a_fixed_config_redoes_every_stage_after_collect(env, monkeypatch):
    """The review's scenario: collect deletes what write and gate made, so after a config error write and gate must
    not be skipped as done."""
    def collect(ctx):
        (ctx.work / "script.json").unlink(missing_ok=True)  # what the real collect stage does to its downstream files
        return StageResult(ok=True)

    def write(ctx):
        (ctx.work / "script.json").write_text("{}")
        return StageResult(ok=True)

    env.behaviour["collect"], env.behaviour["write"] = collect, write
    empty = settings_only(env)
    assert go("--until", "gate") == 0
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(empty))
    assert go("--until", "gate") == 1
    assert stages(env) == {"collect": ("failed", "config:config_missing")}  # write and gate are no longer "done"
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(env.cfg_dir))
    assert go("--until", "gate") == 0
    assert env.calls == ["collect", "write", "gate"] * 2
    assert (env.work / "script.json").exists()


# --- config --------------------------------------------------------------------------------------------------------

def test_missing_config_fails_collect(env, monkeypatch):
    empty = settings_only(env)
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(empty))
    assert go("--until", "gate") == 1
    assert env.calls == [] and stages(env) == {"collect": ("failed", "config:config_missing")}


def test_ping_still_runs_after_a_config_error_and_gets_no_config(env, monkeypatch):
    empty = settings_only(env)
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(empty))
    env.installed["daily.notify"] = env.make("notify")
    assert go() == 1
    assert env.calls == ["notify"]
    assert env.ctxs["notify"].cfg is None
    assert stages(env) == {"collect": ("failed", "config:config_missing"), "notify": ("done", "")}


def test_a_config_error_keeps_only_its_code(env):
    bad = json.loads((env.cfg_dir / "lanes.json").read_text())
    bad["lanes"]["demo-tool"]["disclosure"] = "bogus"
    (env.cfg_dir / "lanes.json").write_text(json.dumps(bad))
    with pytest.raises(ConfigError, match="demo-tool"):  # control: the error itself names the lane
        load_config(env.cfg_dir)
    assert go("--until", "gate") == 1
    assert stages(env) == {"collect": ("failed", "config:bad_shape")}
    assert "demo-tool" not in status_text(env)


def test_a_fixed_config_runs_the_day_on_the_next_try(env, monkeypatch):
    good = env.cfg_dir
    empty = settings_only(env)
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(empty))
    assert go("--until", "gate") == 1
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(good))
    assert go("--until", "gate") == 0
    assert env.calls == ["collect", "write", "gate"]


# --- lock ----------------------------------------------------------------------------------------------------------

def lock_is_free(lock) -> bool:
    """True when no process holds the flock on `lock` (taking it here is released again at once)."""
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


HOLDER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("held", flush=True)
sys.stdin.read()
"""


def holder(lock):
    """Another process that holds the flock on `lock` until its stdin closes; returned once it says it holds it."""
    p = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def let_go(p, kill=False):
    if kill:
        p.kill()
    else:
        p.stdin.close()
    p.wait(timeout=30)
    p.stdout.close()


def wait_for(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting for a handshake"
        time.sleep(0.005)


def test_a_lock_held_by_another_process_refuses_and_is_left_alone(env, capsys):
    env.work.mkdir(parents=True)
    lock = env.work / "run.lock"
    other = holder(lock)
    try:
        before = lock.read_bytes()
        assert go("--until", "gate") == 3
        assert env.calls == [] and lock.read_bytes() == before
        assert "lock held" in capsys.readouterr().err
        assert not (env.work / "status.json").exists()
    finally:
        let_go(other)
    assert go("--until", "gate") == 0  # control: the same lock is free once its holder lets go


def test_a_holder_killed_without_cleanup_does_not_block_the_next_run(env):
    env.work.mkdir(parents=True)
    lock = env.work / "run.lock"
    other = holder(lock)
    assert go("--until", "gate") == 3  # control: while it lives, the run is refused
    let_go(other, kill=True)  # SIGKILL: no finally, no cleanup of any kind
    assert lock.exists()
    assert go("--until", "gate") == 0
    assert env.calls == ["collect", "write", "gate"]


@pytest.mark.parametrize("content", [b"123", b"", b'{"pid": 1, "start": "9", "boot_id": "other"}',
                                     b'{"pid": 4242, "start": "1", "boot_id": "x"}', b"\xff\xfe\x00garbage"])
def test_a_lock_file_left_by_a_dead_process_does_not_block(env, content):
    env.work.mkdir(parents=True)
    lock = env.work / "run.lock"
    lock.write_bytes(content)
    old = time.time() - 365 * 86400
    os.utime(lock, (old, old))
    assert go("--until", "gate") == 0  # no flock is held on it, whatever it says
    assert env.calls == ["collect", "write", "gate"]


def test_a_lock_that_cannot_be_opened_fails_closed(env, capsys):
    """Exit 1 with lock_failed: 3 means only that another run holds the lock."""
    (env.work / "run.lock").mkdir(parents=True)
    assert go("--until", "gate") == 1
    assert env.calls == [] and (env.work / "run.lock").is_dir()
    assert "lock_failed" in capsys.readouterr().err


def test_the_lock_is_held_for_the_whole_run_and_refuses_a_second_run(env):
    seen = {}

    def write(ctx):
        lock = ctx.work / "run.lock"
        seen["diagnostics"] = json.loads(lock.read_text())
        seen["free_during"] = lock_is_free(lock)
        seen["second"] = run.main(["--day", "2026-10-16", "--until", "gate"], today=TODAY)
        return StageResult(ok=True)

    env.behaviour["write"] = write
    assert go("--until", "gate") == 0
    assert seen["second"] == 3 and seen["free_during"] is False
    assert seen["diagnostics"]["pid"] == os.getpid() and re.fullmatch(r"\d{4}-.*Z", seen["diagnostics"]["started"])
    assert env.calls == ["collect", "write", "gate"]  # the refused second run touched nothing


def test_the_lock_file_stays_and_is_free_and_empty_after_a_run(env):
    lock = env.work / "run.lock"
    assert go("--until", "gate") == 0
    assert lock.exists() and lock.read_bytes() == b"" and lock_is_free(lock)  # never unlinked, diagnostics cleared


@pytest.mark.parametrize("behaviour", [StageResult(ok=False, reason="boom"), RuntimeError("x")])
def test_the_lock_is_released_after_a_failed_run(env, behaviour):
    env.behaviour["write"] = behaviour
    assert go("--until", "gate") == 1
    assert (env.work / "run.lock").exists() and lock_is_free(env.work / "run.lock")
    env.behaviour.clear()
    assert go("--until", "gate") == 0  # control: a released lock lets the next run in


def test_the_lock_is_released_when_the_run_is_interrupted(env):
    env.behaviour["write"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        go("--until", "gate")
    assert lock_is_free(env.work / "run.lock")


RACER = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from daily import run
lock, ready, gate, done = (Path(a) for a in sys.argv[2:6])
ready.touch()
while not gate.exists():  # polling for a file the test creates: a handshake, not a delay
    time.sleep(0.001)
print(run._take_lock(lock) is not None, flush=True)
while not done.exists():  # a winner keeps the lock until every racer has reported
    time.sleep(0.001)
"""


def test_racing_runs_never_both_hold_the_lock(env):
    env.work.mkdir(parents=True)
    lock, gate, done = env.work / "run.lock", env.tmp / "go", env.tmp / "done"
    root = str(RUN_PY.parents[1])
    ready = [env.tmp / f"ready-{i}" for i in range(6)]
    procs = [subprocess.Popen([sys.executable, "-c", RACER, root, str(lock), str(r), str(gate), str(done)],
                              stdout=subprocess.PIPE, text=True) for r in ready]
    try:
        wait_for(lambda: all(r.exists() for r in ready))
        gate.touch()
        results = [p.stdout.readline().strip() for p in procs]
    finally:
        done.touch()
        for p in procs:
            p.wait(timeout=30)
            p.stdout.close()
    assert sorted(results) == ["False"] * 5 + ["True"]
    assert lock_is_free(lock)  # control: every racer is gone, so the lock is free again


def test_the_old_reclaim_interleaving_cannot_give_two_runners(env):
    """The race that broke the pid scheme: a run reads a live lock, the holder finishes, a third run takes the lock,
    and the first run then took it over. With a kernel lock there is nothing to read and nothing to take over."""
    env.work.mkdir(parents=True)
    lock = env.work / "run.lock"
    a = holder(lock)
    fd = run._take_lock(lock)  # B tries while A holds it
    assert fd is None
    let_go(a)  # A finishes
    c = holder(lock)  # C takes the lock (its first line proves the flock succeeded)
    try:
        assert run._take_lock(lock) is None  # B tries again: C is live and is not clobbered
        assert go("--until", "gate") == 3
    finally:
        let_go(c)
    fd = run._take_lock(lock)  # control: with C gone, B gets it
    assert fd is not None
    os.close(fd)


# --- SIGTERM -------------------------------------------------------------------------------------------------------

@pytest.fixture
def sigterm_guard():
    """A SIGTERM that reaches the test runner itself (the orchestrator installed no handler) fails the test instead of
    killing pytest."""
    def reached(signum, frame):
        pytest.fail("SIGTERM reached the test runner: the orchestrator installed no handler")

    old = signal.signal(signal.SIGTERM, reached)
    yield
    signal.signal(signal.SIGTERM, old)


def test_sigterm_in_a_stage_fails_that_stage_runs_ping_and_exits_1(env, sigterm_guard):
    env.installed["daily.notify"] = env.make("notify")
    swallowed = []

    def write(ctx):
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            for _ in range(1000):  # the handler raises at once; this loop only gives it a place to land
                time.sleep(0)
        except Exception:  # a stage's own broad handler must not be able to swallow the signal
            swallowed.append(True)
        return StageResult(ok=True)

    env.behaviour["write"] = write
    assert go() == 1
    assert swallowed == []
    assert stages(env) == {"collect": ("done", ""), "write": ("failed", "signal:SIGTERM"), "notify": ("done", "")}
    assert env.calls == ["collect", "write", "notify"]
    assert lock_is_free(env.work / "run.lock")


def test_sigterm_during_the_config_load_fails_collect_and_still_pings(env, monkeypatch, sigterm_guard):
    env.installed["daily.notify"] = env.make("notify")

    def load(dir_):
        os.kill(os.getpid(), signal.SIGTERM)
        for _ in range(1000):
            time.sleep(0)
        return load_config(dir_)

    monkeypatch.setattr(run, "load_config", load)
    assert go() == 1
    assert stages(env) == {"collect": ("failed", "signal:SIGTERM"), "notify": ("done", "")}
    assert env.ctxs["notify"].cfg is None and env.calls == ["notify"]


def _sigterm_now():
    os.kill(os.getpid(), signal.SIGTERM)
    for _ in range(1000):  # the handler raises at once; this loop only gives it a place to land
        time.sleep(0)


def test_sigterm_as_the_next_stage_is_being_prepared_fails_that_stage(env, monkeypatch, sigterm_guard):
    env.installed["daily.notify"] = env.make("notify")
    real, fired = run._forget_after, []

    def forget(status, name):
        if name == "write" and not fired:
            fired.append(True)
            _sigterm_now()
        return real(status, name)

    monkeypatch.setattr(run, "_forget_after", forget)
    assert go() == 1
    assert stages(env) == {"collect": ("done", ""), "write": ("failed", "signal:SIGTERM"), "notify": ("done", "")}
    assert env.calls == ["collect", "notify"]  # write never started


def test_sigterm_between_two_stages_fails_the_one_that_was_next(env, monkeypatch, sigterm_guard):
    env.installed["daily.notify"] = env.make("notify")
    real, fired = run._state, []

    def state(status, name):
        if name == "write" and not fired and real(status, "collect") == "done":  # the loop, after collect is recorded
            fired.append(True)
            _sigterm_now()
        return real(status, name)

    monkeypatch.setattr(run, "_state", state)
    assert go() == 1
    assert fired == [True]
    assert stages(env) == {"collect": ("done", ""), "write": ("failed", "signal:SIGTERM"), "notify": ("done", "")}
    assert env.calls == ["collect", "notify"]


def test_sigterm_during_the_ping_is_a_failed_ping_not_a_crash(env, sigterm_guard):
    for stage in ("notify", "voice", "render", "publish"):
        env.installed[f"daily.{stage}"] = env.make(stage)
    env.behaviour["notify"] = lambda ctx: _sigterm_now()
    assert go() == 1
    assert stages(env)["notify"] == ("failed", "signal:SIGTERM") and stages(env)["publish"] == ("done", "")


def test_the_sigterm_handler_is_only_installed_for_the_run(env, sigterm_guard):
    before = signal.getsignal(signal.SIGTERM)
    seen = {}
    env.behaviour["write"] = lambda ctx: seen.update(handler=signal.getsignal(signal.SIGTERM)) or StageResult(ok=True)
    assert go("--until", "gate") == 0
    assert seen["handler"] is not before  # control: during the run the orchestrator's handler is in place
    assert signal.getsignal(signal.SIGTERM) == before


SIGNAL_RUN = """
import sys
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from daily import run
from daily.stage import StageResult

def ok(ctx):
    return StageResult(ok=True)

def write(ctx):
    print("ready", flush=True)
    sys.stdin.read()  # blocks until the test's SIGTERM raises in here
    return StageResult(ok=True)

mods = {"daily.collect": ok, "daily.writer": write, "daily.notify": ok}
run.load_stage = lambda name: SimpleNamespace(run_stage=mods[name]) if name in mods else None
sys.exit(run.main(["--day", "2026-10-16"]))
"""


def test_a_real_sigterm_records_the_stage_pings_and_exits_1(tmp_path, cfg_dir):
    e = {**os.environ, "ORION_CLOCK_STATE": str(tmp_path / "state"), "ORION_CLOCK_CONFIG": str(cfg_dir)}
    p = subprocess.Popen([sys.executable, "-c", SIGNAL_RUN, str(RUN_PY.parents[1])], env=e, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "ready"  # the writer stage is running
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=30) == 1
    finally:
        p.kill()
        p.stdout.close()
    work = tmp_path / "state" / "2026-10-16"
    data = json.loads((work / "status.json").read_text())["stages"]
    assert {k: (v["state"], v["reason"]) for k, v in data.items()} == {
        "collect": ("done", ""), "write": ("failed", "signal:SIGTERM"), "notify": ("done", "")}
    assert lock_is_free(work / "run.lock")


# --- usage ---------------------------------------------------------------------------------------------------------

def test_missing_day_is_a_usage_error():
    with pytest.raises(SystemExit) as e:
        run.main([], today=TODAY)
    assert e.value.code == 2


def test_bad_day_is_a_usage_error(env, capsys):
    assert run.main(["--day", "someday"], today=TODAY) == 2
    assert "bad --day" in capsys.readouterr().err
    assert not (env.tmp / "state").exists()  # nothing was created for a day that does not exist


def test_a_calendar_date_works_without_today(env):
    assert run.main(["--day", "2026-10-16", "--until", "gate"]) == 0
    assert env.calls == ["collect", "write", "gate"]


@pytest.mark.parametrize("argv", [["--until", "bogus"], ["--from-stage", "bogus"], ["--bogus"]])
def test_unknown_stage_or_flag_is_a_usage_error(env, argv):
    with pytest.raises(SystemExit) as e:
        run.main(["--day", "yesterday", *argv], today=TODAY)
    assert e.value.code == 2


def test_the_terminal_stays_quiet(env, capsys):
    env.behaviour["write"] = StageResult(ok=True, reason="no_items", quiet=True)
    go("--until", "gate")
    env.behaviour["write"] = StageResult(ok=False, reason="boom")
    env.calls.clear()
    go("--until", "gate", "--from-stage", "write")
    assert env.calls == ["write"]  # control: both runs really ran stages
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


# --- status.json ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("junk", ["{not json", "[]", '{"day": "2026-10-15", "stages": {"collect": {"state": "done"}}}',
                                  '{"day": "2026-10-16", "stages": []}'])
def test_an_unusable_status_file_starts_the_day_over(env, junk):
    env.work.mkdir(parents=True)
    (env.work / "status.json").write_text(junk)
    assert go("--until", "gate") == 0
    assert env.calls == ["collect", "write", "gate"]
    assert stages(env)["gate"] == ("done", "")


def test_a_stage_entry_that_is_not_an_object_is_not_done(env):
    env.work.mkdir(parents=True)
    (env.work / "status.json").write_text(json.dumps({"day": "2026-10-16", "stages": {"collect": "done"}}))
    assert go("--until", "collect") == 0
    assert env.calls == ["collect"]


# --- one source of stage order and the module mapping --------------------------------------------------------------

def test_the_path_rule_is_the_ledgers_public_one():
    assert run.path_or_url is ledger.path_or_url


def test_stage_order_has_one_source():
    assert [name for name, _ in run.STAGES] == list(paths.STAGE_ORDER)
    assert run.NAMES == list(paths.STAGE_ORDER)
    assert set(run.MODULES) == set(paths.STAGE_ORDER)


def test_the_cli_takes_its_stage_names_from_the_same_list():
    for name in paths.STAGE_ORDER:
        assert run._parse(["--day", "yesterday", "--until", name]).until == name
        assert run._parse(["--day", "yesterday", "--from-stage", name]).from_stage == name


def test_stages_map_to_their_modules():
    mapping = dict(run.STAGES)
    assert mapping == {"collect": "daily.collect", "write": "daily.writer", "gate": "daily.gate",
                       "voice": "daily.voice", "render": "daily.render", "publish": "daily.publish",
                       "notify": "daily.notify"}


@pytest.mark.parametrize("name", ["publish", "notify"])
def test_the_release_stages_fail_closed_without_settings(name, tmp_path):
    """Registered and importable; without settings (the channel, the disclosure, the notify command) each stops
    instead of choosing a default."""
    mod = importlib.import_module(dict(run.STAGES)[name])
    res = mod.run_stage(Ctx(DAY, tmp_path, None, False))
    assert res == StageResult(ok=False, reason="settings_missing")


@pytest.mark.parametrize("name", ["collect", "write", "gate"])
def test_the_installed_stages_load_for_real(name):
    mod = importlib.import_module(dict(run.STAGES)[name])
    assert callable(mod.run_stage)


def test_the_real_load_stage(tmp_path, monkeypatch):
    real = run.load_stage  # this test does not use the env fixture, so the seam is not replaced
    assert real("daily.gate") is importlib.import_module("daily.gate")
    assert real("daily.no_such_stage_xyz") is None
    (tmp_path / "broken_stage_mod.py").write_text("import a_dependency_that_is_not_installed_xyz\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ModuleNotFoundError):  # a missing dependency is a bug, never "not installed yet"
        real("broken_stage_mod")


# --- reasons are untrusted -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("cannot open /home/someone/.claude/x.json for write", "cannot open <path> for write"),
    ("see ~/notes.md now", "see <path> now"),
    ("copy /mnt/c/Users/someone/a.txt now", "copy <path> now"),
    ("wrote /tmp/x", "wrote <path>"),
    ("read C:\\Users\\someone\\a.txt", "read <path>"),
    ("see a/b/c here", "see <path> here"),
    ("GET https://example.com/x failed", "GET <path> failed"),
    ("clone git@github.com:owner/repo", "clone <path>"),
    ("keeps 24/7 and a/b testing", "keeps 24/7 and a/b testing"),
    ("line one\nline two\t\tthree\r\n", "line one line two three"),
    ("  padded   out  ", "padded out"),
    ("caf\u00e9 \u2014 done", "caf done"),
    ("a\x00b", "ab"),
    ("/ho\u200bme/someone/x", "<path>"),
    ("\uff0fhome\uff0fsomeone", "<path>"),
    ("~\u00e9/secret", "<path>"),
    ("x" * 500, "x" * 160),
    ("\n\u2014\n", ""),
    ("", ""),
    (None, ""),
    (123, ""),
    # control: the reasons the stages really produce pass through untouched
    ("coverage gaps: codex, alpha_agent", "coverage gaps: codex, alpha_agent"),
    ("missing:ledger.json", "missing:ledger.json"),
    ("writer_no_script:invalid:schema", "writer_no_script:invalid:schema"),
    ("config:bad_shape", "config:bad_shape"),
    ("exception:RuntimeError", "exception:RuntimeError"),
    ("no_items", "no_items"),
])
def test_sanitize_reason(raw, expected):
    assert run.sanitize_reason(raw) == expected


def test_a_long_reason_is_cut_to_160_characters_with_no_trailing_space():
    out = run.sanitize_reason("word " * 100)
    assert len(out) <= 160 and out == out.rstrip() and out.startswith("word word")
    assert len(run.sanitize_reason("y" * 159 + " tail")) <= 160


def test_a_path_is_replaced_before_the_cut_never_halved_by_it():
    out = run.sanitize_reason("a" * 150 + " /home/someone/very/long/private/path/name")
    assert "/home" not in out and "someone" not in out and out.endswith("<path>")


@pytest.mark.parametrize("make_result", [
    lambda raw: StageResult(ok=False, reason=raw),
    lambda raw: StageResult(ok=True, reason=raw),
    lambda raw: StageResult(ok=True, reason=raw, quiet=True),
])
def test_every_recorded_reason_is_sanitized(env, make_result):
    raw = "opened /home/someone/.claude/secret.json\nthen \u2014 failed at ~/x " + "z" * 300
    env.behaviour["write"] = make_result(raw)
    go("--until", "gate")
    text = status_text(env)
    reason = json.loads(text)["stages"]["write"]["reason"]
    assert reason == run.sanitize_reason(raw) and reason.startswith("opened <path> then failed at <path> zzz")
    assert len(reason) <= 160 and "\n" not in reason
    assert "/home" not in text and "secret" not in text and text.isascii()


def test_an_exception_records_its_class_name_only(env):
    err = RuntimeError("cannot read /home/someone/secret.txt\nsecond line")
    assert "secret.txt" in str(err)  # control: str(e) would have carried the path and the text
    env.behaviour["write"] = err
    go("--until", "gate")
    assert stages(env)["write"] == ("failed", "exception:RuntimeError")
    text = status_text(env)
    assert "secret" not in text and "second line" not in text and "cannot read" not in text


def test_a_dry_run_sanitizes_too(env):
    env.behaviour["gate"] = StageResult(ok=True, reason="wrote /tmp/v.json")
    go("--until", "gate", "--dry")
    assert stages(env, dry=True)["gate"] == ("dry", "wrote <path>")


# --- the script entry ----------------------------------------------------------------------------------------------

def _script(args, tmp_path, cfg_dir):
    e = {**os.environ, "ORION_CLOCK_STATE": str(tmp_path / "state"), "ORION_CLOCK_CONFIG": str(cfg_dir)}
    return subprocess.run([sys.executable, str(RUN_PY), *args], env=e, capture_output=True, text=True, timeout=60)


def test_the_script_exits_2_on_a_bad_day(tmp_path, cfg_dir):
    done = _script(["--day", "someday"], tmp_path, cfg_dir)
    assert done.returncode == 2 and "bad --day" in done.stderr and done.stdout == ""


def test_the_script_exits_3_when_the_lock_is_held(tmp_path, cfg_dir):
    work = tmp_path / "state" / "2026-10-16-dry"  # a dry run's lock lives in its own directory
    work.mkdir(parents=True)
    other = holder(work / "run.lock")
    try:
        done = _script(["--day", "2026-10-16", "--until", "collect", "--dry"], tmp_path, cfg_dir)
    finally:
        let_go(other)
    assert done.returncode == 3 and "lock held" in done.stderr and done.stdout == ""
    assert not (work / "status.json").exists()



# --- module hygiene ---------------------------------------------------------------------------

def test_no_module_reaches_into_another_modules_private_names():
    """Helpers shared across modules are public: no `from daily.x import _name` and no `x._name` on a daily module."""
    import ast
    root = Path(run.__file__).resolve().parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.relative_to(root).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("daily"):
                for alias in node.names:
                    if alias.name.startswith("_"):
                        offenders.append(f"{path.name}: from {node.module} import {alias.name}")
                    elif node.module in ("daily", "daily.sources"):
                        modules.add(alias.asname or alias.name)
        offenders += [f"{path.name}: {n.value.id}.{n.attr}" for n in ast.walk(tree)
                      if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in modules
                      and n.attr.startswith("_") and not n.attr.startswith("__")]
    assert offenders == []


def test_the_test_only_aliases_are_gone():
    assert not hasattr(ledger, "_pathy") and not hasattr(ledger, "run_stage") and not hasattr(ledger, "gather")


# --- end to end: collect, write and gate through run.main ---------------------------------------------------------

def test_collect_write_and_gate_run_end_to_end_to_a_passing_verdict(monkeypatch, tmp_path, cfg_dir):
    """The real stage modules, run by run.main with the real settings loader; only the readers, the text scanners and
    the `claude -p` call are fakes. The fake writer builds its script from the ledger the collect stage wrote, so the
    gate proves the PR quote against the record file collect wrote beside it."""
    import dataclasses
    from daily import gate, textcheck, writer
    from daily.sources import claude, codex, github
    from daily.sources.claude import ClaudeDay
    from daily.sources.codex import CodexDay
    from daily.sources.github import PR, AttributedPRs
    from daily.textcheck import TextChecks
    from daily_helpers import good_script

    monkeypatch.setenv("ORION_CLOCK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(cfg_dir))
    monkeypatch.setattr(paths, "DEFAULT_CLAUDE_PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(paths, "DEFAULT_CODEX_DB", tmp_path / "codex" / "state_5.sqlite")
    sentence = "The codex lane merged the export flag and the tests stayed green."
    merged = PR("owner/demo-tool", 7, "Add the export flag", "## Summary\n" + sentence + "\n",
                "https://github.com/owner/demo-tool/pull/7", "2026-10-16T21:00:00Z")
    owners = []
    monkeypatch.setattr(github, "merged_prs", lambda day, runner, *, owner: owners.append(owner) or [merged])
    monkeypatch.setattr(github, "attribute", lambda prs, runner, *, want, prefixes: AttributedPRs(
        dataclasses.replace(p, harness="codex") if want(p) else p for p in prs))
    monkeypatch.setattr(claude, "scan_claude", lambda day, root, resolver, *, exclude_fragments: ClaudeDay(2, 1, ()))
    monkeypatch.setattr(codex, "scan_codex", lambda day, db, resolver, sessions, *, exclude_fragments: CodexDay(1, 0, ()))
    monkeypatch.setattr(textcheck, "load", lambda settings=None: TextChecks(lambda t: ("PUBLISH", []), lambda t: []))
    monkeypatch.setattr(gate, "default_deps", lambda settings=None: gate.Deps(lambda t: ("PUBLISH", []), lambda t: [],
                                                                             lambda t: []))
    prompts = []

    def fake_claude(cmd, stdin, env, cwd):
        prompts.append(stdin)
        led = json.loads((Path(cwd) / "ledger.json").read_text(encoding="utf-8"))
        [quote] = [q for q in led["quotes"] if q["kind"] == "pr"]
        script = good_script(quote["id"], story_item_id=led["items"][0]["id"])
        return 0, json.dumps({"is_error": False, "session_id": "s1", "num_turns": 2, "total_cost_usd": 0.1,
                              "usage": {}, writer.RESULT_FIELD: script}), ""
    real_write = writer.run_stage
    monkeypatch.setattr(writer, "run_stage", lambda ctx: real_write(ctx, proc=fake_claude, lint_loader=lambda: None))

    assert run.main(["--day", "2026-10-16", "--until", "gate"], today=TODAY) == 0
    work = tmp_path / "state" / "2026-10-16"
    data = json.loads((work / "status.json").read_text(encoding="utf-8"))
    assert {k: (v["state"], v["reason"]) for k, v in data["stages"].items()} == {
        "collect": ("done", ""), "write": ("done", ""), "gate": ("done", "")}
    assert owners == ["demo-owner"]  # the owner from settings.toml
    assert len(prompts) == 1 and sentence in prompts[0]  # the writer saw the ledger's quote
    assert '"Demo Agents on the Clock"' in prompts[0] and "Sam's AI agents" in prompts[0]  # the show from settings
    assert gate.accepts(work / "verdict.json", work / "script.json", gate.REQUIRED,
                        ledger_path=work / "ledger.json") == (True, "ok")
    verdict = json.loads((work / "verdict.json").read_text(encoding="utf-8"))
    evidence = {c["name"]: c["evidence"] for c in verdict["checks"]}
    assert verdict["pass"] is True and verdict["dry"] is False
    assert evidence["story_in_ledger"] == "pr:demo-tool#7" and evidence["quote_verbatim"] == "matched github.jsonl:1"
    ledger_ = json.loads((work / "ledger.json").read_text(encoding="utf-8"))
    assert ledger_["tz"] == "America/Los_Angeles" and ledger_["scoreboard"]["arcs_closed"] is None  # no tracker


# --- settings.toml: loaded first, and a bad one stops the run before anything else --------------------------------

@pytest.mark.parametrize("text,code", [(None, "settings_missing"), ("timezone = 'UTC'\n", "missing:show.series"),
                                       ("timezone = 'Mars/Base'\n", "bad:timezone")])
def test_bad_settings_print_one_line_and_exit_1_before_any_stage(env, capsys, text, code):
    settings = env.cfg_dir / "settings.toml"
    if text is None:
        settings.unlink()
    else:
        settings.write_text(text)
    assert go("--until", "gate") == 1
    err = capsys.readouterr().err
    assert err.startswith(f"settings error: {code} (") and err.count("\n") == 1
    assert env.calls == [] and not env.work.exists() and not (env.tmp / "state" / "last-run.json").exists()


def test_the_settings_zone_decides_which_day_yesterday_is(env):
    """The due day is a day in the settings' zone: at 03:00 UTC on the 17th it is still the 16th in Los Angeles but
    already the 17th in Tokyo, so the two recap different days."""
    now = dt.datetime(2026, 10, 17, 3, 0, tzinfo=dt.timezone.utc)
    assert run.main(["--day", "due", "--until", "collect"], now=now) == 0
    assert (env.tmp / "state" / "2026-10-15" / "status.json").is_file()  # 20:00 PDT on the 16th: after 05:30
    text = (env.cfg_dir / "settings.toml").read_text().replace("America/Los_Angeles", "Asia/Tokyo")
    (env.cfg_dir / "settings.toml").write_text(text)
    assert run.main(["--day", "due", "--until", "collect"], now=now) == 0
    assert (env.tmp / "state" / "2026-10-16" / "status.json").is_file()  # 12:00 JST on the 17th: after 05:30


def test_every_stage_gets_the_loaded_settings(env):
    assert go("--until", "gate") == 0
    s = env.ctxs["collect"].settings
    assert s.github_owner == "demo-owner" and env.ctxs["gate"].settings is s


# --- exit 5: failed, and the notify stage reported it -------------------------------------------------

def _all_stages(env):
    for stage in ("voice", "render", "publish", "notify"):
        env.installed[f"daily.{stage}"] = env.make(stage)


@pytest.mark.parametrize("reason", ["notified: failed-collect", "already notified: failed-collect"])
def test_a_failed_stage_that_the_ping_reported_exits_5(env, reason):
    _all_stages(env)
    env.behaviour["collect"] = StageResult(ok=False, reason="boom")
    env.behaviour["notify"] = StageResult(ok=True, reason=reason)
    assert run.EXIT_FAILED_REPORTED == 5
    assert go() == 5
    assert stages(env)["collect"] == ("failed", "boom")


def test_a_needs_operator_publish_that_the_notify_reported_exits_5(env):
    _all_stages(env)
    env.behaviour["publish"] = StageResult(ok=False, reason="needs_operator:signed_out")
    env.behaviour["notify"] = StageResult(ok=True, reason="notified: needs_operator")
    assert go() == 5


def test_a_config_error_that_the_ping_reported_exits_5(env, monkeypatch):
    def bad(_):
        raise ConfigError("config_missing:lanes.json")
    monkeypatch.setattr(run, "load_config", bad)
    _all_stages(env)
    env.behaviour["notify"] = StageResult(ok=True, reason="notified: config_error")
    assert go() == 5


def test_a_failure_the_ping_did_not_report_exits_1(env):
    _all_stages(env)
    env.behaviour["collect"] = StageResult(ok=False, reason="boom")
    for ping_result in (StageResult(ok=True, reason=""), StageResult(ok=True, reason="nothing_to_report"),
                        StageResult(ok=False, reason="inbox_exit_1"), OSError("x")):
        env.behaviour["notify"] = ping_result
        assert go() == 1


def test_a_failure_with_no_ping_module_or_no_ping_stage_exits_1(env):
    env.behaviour["collect"] = StageResult(ok=False, reason="boom")
    assert go() == 1                                  # the notify module is not installed
    _all_stages(env)
    env.behaviour["notify"] = StageResult(ok=True, reason="notified: x")
    assert go("--until", "render") == 1               # the run never reached the notify stage


def test_a_dry_failure_exits_1_and_a_clean_run_still_exits_0(env):
    _all_stages(env)
    env.behaviour["collect"] = StageResult(ok=False, reason="boom")
    env.behaviour["notify"] = StageResult(ok=True, reason="notified: x")
    assert go("--dry") == 1
    env.behaviour.clear()
    env.behaviour["notify"] = StageResult(ok=True, reason="notified: ready")
    assert go() == 0


def test_the_reported_prefixes_are_the_notify_contract():
    assert paths.NOTIFY_REPORTED == ("notified", "already notified", "closed outside")


def test_a_failure_whose_item_was_closed_outside_counts_as_reported(env):
    _all_stages(env)
    env.behaviour["write"] = StageResult(ok=False, reason="writer_timeout")
    env.behaviour["notify"] = StageResult(ok=True, reason="closed outside: failed")
    assert go() == 5


def test_day_due_builds_the_missed_day_for_an_early_wake(env):
    zone = ZoneInfo("America/Los_Angeles")  # the fixture settings' zone
    early = dt.datetime(2026, 10, 17, 4, 0, tzinfo=zone)
    late = dt.datetime(2026, 10, 17, 6, 0, tzinfo=zone)
    run.main(["--day", "due", "--until", "collect"], today=early.date(), now=early)
    assert (env.tmp / "state" / "2026-10-15" / "status.json").is_file()
    assert not (env.tmp / "state" / "2026-10-16").exists()
    run.main(["--day", "due", "--until", "collect"], today=late.date(), now=late)
    assert (env.tmp / "state" / "2026-10-16" / "status.json").is_file()



# --- the last-run record (OnFailure files its alert under the day this run selected) ---------------------------------

def last_run(env):
    return json.loads((env.tmp / "state" / "last-run.json").read_text())


def test_a_real_run_records_the_day_it_selected_before_any_stage(env):
    seen = {}
    env.behaviour["collect"] = lambda ctx: seen.update(rec=last_run(env)) or StageResult(ok=True)
    assert go("--until", "collect") == 0
    assert seen["rec"]["day"] == "2026-10-16" and seen["rec"]["pid"] == os.getpid()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", seen["rec"]["started_at"])


def test_the_record_is_written_atomically(env, monkeypatch):
    calls = []
    real = run.write_json
    monkeypatch.setattr(run, "write_json", lambda p, o: (calls.append(Path(p).name), real(p, o))[1])
    go("--until", "collect")
    assert "last-run.json" in calls


def test_a_dry_run_and_a_refused_run_leave_the_record_alone(env):
    root = env.tmp / "state"
    root.mkdir(parents=True)
    (root / "last-run.json").write_text('{"day": "2026-01-01"}')
    assert go("--until", "gate", "--dry") == 0
    assert (root / "last-run.json").read_text() == '{"day": "2026-01-01"}'
    env.work.mkdir(parents=True)
    other = holder(env.work / "run.lock")
    try:
        assert go("--until", "gate") == 3
    finally:
        let_go(other)
    assert (root / "last-run.json").read_text() == '{"day": "2026-01-01"}'


def test_an_unwritable_record_never_stops_the_run(env):
    (env.tmp / "state" / "last-run.json").mkdir(parents=True)
    assert go("--until", "gate") == 0
    assert env.calls == ["collect", "write", "gate"]
