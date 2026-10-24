"""Voicing on a remote GPU box over ssh (settings.toml `tts.host`), with the local runner as the fallback.

The remote host is any machine you can ssh to without a password prompt (BatchMode) that has Chatterbox installed
(`tts.remote_python`) and a folder this pipeline may use for scratch runs (`tts.remote_dir`). A run there:

1. preflight (read only): nvidia-smi over ssh must show at least the runner's free-memory floor and no GPU process
   except the names in `tts.remote_gpu_allow` (a resident model server you are happy to share with, say). Anything
   else on the GPU is someone else's job: the remote is `busy` and is left alone. Nothing is ever paused, unloaded
   or stopped there;
2. a fresh run folder `<remote_dir>/<YYYYMMDD>-<12 hex>` gets lines.json, the exact voices.json bytes the stage
   hashed, and the runner's modules;
3. the runner runs under `timeout` (so a dropped ssh cannot leave it holding the GPU), with CUDA required (it exits
   at once rather than voicing on the remote CPU) and the Hugging Face hub offline (download the model there once);
4. only the wavs and tts-results.json come back, into a voice folder emptied of them first, and the same evidence
   checks as a local run apply (plus: the device must be cuda);
5. the run folder is removed on every outcome once it exists, and dated run folders older than seven days (a crash's
   leftovers) are pruned.

Every problem raises RemoteSkip with a short code from CODES; the voice stage then runs the local runner once (or,
when `tts.target` is "remote", fails with `tts_remote_<code>`). Every subprocess goes through _sh: an argv list, no
local shell, no stdin, a timeout. The remote paths are validated by settings.py to plain characters, so the remote
shell that ssh hands its command to never reads one as syntax."""
from __future__ import annotations

import re
import secrets
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from daily import tts_runner

CODES = ("unreachable", "busy", "low_memory", "rsync_failed", "runner_failed", "bad_results", "no_cuda")
RUN_RE = re.compile(r"\d{8}-[0-9a-f]{12}")
MODULES = ("tts_runner.py", "tts_plan.py", "voiceconf.py", "files.py", "__init__.py")  # what the runner imports
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
RSH = "ssh -o BatchMode=yes -o ConnectTimeout=10"
PREFLIGHT_TIMEOUT_S = 30
COPY_TIMEOUT_S = 600
PRUNE_TIMEOUT_S = 60
PRUNE_DAYS = 7
# A GPU voices a script in a few minutes, so a run still going after this is stuck: fall back rather than wait out the
# local CPU worst case and then voice again. The remote `timeout` ends the runner at this mark.
RUN_TIMEOUT_S = 3000
KILL_AFTER_S = 60
RESULTS = tts_runner.RESULTS
STAGE_DIR = ".remote-stage"


@dataclass(frozen=True)
class Remote:
    """Where and how to voice remotely: the ssh destination, the Chatterbox Python and the scratch folder there, and
    the GPU process names that may share the GPU."""

    host: str
    python: str
    root: str
    gpu_allow: tuple = ()

    @classmethod
    def from_settings(cls, settings) -> "Remote | None":
        host = getattr(settings, "tts_host", None)
        if not host:
            return None
        return cls(host, settings.tts_remote_python, settings.tts_remote_dir.rstrip("/"),
                   tuple(settings.tts_remote_gpu_allow))


class RemoteSkip(Exception):
    """The remote cannot voice this run; args[0] is one of CODES. `log_opened` says whether this run had opened
    voice/tts.log by then."""
    log_opened = False


def _sh(argv: list[str], *, timeout: float, out=None) -> subprocess.CompletedProcess:
    """The one subprocess seam: an argv list, no shell, no stdin, a timeout. With `out` (an open text file) the
    command's stdout and stderr stream into it as it runs; without it they are captured as text."""
    if out is not None:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, timeout=timeout)
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)


