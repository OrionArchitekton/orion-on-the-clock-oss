#!/usr/bin/env python3
"""The daily build orchestrator.

python3 -m daily.run --day due|yesterday|YYYY-MM-DD [--until STAGE] [--from-stage STAGE] [--dry]

Exit codes: 0 done or quiet, 1 a stage failed (or needed the operator, or the config was bad) and was NOT reported, or
the settings could not be loaded, or the lock could not be taken (lock_failed), 2 usage error, 3 the lock is held by
another run, 5 a stage failed (or needed the operator, or the config was bad) and the notify stage reported it. A
systemd unit treats 3 and 5 as success, so its OnFailure handler means only "died before reporting".

settings.toml is loaded first (daily.settings), before the day is chosen, because the day is a day in the settings'
time zone. Settings that are missing or invalid print one line to stderr and exit 1 before anything else runs.

The stage order is paths.STAGE_ORDER; MODULES says which module runs each stage, and a module is imported only when its
stage runs, so a stage whose module is not installed yet is recorded `skipped` and fails the run (fail closed).
status.json (in the day's work directory) records one entry per stage; a rerun skips stages already `done`. A `dry` run
records `dry`, which a real run redoes. A `quiet` day is terminal until `--from-stage` names that stage or an earlier
one. The notify stage runs last after every outcome (done, quiet, failed, config error) when the run goes all the way
to it, so `ctx.cfg` may be None there.

Every reason is untrusted text (a stage's own reason, an exception, a config error). `sanitize_reason` runs on each one
before status.json is written, and so before the notify stage reads it. An exception records only its class name.

The lock is a kernel lock: `<work>/run.lock` is opened (created if missing, never deleted) and flock'd exclusively
without blocking, and held for the whole run. The kernel drops it when the process ends, even on SIGKILL or a WSL
shutdown, so a lock cannot go stale and nothing is ever reclaimed. A run that finds it held exits 3; one that cannot
open or lock the file at all exits 1 (lock_failed). After locking, the
owner's pid and start time are written into the file for diagnostics only; they are cleared when the run ends and
nothing reads them.

SIGTERM raises RunTerminated (a BaseException, so a stage's own `except Exception` cannot swallow it). The stage that was
running is recorded failed with `signal:SIGTERM`, the notify stage still runs, and the exit code is 1.

Running a stage first removes the status entries of every later stage (a config error does the same for collect), so a
rerun can never skip a stage whose inputs or files an earlier rerun has replaced.

A `--dry` run works in its own directory, `<state>/<day>-dry/` (paths.work_dir(day, dry=True)), with its own
status.json, run.lock, receipts and archives; it never creates, changes or deletes anything under `<state>/<day>/`.
`--dry --from-stage <voice or later>` on a dry directory that lacks the gated text first seeds the text by COPY from the
real day (see _seed_dry; the real voice/ is never copied, so a rehearsal from render voices silence first); without a
passing real text verdict it starts at collect in the dry directory.

The terminal stays quiet: only a usage error, a settings error or a failed lock prints (to stderr), and the notify
stage's default sink prints the run's one item (as a JSON line, to stdout) when no notify command is set.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import importlib
import json
import os
import signal
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daily import paths  # noqa: E402
from daily.config import ConfigError, load_config  # noqa: E402
from daily.day import parse_day, use_timezone, zone  # noqa: E402
from daily.files import now_iso, read_json, write_json  # noqa: E402
from daily.ledger import path_or_url  # noqa: E402  (the ledger's path rule: one definition of a path-like token)
from daily.settings import SettingsError, load_settings  # noqa: E402
from daily.stage import Ctx, StageResult  # noqa: E402,F401  (re-exported; defined in daily.stage)

NOTIFY = "notify"
EXIT_FAILED_REPORTED = 5  # a stage failed, needed the operator or the config was bad, and the notify stage reported it
REASON_MAX = 160
MODULES = {
    "collect": "daily.collect",
    "write": "daily.writer",
    "gate": "daily.gate",
    "voice": "daily.voice",
    "render": "daily.render",
    "publish": "daily.publish",
    "notify": "daily.notify",
}
STAGES: list = [(name, MODULES[name]) for name in paths.STAGE_ORDER]
NAMES = [name for name, _ in STAGES]


def load_stage(module_name: str):
    """The stage module, or None when that module is not installed yet (later plans add it)."""
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        if e.name == module_name:
            return None
        raise


def sanitize_reason(reason) -> str:
    """A reason as one line of printable ASCII, at most REASON_MAX characters, with every path-like token (the
    ledger's rule: a URL, a home, temp or mount folder, a drive path, two or more slashes) replaced by "<path>".
    NFKC first, so a fullwidth slash reads as a slash; whitespace of any kind becomes a space; control, zero-width and
    other non-ASCII characters drop before the path check, so none can hide a path from it."""
    if not isinstance(reason, str):
        return ""
    spaced = "".join(" " if c.isspace() else c for c in unicodedata.normalize("NFKC", reason))
    tokens = "".join(c for c in spaced if " " <= c <= "~").split()
    return " ".join("<path>" if path_or_url(t) else t for t in tokens)[:REASON_MAX].rstrip()


def _status(work: Path, day: dt.date) -> dict:
    p = work / "status.json"
    try:
        data = read_json(p) if p.exists() else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("day") != day.isoformat() or not isinstance(data.get("stages"), dict):
        data = {"day": day.isoformat(), "stages": {}}
    return data


def _state(status: dict, name: str):
    entry = status["stages"].get(name)
    return entry.get("state") if isinstance(entry, dict) else None


def _record(work: Path, status: dict, name: str, state: str, reason: str = "") -> None:
    status["stages"][name] = {"state": state, "reason": sanitize_reason(reason), "at": now_iso()}
    write_json(work / "status.json", status)


def _parse(argv):
    ap = argparse.ArgumentParser(prog="python3 -m daily.run", description="The daily build orchestrator.")
    ap.add_argument("--day", required=True, help="due (the day whose run was due at the last 05:30 local time), "
                    "yesterday, or YYYY-MM-DD (a day in the settings' time zone)")
    ap.add_argument("--until", choices=NAMES, default=NOTIFY)
    ap.add_argument("--from-stage", choices=NAMES)
    ap.add_argument("--dry", action="store_true")
    return ap.parse_args(argv)


class RunTerminated(BaseException):
    """SIGTERM arrived. A BaseException like KeyboardInterrupt, so no `except Exception` in a stage swallows it."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


def _on_sigterm(signum, frame):
    raise RunTerminated(signal.Signals(signum).name)


def _take_lock(lock: Path) -> int | None:
    """The open lock file with an exclusive flock held (closing it releases the lock), or None when another run holds
    it. Creates the file when missing and never deletes it. An error other than "held" (the path cannot be opened or
    locked) is raised as OSError."""
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)  # not inheritable: a child process never keeps the lock alive
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    try:  # diagnostics for a person; the lock is the flock, never this text
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(), "started": now_iso()}).encode("utf-8"))
    except OSError:
        pass
    return fd


