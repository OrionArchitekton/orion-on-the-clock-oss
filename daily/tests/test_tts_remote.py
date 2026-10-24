"""Voicing on a remote GPU box (settings.toml `tts.host`) with the local runner as the fallback, and how the voice
stage picks where to voice (`tts.target`). Nothing here runs ssh or rsync: every subprocess goes through
tts_remote._sh, which these tests replace with a recorder, and the local runner is faked."""
from __future__ import annotations

import ast
import dataclasses
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from daily import files, tts_remote, tts_runner, voice
from daily.settings import load_settings
from media_helpers import make_config

REF = "abc123def4567890"
LINES = [{"id": "cold_open", "scene": "cold_open", "voice": "host", "text": "hello there", "params": {}},
         {"id": "out", "scene": "out", "voice": "host", "text": "goodbye now", "params": {}}]
HOST = "gpu-box"
ROOT = "/var/tmp/tts-runs"
REMOTE_PY = "/opt/tts/bin/python"
TTS_TOML = f'''
[tts]
python = "/opt/local-tts/bin/python"
host = "{HOST}"
remote_python = "{REMOTE_PY}"
remote_dir = "{ROOT}"
remote_gpu_allow = ["llama-server", "ollama"]
'''
CODES = {"unreachable", "busy", "low_memory", "rsync_failed", "runner_failed", "bad_results", "no_cuda"}
VOICES_BYTES = b'{"exact": "bytes"}\n'
REAL_RUN_LOCAL = voice._run_local  # the env fixture replaces it; one test puts it back
SHA = files.sha256_bytes(VOICES_BYTES)


class FakeRemote:
    """Stands in for tts_remote._sh. Knobs pick where the remote fails; every call is recorded."""

    def __init__(self, free_mib="9000", apps="llama-server, 19000", fail=None, results="ok", device="cuda"):
        self.free_mib, self.apps, self.fail, self.results, self.device = free_mib, apps, fail, results, device
        self.calls, self.staged = [], {}

    def __call__(self, argv, *, timeout, out=None):
        self.calls.append({"argv": list(argv), "timeout": timeout})
        rc, text = 0, ""
        if "--query-gpu=memory.free" in argv:
            rc, text = (255 if self.fail == "unreachable" else 0), self.free_mib + "\n"
        elif "--query-compute-apps=process_name,used_memory" in argv:
            text = self.apps + "\n"
        elif argv[0] == "rsync" and argv[-1].startswith(f"{HOST}:") and argv[-1].endswith("/"):  # upload
            stage = Path(argv[-2])
            self.staged = {str(p.relative_to(stage)): p.read_bytes() for p in stage.rglob("*") if p.is_file()}
            rc = 23 if self.fail == "upload" else 0
        elif argv[0] == "rsync":  # fetch
            if self.fail == "fetch":
                rc = 23
            else:
                self._fetch(Path(argv[-1]))
        elif "mkdir" in argv:
            rc = 1 if self.fail == "mkdir" else 0
        elif "tts_runner.py" in " ".join(argv):
            if out is not None:
                out.write("device cuda: 2 line(s)\n" + ("" if self.fail == "no_marker" else "RUNNER_OK\n"))
            rc = 1 if self.fail == "runner" else 3 if self.fail == "no_cuda" else 0
        elif argv[-1].startswith("find "):
            rc = 1 if self.fail == "prune" else 0
        elif "rm" in argv:
            rc = 1 if self.fail == "cleanup" else 0
        else:
            raise AssertionError(f"unexpected argv {argv}")
        return subprocess.CompletedProcess(argv, rc, stdout=text, stderr="")

    def _fetch(self, dest: Path):
        if self.results == "nothing":
            return
        for ln in LINES:
            if self.results == "missing_wav" and ln["id"] == "out":
                continue
            (dest / f"{ln['id']}.wav").write_bytes(b"RIFF")
        if self.results != "no_results":
            rows = [{"id": ln["id"]} for ln in LINES]
            if self.results == "wrong_ids":
                rows = rows[:1]
            ref = "0000000000000000" if self.results == "wrong_ref" else REF
            (dest / "tts-results.json").write_text(json.dumps(
                {"device": self.device, "ref_sha256": ref, "lines": rows, "tts_target": "bogus", "remote_skip": "bogus"}))

    def kinds(self):
        out = []
        for c in self.calls:
            a = c["argv"]
            out.append("preflight" if "nvidia-smi" in a else "upload" if a[0] == "rsync" and a[-1].startswith(HOST)
                       else "fetch" if a[0] == "rsync" else "mkdir" if "mkdir" in a
                       else "run" if "tts_runner.py" in " ".join(a) else "prune" if a[-1].startswith("find ")
                       else "cleanup" if "rm" in a else "?")
        return out