def _smi(remote: Remote, query: str) -> list[str]:
    try:
        r = _sh([*SSH, remote.host, "nvidia-smi", f"--query-{query}", "--format=csv,noheader,nounits"],
                timeout=PREFLIGHT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        raise RemoteSkip("unreachable") from None
    if r.returncode != 0:
        raise RemoteSkip("unreachable")
    return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]


def preflight(remote: Remote) -> None:
    """Raise RemoteSkip unless the remote GPU is clear: no GPU process but the allowed ones (any other is another
    job: `busy`) and at least the runner's own free-memory floor on every GPU (`low_memory`). Unreadable output is
    `unreachable`. Read only: it never pauses, unloads or stops anything."""
    free = _smi(remote, "gpu=memory.free")
    apps = _smi(remote, "compute-apps=process_name,used_memory")
    try:
        free_bytes = min(int(v) for v in free) * 1024 ** 2 if free else None
    except ValueError:
        free_bytes = None
    if free_bytes is None:
        raise RemoteSkip("unreachable")
    allowed = {name.lower() for name in remote.gpu_allow}
    for row in apps:
        name = row.rpartition(",")[0].strip().rpartition("/")[2].lower()  # the process's file name, exactly
        if name not in allowed:
            raise RemoteSkip("busy")
    if free_bytes < tts_runner.MIN_FREE_CUDA:
        raise RemoteSkip("low_memory")