def main(argv=None, today: dt.date | None = None, now: dt.datetime | None = None) -> int:
    args = _parse(argv)
    try:
        settings = load_settings(paths.config_dir())
        use_timezone(settings.timezone)
    except SettingsError as e:
        print(f"settings error: {e} ({paths.config_dir() / 'settings.toml'}; see config.example/settings.toml)",
              file=sys.stderr)
        return 1
    try:
        day = parse_day(args.day, today or dt.datetime.now(zone()).date(), now)
    except ValueError:
        print(f"bad --day: {args.day}", file=sys.stderr)
        return 2
    work = paths.work_dir(day, dry=args.dry)
    if args.dry:  # a symlinked dry folder must not lead into the real day (or into anything inside it)
        real = paths.work_dir(day).resolve()
        mine = work.resolve()
        if mine == real or real in mine.parents:
            print("dry_dir_is_real", file=sys.stderr)
            return 1
    work.mkdir(parents=True, exist_ok=True)
    lock = work / "run.lock"
    try:
        fd = _take_lock(lock)
    except OSError as e:
        print(f"lock_failed: {lock}: {e.strerror}", file=sys.stderr)
        return 1  # 3 means only "another run holds it"
    if fd is None:
        print(f"lock held: {lock}", file=sys.stderr)
        return 3
    if not args.dry:   # the OnFailure handler files its alert under the day THIS run selected, not a recomputed one
        try:
            write_json(paths.last_run_path(), {"day": day.isoformat(), "started_at": now_iso(), "pid": os.getpid()})
        except OSError:
            pass
    try:
        try:
            previous = signal.signal(signal.SIGTERM, _on_sigterm)
            installed = True
        except ValueError:  # not the main thread: no handler, the default action stays
            installed = False
        try:
            return _run(args, day, work, settings)
        finally:
            if installed:
                signal.signal(signal.SIGTERM, signal.SIG_DFL if previous is None else previous)
    finally:
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        os.close(fd)  # releases the flock; the file stays