@pytest.fixture
def base(tmp_path):
    """Real settings loaded from a config folder whose [tts] table names the remote and the allowed GPU processes."""
    return load_settings(make_config(tmp_path / "cfg", extra=TTS_TOML))


@pytest.fixture
def env(tmp_path, monkeypatch):
    vd = tmp_path / "voice"
    vd.mkdir()
    files.write_json(vd / "lines.json", LINES)
    vp = tmp_path / "voices.json"
    vp.write_bytes(VOICES_BYTES)
    local_calls = []

    def local(lines, voice_dir, voices_path, ref, settings):
        local_calls.append(1)
        (Path(voice_dir) / "tts-results.json").write_text("{}")
        return {"device": "cpu", "ref_sha256": ref, "lines": [{"id": ln["id"]} for ln in lines]}

    monkeypatch.setattr(voice, "_run_local", local)
    return vd, vp, local_calls


def remote(monkeypatch, **kw):
    fake = FakeRemote(**kw)
    monkeypatch.setattr(tts_remote, "_sh", fake)
    return fake


def run(env, settings, **kw):
    vd, vp, _ = env
    return voice.run_tts(LINES, vd, vp, REF, settings=settings, voices_sha=SHA, day="2026-10-18", **kw)


def with_(settings, **over):
    return dataclasses.replace(settings, **over)


# ---- the Remote from settings ----

def test_the_remote_comes_from_the_settings(base):
    r = tts_remote.Remote.from_settings(base)
    assert r == tts_remote.Remote(HOST, REMOTE_PY, ROOT, ("llama-server", "ollama"))
    assert tts_remote.Remote.from_settings(with_(base, tts_host=None)) is None


def test_the_default_gpu_allow_is_empty_so_any_gpu_process_means_busy(tmp_path, env, monkeypatch):
    plain = load_settings(make_config(tmp_path / "plain", extra=TTS_TOML.replace(
        'remote_gpu_allow = ["llama-server", "ollama"]\n', "")))
    assert plain.tts_remote_gpu_allow == () and tts_remote.Remote.from_settings(plain).gpu_allow == ()
    remote(monkeypatch, apps="llama-server, 19000")
    res = run(env, plain)
    assert res["tts_target"] == "local" and res["remote_skip"] == "busy"
    remote(monkeypatch, apps="")  # an empty GPU passes with no allowlist at all
    assert run(env, plain)["tts_target"] == "remote"


# ---- the remote path ----

def test_a_clear_remote_voices_there_and_never_touches_the_local_runner(env, base, monkeypatch):
    fake = remote(monkeypatch)
    res = run(env, base)
    assert env[2] == []
    assert fake.kinds() == ["preflight", "preflight", "mkdir", "upload", "run", "fetch", "cleanup", "prune"]
    assert res["tts_target"] == "remote" and res["device"] == "cuda" and "remote_skip" not in res
    saved = json.loads((env[0] / "tts-results.json").read_text())
    assert saved["tts_target"] == "remote" and saved["device"] == "cuda"


def test_the_preflight_argv(env, base, monkeypatch):
    fake = remote(monkeypatch)
    run(env, base)
    pre = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", HOST, "nvidia-smi"]
    assert fake.calls[0]["argv"] == pre + ["--query-gpu=memory.free", "--format=csv,noheader,nounits"]
    assert fake.calls[1]["argv"] == pre + ["--query-compute-apps=process_name,used_memory",
                                           "--format=csv,noheader,nounits"]
    assert all(c["timeout"] > 0 for c in fake.calls)