def _copy(argv: list[str]) -> None:
    try:
        r = _sh(argv, timeout=COPY_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        raise RemoteSkip("rsync_failed") from None
    if r.returncode != 0:
        raise RemoteSkip("rsync_failed")


def _stage(voice_dir: Path, voices_bytes: bytes, stage: Path) -> None:
    """Lay out what the remote needs under `stage`: lines.json, the voices.json bytes as they are now, and the runner's
    modules under daily/ (so the script path run there imports the same way as here)."""
    (stage / "daily").mkdir(parents=True)
    (stage / "lines.json").write_bytes((voice_dir / "lines.json").read_bytes())
    (stage / "voices.json").write_bytes(voices_bytes)
    here = Path(__file__).resolve().parent
    for name in MODULES:
        (stage / "daily" / name).write_bytes((here / name).read_bytes())


def _is_run_dir(remote: Remote, rd: str) -> bool:
    return rd.startswith(remote.root + "/") and bool(RUN_RE.fullmatch(rd[len(remote.root) + 1:]))


def _remove(remote: Remote, rd: str) -> None:
    """Best effort: remove one validated run folder. A failure here never changes the outcome (a leftover dated folder
    is pruned later)."""
    if not _is_run_dir(remote, rd):
        return
    try:
        _sh([*SSH, remote.host, "rm", "-r", "--", rd], timeout=PREFLIGHT_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - cleanup must not change the result
        pass


def prune_pattern(remote: Remote) -> str:
    """The find -regex (POSIX extended) that matches only dated run folders directly under the scratch folder."""
    return re.sub(r"([.])", r"\\\1", remote.root) + "/[0-9]{8}-[0-9a-f]{12}"


def prune(remote: Remote, days: int = PRUNE_DAYS) -> bool:
    """Remove dated run folders on the remote older than `days` (those a crash left behind). True when the command
    succeeded. Only direct children of the scratch folder whose names are `YYYYMMDD-<12 hex>` are matched."""
    cmd = ["find", remote.root, "-mindepth", "1", "-maxdepth", "1", "-type", "d", "-regextype", "posix-extended",
           "-regex", prune_pattern(remote), "-mtime", f"+{int(days)}", "-exec", "rm", "-r", "--", "{}", "+"]
    try:
        r = _sh([*SSH, remote.host, " ".join(shlex.quote(a) for a in cmd)], timeout=PRUNE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def prune_after(remote: Remote, voice_dir: Path, log_name: str) -> None:
    """Best effort after a remote attempt: prune. A failure of any kind changes nothing and is noted in the named log
    (tts.log after a success, tts-remote.log after a failure)."""
    try:
        ok = prune(remote)
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        try:
            with open(voice_dir / log_name, "a", encoding="utf-8") as fh:
                fh.write("remote prune failed\n")
        except OSError:
            pass


def run(remote: Remote, lines: list[dict], voice_dir: Path, voices_bytes: bytes, day, check) -> dict:
    """Voice on the remote: preflight, stage, run, fetch, check. `check(lines, voice_dir)` is the voice stage's
    evidence check (it raises on a run its results do not prove). Raises RemoteSkip(code) for any remote problem; never
    retried. The local shared lock is not taken (the remote runner takes its own there). The run folder is removed on
    every outcome once it exists."""
    preflight(remote)
    stamp = str(day).replace("-", "")
    rd = f"{remote.root}/{stamp}-{secrets.token_hex(6)}"
    if not _is_run_dir(remote, rd):
        raise RemoteSkip("rsync_failed")
    state = {"log": False}
    try:
        try:
            return _steps(remote, lines, Path(voice_dir), voices_bytes, rd, state, check)
        finally:
            _remove(remote, rd)
    except RemoteSkip as e:
        e.log_opened = state["log"]
        raise


def _steps(remote: Remote, lines, voice_dir: Path, voices_bytes, rd: str, state: dict, check) -> dict:
    stage = voice_dir / STAGE_DIR
    try:
        shutil.rmtree(stage, ignore_errors=True)
        try:
            _stage(voice_dir, voices_bytes, stage)
        except OSError:
            raise RemoteSkip("rsync_failed") from None
        _copy([*SSH, remote.host, "mkdir", "-p", "--", f"{rd}/daily", f"{rd}/out"])
        _copy(["rsync", "-rt", "--no-links", "-e", RSH, "--", f"{stage}/", f"{remote.host}:{rd}/"])
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    log = voice_dir / "tts.log"
    cmd = [*SSH, "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=6", remote.host,
           "timeout", f"--kill-after={KILL_AFTER_S}", str(RUN_TIMEOUT_S),
           "env", "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True", f"{tts_runner.REQUIRE_CUDA_ENV}=1",
           "HF_HUB_OFFLINE=1", remote.python, f"{rd}/daily/tts_runner.py", f"{rd}/lines.json", f"{rd}/out",
           f"{rd}/voices.json"]
    try:
        with open(log, "w", encoding="utf-8", errors="replace") as fh:
            state["log"] = True
            r = _sh(cmd, timeout=RUN_TIMEOUT_S + KILL_AFTER_S + 60, out=fh)
    except (OSError, subprocess.SubprocessError):
        raise RemoteSkip("runner_failed") from None
    if r.returncode == tts_runner.NO_CUDA_EXIT:  # the GPU filled after the preflight; the runner refused the CPU
        raise RemoteSkip("no_cuda")
    if r.returncode != 0 or not runner_ok(log):
        raise RemoteSkip("runner_failed")
    try:  # only what this fetch brings can be read as the results
        (voice_dir / RESULTS).unlink(missing_ok=True)
        for ln in lines:
            (voice_dir / f"{ln['id']}.wav").unlink(missing_ok=True)
    except OSError:
        raise RemoteSkip("rsync_failed") from None
    _copy(["rsync", "-rt", "--no-links", "--include=*.wav", f"--include={RESULTS}", "--exclude=*", "-e", RSH, "--",
           f"{remote.host}:{rd}/out/", f"{voice_dir}/"])
    try:
        results = check(lines, voice_dir)
    except Exception:  # noqa: BLE001 - any evidence failure is bad results here; the local fallback runs next
        raise RemoteSkip("bad_results") from None
    if results.get("device") != "cuda":  # a remote run exists to use its GPU
        raise RemoteSkip("bad_results")
    return results


def runner_ok(log: Path) -> bool:
    """The runner prints RUNNER_OK as its last line; read the log's tail for it."""
    try:
        tail = Path(log).read_bytes()[-4096:].decode("utf-8", errors="replace")
    except OSError:
        return False
    return any(ln.strip() == "RUNNER_OK" for ln in tail.splitlines()[-3:])