def _invoke(module_name: str, ctx: Ctx) -> StageResult | None:
    """The stage's result, None when its module is not installed. A module that fails to import, a stage that raises
    and a result that is not a StageResult are all failed stages (the reason is the exception's class name only)."""
    try:
        mod = load_stage(module_name)
        if mod is None:
            return None
        res = mod.run_stage(ctx)
    except RunTerminated as e:
        return StageResult(ok=False, reason=f"signal:{e.name}")
    except Exception as e:
        return StageResult(ok=False, reason=f"exception:{type(e).__name__}")
    return res if isinstance(res, StageResult) else StageResult(ok=False, reason="bad_result")


def _forget_after(status: dict, name: str) -> bool:
    """Drop the entries of every stage after `name` (in memory); True when there were any."""
    later = [n for n in NAMES[NAMES.index(name) + 1:] if n in status["stages"]]
    for n in later:
        del status["stages"][n]
    return bool(later)


def _pending(status: dict, until: int) -> str | None:
    """The first stage before notify, up to `until`, that is not done: the one a run is working on or about to start."""
    return next((n for n in NAMES[: until + 1] if n != NOTIFY and _state(status, n) != "done"), None)


SEED_RECORDS = ("writer-run.json", "writer-prompt.txt")  # the writer's record files, copied beside the gated text


def _put_bytes(dst: Path, data: bytes) -> None:
    """Write `data` to `dst` through a temp file and os.replace; a symlink at `dst` is removed first, never followed."""
    if dst.is_symlink():
        dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".seed.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dst)


def _copy_file(src: Path, dst: Path) -> None:
    """A copy of src's CONTENT (a symlink source is read through; a symlink destination is replaced, never followed)."""
    _put_bytes(dst, src.read_bytes())


def _copy_tree(src: Path, dst: Path) -> None:
    """Merge src's content into dst: files copied (never linked), existing unrelated entries kept, a symlinked
    destination folder or file replaced by a real one. The real folder is only read."""
    if dst.is_symlink():
        dst.unlink()
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        if entry.is_dir():
            _copy_tree(entry, dst / entry.name)
        else:
            _copy_file(entry, dst / entry.name)


def _dry_voice_ready(dry_work: Path) -> bool:
    """True when the dry folder holds a dry voicing (voice/lines.json and every line's words marked dry, the only
    words a dry build takes: compose.voiced_lines, lazily, as every stage module is). Anything else is not reused."""
    if not (dry_work / "voice" / "lines.json").is_file():
        return False
    from daily import compose
    try:
        compose.voiced_lines(dry_work, dry=True)
    except (OSError, ValueError, LookupError, TypeError):
        return False
    return True


def _seed_dry(day: dt.date, dry_work: Path, status: dict, from_stage: str | None) -> None:
    """Prepare a media rehearsal (`--dry --from-stage voice` or later) so it never redoes the writer.

    If the dry folder already holds a text verdict, script and ledger that pass as one read, the stages before
    `from_stage` count as done in the dry status (a dry run records them as `dry`, which would rerun the writer), and so
    does voice when the folder holds a dry voicing (_dry_voice_ready). Otherwise the text is seeded by copy from the
    real day: only a real day whose text verdict passes as one read and is not itself a dry verdict is used. The script
    and ledger are written from the very bytes that judgement returned and the verdict from its parse; sources/ and
    the writer record files are copied as content, merged into the dry folder, never through a symlink. The real
    voice/ is never copied: a dry build takes only dry words, so a rehearsal from render or later voices silence in its
    own folder first (voice is left undone and runs dry), and the real takes stay where they are. A copy that fails part
    way marks nothing done: the run starts at collect and replaces it. Without a passing real verdict nothing is seeded
    and the run starts at collect in the dry directory (the writer is never called for the real directory). The real
    directory is only read; the stages after still re-verify the bytes (gate.read_verified), as on any run."""
    if not from_stage or NAMES.index(from_stage) <= NAMES.index("gate"):
        return
    from daily import gate  # lazy, like every stage module
    after_voice = NAMES.index(from_stage) > NAMES.index("voice")
    mine = gate.read_verified_full(dry_work / "verdict.json", dry_work / "script.json", dry_work / "ledger.json",
                                   gate.REQUIRED)
    if mine[0] is not None:  # already seeded or built here: reuse it
        for name in ("collect", "write", "gate"):
            status["stages"][name] = {"state": "done", "reason": "reused", "at": now_iso()}
        if after_voice and _state(status, "voice") in ("dry", "done") and _dry_voice_ready(dry_work):
            status["stages"]["voice"] = {"state": "done", "reason": "reused", "at": now_iso()}
        else:
            status["stages"].pop("voice", None)  # no dry voicing here (or real words): voice runs dry before render
        write_json(dry_work / "status.json", status)
        return
    real = paths.work_dir(day)
    text, script_b, ledger_b, _why = gate.read_verified_full(real / "verdict.json", real / "script.json",
                                                            real / "ledger.json", gate.REQUIRED)
    if text is None or text.get("dry") is not False:
        return
    seeded = ["collect", "write", "gate"]
    try:
        _put_bytes(dry_work / "script.json", script_b)
        _put_bytes(dry_work / "ledger.json", ledger_b)
        _put_bytes(dry_work / "verdict.json", json.dumps(text, indent=2, sort_keys=True).encode("utf-8"))
        for name in SEED_RECORDS:
            if (real / name).is_file():
                _copy_file(real / name, dry_work / name)
        if (real / "sources").is_dir():
            _copy_tree(real / "sources", dry_work / "sources")
    except OSError:
        return  # a half copy is never marked done: the run starts at collect and replaces it
    for name in seeded:
        status["stages"][name] = {"state": "done", "reason": "seeded", "at": now_iso()}
    status["stages"].pop("voice", None)  # whatever voiced here before belonged to other text: voice runs dry again
    write_json(dry_work / "status.json", status)