def test_the_remote_run_dir_and_command(env, base, monkeypatch):
    fake = remote(monkeypatch)
    run(env, base)
    kinds = fake.kinds()
    mk, up, rn, fe, rm = (fake.calls[kinds.index(k)]["argv"] for k in ("mkdir", "upload", "run", "fetch", "cleanup"))
    rd = re.match(rf"({re.escape(ROOT)}/\d{{8}}-[0-9a-f]{{12}})/", mk[-1]).group(1)
    assert mk == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", HOST, "mkdir", "-p", "--",
                  f"{rd}/daily", f"{rd}/out"]
    assert rd.rpartition("/")[2].startswith("20261018-")
    assert up[-1] == f"{HOST}:{rd}/" and up[0] == "rsync" and up[-3] == "--"
    assert "--no-links" in up and "--no-links" in fe and fe[-3] == "--"
    assert "ServerAliveInterval=30" in rn
    assert rn[:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"] and HOST in rn
    tail = rn[rn.index(HOST) + 1:]
    assert tail == ["timeout", "--kill-after=60", str(tts_remote.RUN_TIMEOUT_S), "env",
                    "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True", "ORION_CLOCK_TTS_REQUIRE_CUDA=1",
                    "HF_HUB_OFFLINE=1", REMOTE_PY, f"{rd}/daily/tts_runner.py", f"{rd}/lines.json", f"{rd}/out",
                    f"{rd}/voices.json"]
    assert fake.calls[kinds.index("run")]["timeout"] > tts_remote.RUN_TIMEOUT_S
    assert fe[-2] == f"{HOST}:{rd}/out/" and fe[-1] == str(env[0]) + "/"
    assert "--include=*.wav" in fe and "--include=tts-results.json" in fe and "--exclude=*" in fe
    assert rm[-4:] == ["rm", "-r", "--", rd]


def test_the_exact_voices_bytes_and_runner_modules_are_staged(env, base, monkeypatch):
    fake = remote(monkeypatch)
    run(env, base)
    assert fake.staged["voices.json"] == VOICES_BYTES
    assert fake.staged["lines.json"] == (env[0] / "lines.json").read_bytes()
    assert {k for k in fake.staged if k.startswith("daily/")} == {
        f"daily/{m}" for m in ("tts_runner.py", "tts_plan.py", "voiceconf.py", "files.py", "__init__.py")}
    assert not (env[0] / tts_remote.STAGE_DIR).exists()


def test_every_argv_is_a_list_of_strings(env, base, monkeypatch):
    fake = remote(monkeypatch)
    run(env, base)
    assert all(isinstance(c["argv"], list) and all(isinstance(a, str) for a in c["argv"]) for c in fake.calls)


@pytest.mark.parametrize("kw,code", [
    ({"free_mib": "2000"}, "low_memory"),
    ({"free_mib": "4607"}, "low_memory"),
    ({"free_mib": "9000\n3000"}, "low_memory"),
    ({"free_mib": "3000\n9000"}, "low_memory"),
    ({"apps": "/opt/ollama-runner/python3, 100"}, "busy"),
    ({"apps": "llama-server-extra, 100"}, "busy"),
    ({"apps": "python3, llama-server"}, "busy"),
    ({"device": "cpu"}, "bad_results"),
    ({"results": "nothing"}, "bad_results"),
    ({"apps": "python3, 3000"}, "busy"),
    ({"apps": "llama-server, 19000\n/usr/bin/python3, 3000"}, "busy"),
    ({"apps": "[N/A], 100"}, "busy"),
    ({"fail": "unreachable"}, "unreachable"),
    ({"free_mib": "garbage"}, "unreachable"),
    ({"fail": "mkdir"}, "rsync_failed"),
    ({"fail": "upload"}, "rsync_failed"),
    ({"fail": "runner"}, "runner_failed"),
    ({"fail": "no_marker"}, "runner_failed"),
    ({"fail": "no_cuda"}, "no_cuda"),
    ({"fail": "fetch"}, "rsync_failed"),
    ({"results": "no_results"}, "bad_results"),
    ({"results": "wrong_ids"}, "bad_results"),
    ({"results": "wrong_ref"}, "bad_results"),
    ({"results": "missing_wav"}, "bad_results"),
])
def test_every_remote_failure_falls_back_to_local_once_with_a_code(env, base, monkeypatch, kw, code):
    fake = remote(monkeypatch, **kw)
    res = run(env, base)
    assert env[2] == [1]
    assert res["tts_target"] == "local" and res["remote_skip"] == code and code in CODES
    assert json.loads((env[0] / "tts-results.json").read_text())["remote_skip"] == code
    assert fake.kinds().count("run") <= 1 and fake.kinds().count("preflight") <= 2  # the remote is never retried
    assert fake.kinds().count("prune") <= 1


def test_the_codes_are_the_documented_set():
    assert set(tts_remote.CODES) == CODES
    assert {f"tts_remote_{c}" for c in CODES} | {"tts_remote_unconfigured"} <= voice.VOICE_CODES


def test_the_allowed_server_names_pass_the_preflight(env, base, monkeypatch):
    remote(monkeypatch, apps="/usr/local/bin/llama-server, 19000\nollama, 100")
    assert run(env, base)["tts_target"] == "remote"


def test_an_allowed_name_matches_the_file_name_exactly_ignoring_case(env, base, monkeypatch):
    remote(monkeypatch, apps="/usr/bin/OLLAMA, 100")
    assert run(env, base)["tts_target"] == "remote"
    remote(monkeypatch, apps="/usr/bin/ollama-helper, 100")
    assert run(env, base)["remote_skip"] == "busy"


def test_an_empty_gpu_passes_the_preflight(env, base, monkeypatch):
    remote(monkeypatch, apps="")
    assert run(env, base)["tts_target"] == "remote"


def test_the_preflight_only_reads(base, monkeypatch):
    """It asks nvidia-smi two questions and nothing else: no kill, no unload, no pause, on a busy GPU or a clear one."""
    for apps in ("python3, 3000", ""):
        fake = remote(monkeypatch, apps=apps)
        try:
            tts_remote.preflight(tts_remote.Remote.from_settings(base))
        except tts_remote.RemoteSkip:
            pass
        assert [c["argv"][c["argv"].index(HOST) + 1] for c in fake.calls] == ["nvidia-smi", "nvidia-smi"]


def test_a_timed_out_remote_run_falls_back(env, base, monkeypatch):
    fake = remote(monkeypatch)

    def sh(argv, *, timeout, out=None):
        if "tts_runner.py" in " ".join(argv):
            raise subprocess.TimeoutExpired(argv, timeout)
        return fake(argv, timeout=timeout, out=out)

    monkeypatch.setattr(tts_remote, "_sh", sh)
    assert run(env, base)["remote_skip"] == "runner_failed" and env[2] == [1]


def test_a_failed_cleanup_does_not_fail_the_run(env, base, monkeypatch):
    remote(monkeypatch, fail="cleanup")
    assert run(env, base)["tts_target"] == "remote" and env[2] == []


def test_the_remote_log_is_kept_when_the_local_runner_takes_over(env, base, monkeypatch):
    remote(monkeypatch, fail="runner")
    run(env, base)
    assert "RUNNER_OK" in (env[0] / "tts-remote.log").read_text()


# ---- where voicing runs: tts.target ----

def test_target_local_never_calls_the_remote(env, base, monkeypatch):
    fake = remote(monkeypatch)
    res = run(env, with_(base, tts_target="local"))
    assert fake.calls == [] and env[2] == [1] and res["tts_target"] == "local" and "remote_skip" not in res


def test_target_remote_forces_the_remote_and_does_not_fall_back(env, base, monkeypatch):
    remote(monkeypatch)
    forced = with_(base, tts_target="remote")
    assert run(env, forced)["tts_target"] == "remote" and env[2] == []
    remote(monkeypatch, apps="python3, 1")
    with pytest.raises(voice.VoiceError) as e:
        run(env, forced)
    assert env[2] == [] and voice.failure_code(e.value) == "voice:tts_remote_busy"


def test_auto_without_a_host_voices_here_only(env, base, monkeypatch):
    fake = remote(monkeypatch)
    res = run(env, with_(base, tts_target="auto", tts_host=None))
    assert fake.calls == [] and env[2] == [1] and res["tts_target"] == "local" and "remote_skip" not in res


def test_a_forced_remote_with_no_host_is_its_own_code(env, base, monkeypatch):
    """settings.py refuses target = "remote" without a host; run_tts refuses it again rather than voicing here."""
    fake = remote(monkeypatch)
    with pytest.raises(voice.VoiceError) as e:
        run(env, with_(base, tts_target="remote", tts_host=None))
    assert voice.failure_code(e.value) == "voice:tts_remote_unconfigured" and fake.calls == [] and env[2] == []


def test_an_unknown_target_value_means_auto(env, base, monkeypatch):
    remote(monkeypatch)
    assert run(env, with_(base, tts_target="nonsense"))["tts_target"] == "remote"


def test_tts_targets_is_remote_then_local():
    assert voice.TTS_TARGETS == ("remote", "local")


def test_the_manifest_records_where_voicing_ran(tmp_path):
    voice.write_manifest(tmp_path, LINES, {"cold_open": "", "out": ""}, "v" * 64, REF,
                         tts={"tts_target": "local", "device": "cpu", "remote_skip": "busy"})
    m = json.loads((tmp_path / voice.MANIFEST).read_text())
    assert (m["tts_target"], m["device"], m["remote_skip"]) == ("local", "cpu", "busy")
    voice.write_manifest(tmp_path, LINES, {"cold_open": "", "out": ""}, "v" * 64, REF,
                         tts={"tts_target": "remote", "device": "cuda"})
    m = json.loads((tmp_path / voice.MANIFEST).read_text())
    assert m["tts_target"] == "remote" and "remote_skip" not in m


def test_local_results_record_target_and_device(env, base):
    res = voice.run_tts(LINES, env[0], env[1], REF, settings=with_(base, tts_target="local"))
    assert res["tts_target"] == "local" and res["device"] == "cpu"


def test_local_runs_never_require_cuda_and_carry_the_lock(env, base, tmp_path, monkeypatch):
    """The real local runner call (subprocess faked): the alloc setting and the configured lock file reach the child;
    the remote's require-cuda switch and offline hub never do, and the parent's environment is left as it was."""
    monkeypatch.setattr(voice, "_run_local", REAL_RUN_LOCAL)  # the real one, not the env fixture's stand-in
    monkeypatch.delenv(tts_runner.REQUIRE_CUDA_ENV, raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, env=kw["env"])
        kw["stdout"].write("RUNNER_OK\n")
        vd = Path(argv[3])
        for ln in LINES:
            (vd / f"{ln['id']}.wav").write_bytes(b"RIFF")
        files.write_json(vd / "tts-results.json", {"device": "cpu", "ref_sha256": REF,
                                                   "lines": [{"id": ln["id"]} for ln in LINES]})
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(voice.subprocess, "run", fake_run)
    lock = tmp_path / "locks" / "tts.lock"
    res = voice.run_tts(LINES, env[0], env[1], REF, settings=with_(base, tts_target="local", tts_lock=lock))
    assert res["tts_target"] == "local"
    assert seen["argv"][0] == "/opt/local-tts/bin/python" and seen["argv"][1] == str(voice.TTS_RUNNER)
    assert seen["env"]["PYTORCH_CUDA_ALLOC_CONF"] == "expandable_segments:True"
    assert seen["env"][tts_runner.LOCK_ENV] == str(lock)
    assert tts_runner.REQUIRE_CUDA_ENV not in seen["env"] and "HF_HUB_OFFLINE" not in seen["env"]
    assert tts_runner.LOCK_ENV not in os.environ and "HF_HUB_OFFLINE" not in os.environ


def test_free_memory_exactly_at_the_floor_passes(env, base, monkeypatch):
    remote(monkeypatch, free_mib="4608")  # 4.5 GiB: the floor is "at least"
    assert run(env, base)["tts_target"] == "remote"


def test_two_gpus_both_clear_pass(env, base, monkeypatch):
    remote(monkeypatch, free_mib="9000\n5000")
    assert run(env, base)["tts_target"] == "remote"


def test_stale_local_results_and_wavs_are_never_read_as_the_fetch(env, base, monkeypatch):
    vd = env[0]
    (vd / "tts-results.json").write_text(json.dumps({"device": "cuda", "ref_sha256": REF,
                                                     "lines": [{"id": ln["id"]} for ln in LINES]}))
    for ln in LINES:
        (vd / f"{ln['id']}.wav").write_bytes(b"stale")
    remote(monkeypatch, results="nothing")
    assert run(env, base)["remote_skip"] == "bad_results" and env[2] == [1]


def test_fetched_target_keys_are_overwritten_by_the_real_ones(env, base, monkeypatch):
    remote(monkeypatch)
    res = run(env, base)
    assert res["tts_target"] == "remote" and "remote_skip" not in res
    assert "remote_skip" not in json.loads((env[0] / "tts-results.json").read_text())


def test_changed_voices_bytes_stop_before_any_remote_call(env, base, monkeypatch):
    fake = remote(monkeypatch)
    with pytest.raises(voice.VoiceError) as e:
        voice.run_tts(LINES, env[0], env[1], REF, settings=base, voices_sha="0" * 64, day="2026-10-18")
    assert e.value.args == ("voice_config_changed",) and fake.calls == [] and env[2] == []


@pytest.mark.parametrize("kw", [{}, {"fail": "runner"}, {"fail": "no_cuda"}, {"fail": "fetch"}, {"fail": "upload"},
                                {"results": "wrong_ref"}, {"device": "cpu"}])
def test_the_remote_run_dir_is_removed_on_every_outcome_that_made_it(env, base, monkeypatch, kw):
    fake = remote(monkeypatch, **kw)
    run(env, base)
    rms = [c["argv"] for c in fake.calls if "rm" in c["argv"]]
    mk = next(c["argv"] for c in fake.calls if "mkdir" in c["argv"])
    rd = mk[-1].removesuffix("/out")
    assert rms == [[*tts_remote.SSH, HOST, "rm", "-r", "--", rd]]


def test_a_cleanup_that_raises_changes_nothing(env, base, monkeypatch):
    fake = remote(monkeypatch, fail="runner")

    def sh(argv, *, timeout, out=None):
        if "rm" in argv:
            raise subprocess.TimeoutExpired(argv, timeout)
        return fake(argv, timeout=timeout, out=out)

    monkeypatch.setattr(tts_remote, "_sh", sh)
    assert run(env, base)["remote_skip"] == "runner_failed"


def test_nothing_is_cleaned_when_no_run_dir_was_made(env, base, monkeypatch):
    fake = remote(monkeypatch, apps="python3, 1")
    run(env, base)
    assert not any("rm" in c["argv"] for c in fake.calls)


def test_remove_touches_only_a_dated_run_dir_under_the_scratch_folder(base, monkeypatch):
    fake = remote(monkeypatch)
    r = tts_remote.Remote.from_settings(base)
    for bad in (ROOT, f"{ROOT}/", f"{ROOT}/../etc", f"{ROOT}/20261018-0123456789ab/x", "/20261018-0123456789ab",
                f"{ROOT}x/20261018-0123456789ab", f"{ROOT}/20261018-0123456789AB"):
        tts_remote._remove(r, bad)
    assert fake.calls == []
    tts_remote._remove(r, f"{ROOT}/20261018-0123456789ab")
    assert [c["argv"] for c in fake.calls] == [[*tts_remote.SSH, HOST, "rm", "-r", "--", f"{ROOT}/20261018-0123456789ab"]]


def test_a_failing_check_is_bad_results(env, base, monkeypatch):
    """run() hands the stage's evidence check the fetched folder; any failure of that check is bad_results."""
    remote(monkeypatch)
    r = tts_remote.Remote.from_settings(base)

    def check(lines, vd):
        raise ValueError("not proved")

    with pytest.raises(tts_remote.RemoteSkip) as e:
        tts_remote.run(r, LINES, env[0], VOICES_BYTES, "2026-10-18", check)
    assert e.value.args == ("bad_results",) and e.value.log_opened is True


# ---- pruning the scratch folder ----

def test_prune_removes_only_dated_run_dirs_older_than_seven_days(base, monkeypatch):
    calls = []
    monkeypatch.setattr(tts_remote, "_sh", lambda argv, *, timeout, out=None: (
        calls.append(argv), subprocess.CompletedProcess(argv, 0, stdout="", stderr=""))[1])
    assert tts_remote.prune(tts_remote.Remote.from_settings(base)) is True
    argv = calls[0]
    assert argv[:6] == [*tts_remote.SSH, HOST]
    cmd = argv[-1]
    assert "-mtime +7" in cmd and "-maxdepth 1" in cmd and ROOT in cmd and "rm -r --" in cmd


def test_the_remote_log_is_not_renamed_when_this_run_never_opened_it(env, base, monkeypatch):
    (env[0] / "tts.log").write_text("an earlier log\n")
    remote(monkeypatch, apps="python3, 1")
    run(env, base)
    assert (env[0] / "tts.log").read_text() == "an earlier log\n" and not (env[0] / "tts-remote.log").exists()


def test_an_oserror_before_the_fetch_falls_back(env, base, monkeypatch):
    remote(monkeypatch)
    real = Path.unlink

    def unlink(self, *a, **k):
        if self.name == "tts-results.json":
            raise PermissionError("no")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert run(env, base)["tts_target"] == "local" and env[2] == [1]


def test_remote_modules_cover_every_package_module_the_runner_needs():
    here = Path(tts_remote.__file__).resolve().parent
    todo, seen = ["tts_runner"], set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        for node in ast.walk(ast.parse((here / f"{name}.py").read_text())):
            if isinstance(node, ast.ImportFrom) and (node.level == 1 or node.module == "daily"):
                todo += [a.name for a in node.names if (here / f"{a.name}.py").is_file()]
            elif isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                todo.append(node.module)
    assert {f"{n}.py" for n in seen} <= set(tts_remote.MODULES) and "__init__.py" in tts_remote.MODULES


def test_local_runs_do_not_set_hf_offline(env, base, monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    run(env, with_(base, tts_target="local"))
    assert "HF_HUB_OFFLINE" not in os.environ


def test_prune_runs_after_a_success_and_after_a_failure(env, base, monkeypatch):
    fake = remote(monkeypatch)
    run(env, base)
    assert fake.kinds()[-1] == "prune"
    fake = remote(monkeypatch, fail="runner")
    run(env, base)
    assert fake.kinds()[-1] == "prune"
    fake = remote(monkeypatch, apps="python3, 1")  # busy: ssh works, so the prune still runs
    run(env, base)
    assert fake.kinds()[-1] == "prune"


def test_no_prune_when_the_remote_is_unreachable(env, base, monkeypatch):
    fake = remote(monkeypatch, fail="unreachable")
    run(env, base)
    assert "prune" not in fake.kinds()


def test_a_failed_prune_changes_nothing_and_is_logged(env, base, monkeypatch):
    remote(monkeypatch, fail="prune")
    res = run(env, base)
    assert res["tts_target"] == "remote" and env[2] == [] and "remote_skip" not in res
    assert "prune failed" in (env[0] / "tts.log").read_text()


def test_a_prune_that_raises_is_swallowed_and_logged_with_the_remote_log(env, base, monkeypatch):
    fake = remote(monkeypatch, fail="runner")

    def sh(argv, *, timeout, out=None):
        if argv[-1].startswith("find "):
            raise subprocess.TimeoutExpired(argv, timeout)
        return fake(argv, timeout=timeout, out=out)

    monkeypatch.setattr(tts_remote, "_sh", sh)
    assert run(env, base)["remote_skip"] == "runner_failed"
    assert "prune failed" in (env[0] / "tts-remote.log").read_text()


def test_prune_has_its_own_short_timeout_and_targets_only_dated_dirs_under_one_parent(env, base, monkeypatch):
    fake = remote(monkeypatch)
    run(env, base)
    call = next(c for c in fake.calls if c["argv"][-1].startswith("find "))
    assert call["timeout"] == tts_remote.PRUNE_TIMEOUT_S <= 120
    cmd = shlex.split(call["argv"][-1])
    assert cmd[1] == ROOT and cmd[cmd.index("-maxdepth") + 1] == "1" and cmd[cmd.index("-type") + 1] == "d"
    assert cmd[cmd.index("-regex") + 1] == ROOT + "/[0-9]{8}-[0-9a-f]{12}"
    assert cmd[cmd.index("-mtime") + 1] == "+7"
    assert cmd[cmd.index("-exec"):] == ["-exec", "rm", "-r", "--", "{}", "+"]
    assert "-rf" not in cmd and "*" not in cmd


def test_the_prune_regex_matches_only_run_dir_names(base):
    rx = re.compile(tts_remote.prune_pattern(tts_remote.Remote.from_settings(base)))
    assert rx.fullmatch(ROOT + "/20261018-0123456789ab")
    for bad in ("/20261018-0123456789a", "/x20261018-0123456789ab", "/20261018-0123456789AB",
                "/../20261018-0123456789ab", "/20261018-0123456789ab/extra"):
        assert not rx.fullmatch(ROOT + bad)


def test_the_prune_regex_escapes_dots_in_the_scratch_folder():
    """A dot in the folder name is a literal dot to find's -regex, never "any character"."""
    r = tts_remote.Remote(HOST, REMOTE_PY, "/srv/tts.runs/v1.2")
    pattern = tts_remote.prune_pattern(r)
    assert pattern == "/srv/tts\\.runs/v1\\.2/[0-9]{8}-[0-9a-f]{12}"
    rx = re.compile(pattern)  # POSIX extended and Python agree on this subset
    assert rx.fullmatch("/srv/tts.runs/v1.2/20261018-0123456789ab")
    assert not rx.fullmatch("/srv/ttsXruns/v1.2/20261018-0123456789ab")
    assert not rx.fullmatch("/srv/tts.runs/v1x2/20261018-0123456789ab")