def _run(args, day: dt.date, work: Path, settings=None) -> int:
    status = _status(work, day)
    until = NAMES.index(args.until)
    if args.from_stage:
        for name in NAMES[NAMES.index(args.from_stage):]:
            status["stages"].pop(name, None)
        write_json(work / "status.json", status)
    if args.dry:
        _seed_dry(day, work, status, args.from_stage)
    cfg, config_error = None, None
    try:
        cfg = load_config(paths.config_dir())
    except ConfigError as e:
        config_error = "config:" + str(e).partition(":")[0]  # the code, never the detail
    except RunTerminated as e:
        config_error = f"signal:{e.name}"
    except Exception as e:
        config_error = f"exception:{type(e).__name__}"
    ctx = Ctx(day=day, work=work, cfg=cfg, dry=args.dry, settings=settings)
    outcome, current = "done", None
    if config_error:
        _forget_after(status, "collect")
        _record(work, status, "collect", "failed", config_error)
        outcome = "failed"
    elif any(_state(status, name) == "quiet" for name in NAMES):
        outcome = "quiet"
    try:
        if outcome == "done":
            for name, module_name in STAGES[: until + 1]:
                if name == NOTIFY:
                    break
                if _state(status, name) == "done":
                    continue
                current = name
                if _forget_after(status, name):  # what this stage may replace is no longer done, even if we are cut off
                    write_json(work / "status.json", status)
                res = _invoke(module_name, ctx)
                if res is None:
                    _record(work, status, name, "skipped", "not_installed")
                    outcome = "failed"
                    break
                if not res.ok:  # before quiet: a result that is both failed and quiet is a failure
                    _record(work, status, name, "failed", res.reason)
                    outcome = "failed"
                    break
                # a dry stage is recorded as "dry", never "done" (or "quiet"), so a later real run does not skip it
                if res.quiet:
                    _record(work, status, name, "dry" if ctx.dry else "quiet", res.reason)
                    outcome = "quiet"
                    break
                _record(work, status, name, "dry" if ctx.dry else "done", res.reason)
                current = None
    except RunTerminated as e:  # SIGTERM outside a stage's own code: between stages or while writing status.json
        name = current or _pending(status, until)
        if name:
            _forget_after(status, name)
            _record(work, status, name, "failed", f"signal:{e.name}")
        outcome = "failed"
    if NAMES[until] == NOTIFY:
        res = _invoke(dict(STAGES)[NOTIFY], ctx)
        if res is None:
            _record(work, status, NOTIFY, "skipped", "not_installed")  # no notify module at all: not a failure
        else:
            _record(work, status, NOTIFY, ("dry" if ctx.dry else "done") if res.ok else "failed", res.reason)
            if not res.ok:
                outcome = "failed"  # a report that never reached the operator is a failed stage: exit 1, rerun reports again
            elif outcome == "failed" and not ctx.dry and res.reason.partition(":")[0] in paths.NOTIFY_REPORTED:
                return EXIT_FAILED_REPORTED  # the inbox already has it: the unit's OnFailure item would be a duplicate
    return 0 if outcome in ("done", "quiet") else 1


if __name__ == "__main__":
    sys.exit(main())
