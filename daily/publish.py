"""Publish stage: upload the day's episode to your channel through the YouTube Studio driver (release/studio.py),
PRIVATE unless your off switch turns public mode on.

Public mode: <config_dir>/publish.json must be a regular file that parses as a JSON object whose "visibility" is exactly
"public" (other keys are notes); anything else (missing, unreadable, not an object, any other value, a FIFO or a
device) is private, never an error. The mode is read once per run, recorded in upload-item.json ("visibility") and
passed to the child as `--visibility public|private`; the child never reads the file. A day already private_verified
or public_verified is done whatever the mode: an upload is never flipped.

This is the last gate before your channel, so it checks what `gate.accepts` alone does not:
- the text verdict is judged once on the exact script and ledger bytes (`gate.read_verified_full`), and the title and
  description come from those bytes;
- the render verdict is read once (`gate.read_verified_render`, ledger bound) and those same bytes say stage "render",
  this day, dry false, and the hashes of the script and ledger bytes just judged;
- the disclosure line (settings `show.disclosure`) is one plain line with no long dash, no angle bracket and no control
  character, and it ends every description exactly once;
- what is uploaded is a staging copy of episode.mp4 whose hash equals render.json's subject_sha256;
- a dry run judges render-dry.json against episode-dry.mp4 (dry true there), writes upload-item.json with dry true
  and never starts the child (so never a browser);
- one video per day: a receipt for another render is a needs-you stop, a verified receipt for this render is done;
- the synthetic-content flag is set by the driver always, and the disclosure is always in the description.

The browser work runs as a child process (argv list, stdin DEVNULL, a timeout) so the browser is given back by the
child. A private_verified or public_verified receipt for this day and render with a valid video id makes the stage ok
whatever the child's exit code was (a release problem after the save goes to publish.log as release_problem:<code>).
Child exit codes: 0 done, 1 abort, 2 usage, 3 the browser stayed busy (a leased pool only), 4 needs you. Every reason
is a short code: never child output, a path, a title or an exception text (the child's output goes to publish.log).
The child's `SWITCHED_BACK false` line (the browser could not be put back on your home channel) is logged as
`switched_back:false`; it never changes the result, and the notify stage reads the same fact from the receipt.

Reasons: settings_missing, missing:channel.id, text_not_accepted:<gate code | dry>, script_unreadable, script_quiet,
script_day_mismatch, no_title, bad_disclosure, render_not_accepted:<gate code | stage | day | dry | script_sha256 |
ledger_sha256>, staging_mismatch, needs_you:<receipt_corrupt | receipt_other_day | receipt_other_render |
receipt_invalid | upload_locked | any NEEDS_YOU_CODES entry the child printed>, already_private:<id>,
already_public:<id>, dry_run, uploaded_private:<id>, uploaded_public:<id>, studio_abort, studio_usage, browser_busy,
needs_you, studio_exit_<n>, studio_signal, studio_timeout, studio_not_runnable, studio_receipt_missing,
upload_item_unwritable."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

from daily import files, gate, paths
from daily.settings import SettingsError
from daily.stage import Ctx, StageResult  # noqa: F401  (Ctx documents run_stage's argument)

REPO = paths.REPO
STUDIO = REPO / "release" / "studio.py"
STAGING_NAME = "upload-episode.mp4"
KEEP_ENV = "ORION_CLOCK_KEEP_ON_ABORT"   # the driver keeps the browser after a failure for an attended look; never here
# The driver waits up to 40 min for a leased browser, then uploads; this sits well above both.
UPLOAD_TIMEOUT_S = 90 * 60
RELEASE_TIMEOUT_S = 180
EXIT_REASONS = {1: "studio_abort", 2: "studio_usage", 3: "browser_busy", 4: "needs_you"}
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
LONG_DASHES = (chr(0x2013), chr(0x2014), chr(0x2015))
# the driver's terminal receipt stages -> the reason word for a verified upload
VERIFIED = {"private_verified": "private", "public_verified": "public"}
# The codes the driver prints on its last line `NEEDS_YOU <code>` after an exit 4 (pinned equal by a test).
NEEDS_YOU_CODES = ("signed_out", "wrong_channel", "receipt_corrupt", "receipt_other_day", "receipt_other_render",
                   "receipt_identity_changed", "upload_started_no_video", "receipt_not_resumable", "bad_video_id",
                   "ledger_has_day", "unreadable_ledger", "draft_gone", "visibility_mismatch",
                   "public_unconfirmed")
_NEEDS_LINE = re.compile(r"^NEEDS_YOU ([a-z_]+)$", re.M)
_CLOSE_LINE = re.compile(r"^CLOSED_TAB (true|false)$", re.M)   # the driver's note on the tab it opened
_SWITCH_LINE = re.compile(r"^SWITCHED_BACK (true|false)$", re.M)   # the driver's note on the home channel switch
MODE_FILE = "publish.json"   # in the private config folder: your off switch for public mode


def _stage_agrees(rec: dict) -> bool:
    """A terminal stage counts only with the visibility it names: public_verified needs "PUBLIC"; private_verified
    needs "PRIVATE" or no field at all (a receipt written before public mode)."""
    if rec.get("stage") == "public_verified":
        return rec.get("visibility") == "PUBLIC"
    return "visibility" not in rec or rec["visibility"] == "PRIVATE"


def visibility_mode() -> str:
    """"public" only when <config_dir>/publish.json is a regular file (a symlink to one counts) that parses as a JSON
    object whose "visibility" is exactly "public"; "private" for everything else (missing, unreadable, a FIFO or
    device that could block the read, not an object, any other value). Never raises."""
    try:
        path = paths.config_dir() / MODE_FILE
        if not path.is_file():
            return "private"
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "private"
    return "public" if isinstance(data, dict) and data.get("visibility") == "public" else "private"


def disclosure_ok(text) -> bool:
    """The disclosure line may end a YouTube description: one non-blank line of at most 200 characters with no long
    dash, no angle bracket (YouTube refuses them) and no control, format or line-separator character."""
    return (isinstance(text, str) and bool(text.strip()) and len(text) <= 200
            and not any(c in text for c in LONG_DASHES + ("<", ">"))
            and not any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in text))


def _needs_you_reason(output: str) -> str:
    """needs_you:<code> from the child's last NEEDS_YOU line when the code is in the fixed set, else plain needs_you."""
    found = _NEEDS_LINE.findall(output or "")
    return f"needs_you:{found[-1]}" if found and found[-1] in NEEDS_YOU_CODES else "needs_you"


def _write_bytes(path: Path, data: bytes) -> None:
    """Atomic, fsynced write of the staging copy (a symlink at `path` is replaced, never followed)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def description(raw, disclosure: str) -> str:
    """The script's description with the disclosure exactly once, at the end (repeats in the script are dropped)."""
    desc = raw.replace(disclosure, "").strip() if isinstance(raw, str) else ""
    return f"{desc}\n\n{disclosure}" if desc else disclosure


class UploadLocked(Exception):
    pass


@contextlib.contextmanager
def _upload_lock(work: Path):
    """The driver's own lock (non-blocking flock on <work>/upload.lock, same path and flags), held while this stage
    writes the staging copy and the item so a manual upload mid-flight never has them replaced. It is released before
    the child starts, because the child takes the same lock. The gap is covered by the child, which re-hashes the file
    named by the item when it checks the item and again after handing it to Studio, so a swapped staging copy is
    refused there."""
    fd = os.open(work / "upload.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise UploadLocked() from None
        yield
    finally:
        os.close(fd)


def _receipt(work: Path, day: str, subject: str):
    """A stage result (stop here: done, or a needs-you stop with no browser), or None to go on."""
    p = work / "upload.json"
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return StageResult(ok=False, reason="needs_you:receipt_corrupt")
    if not isinstance(rec, dict):
        return StageResult(ok=False, reason="needs_you:receipt_corrupt")
    if not rec:
        return None
    if rec.get("day") != day:
        return StageResult(ok=False, reason="needs_you:receipt_other_day")
    got = rec.get("subject_sha256")
    if not isinstance(got, str):
        return StageResult(ok=False, reason="needs_you:receipt_invalid")
    if got != subject:
        return StageResult(ok=False, reason="needs_you:receipt_other_render")
    if rec.get("stage") in VERIFIED:   # a finished day is done in either mode: an upload is never flipped
        vid = rec.get("video_id")
        if not isinstance(vid, str) or not VIDEO_ID.fullmatch(vid) or not _stage_agrees(rec):
            return StageResult(ok=False, reason="needs_you:receipt_invalid")
        return StageResult(ok=True, reason=f"already_{VERIFIED[rec['stage']]}:{vid}")
    return None   # a partial upload of this same render: the child resumes it


def _verified_video(work: Path, day: str, subject: str):
    """(video id, "private" or "public") when upload.json proves a private_verified or public_verified upload of this
    render for this day, with the visibility that stage names, else None."""
    try:
        rec = json.loads((work / "upload.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not (isinstance(rec, dict) and rec.get("day") == day and rec.get("subject_sha256") == subject
            and rec.get("stage") in VERIFIED and _stage_agrees(rec)):
        return None
    vid = rec.get("video_id")
    return (vid, VERIFIED[rec["stage"]]) if isinstance(vid, str) and VIDEO_ID.fullmatch(vid) else None


def _run_child(runner, cmd, work: Path, **kw):
    env = {k: v for k, v in os.environ.items() if k != KEEP_ENV}   # unattended always gives the browser back
    return runner(cmd, cwd=str(REPO), capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env, **kw)


def _release(runner, work: Path) -> None:
    """Best effort: give the browser back after a child that could not do it itself. Never raises."""
    try:
        _run_child(runner, [sys.executable, "-u", str(STUDIO), "release"], work, timeout=RELEASE_TIMEOUT_S)
    except Exception:
        pass


def run_stage(ctx, runner=subprocess.run) -> StageResult:
    settings = getattr(ctx, "settings", None)
    if settings is None:
        return StageResult(ok=False, reason="settings_missing")
    work, day = Path(ctx.work), ctx.day.isoformat()
    ledger_path = work / "ledger.json"
    text, script_b, ledger_b, why = gate.read_verified_full(work / "verdict.json", work / "script.json", ledger_path,
                                                            gate.REQUIRED)
    if text is None:
        return StageResult(ok=False, reason=f"text_not_accepted:{why}")
    # A real run needs dry false on the text verdict. A dry rehearsal folder holds either a dry verdict (dry true) or
    # one seeded by copy from the real day (dry false); both are fine there because nothing is uploaded.
    if not isinstance(text.get("dry"), bool) or (text["dry"] and not ctx.dry):
        return StageResult(ok=False, reason="text_not_accepted:dry")
    try:
        script = json.loads(script_b.decode("utf-8"))
    except (ValueError, RecursionError):
        script = None
    if not isinstance(script, dict):
        return StageResult(ok=False, reason="script_unreadable")
    if script.get("quiet") is not False:
        return StageResult(ok=False, reason="script_quiet")
    if script.get("day") != day:
        return StageResult(ok=False, reason="script_day_mismatch")
    title = script.get("title")
    if not isinstance(title, str) or not title.strip():
        return StageResult(ok=False, reason="no_title")
    if not disclosure_ok(settings.disclosure):
        return StageResult(ok=False, reason="bad_disclosure")

    sfx = "-dry" if ctx.dry else ""   # a dry render writes render-dry.json and episode-dry.mp4 (never the real names)
    render, video, render_ledger_b, why = gate.read_verified_render(work / f"render{sfx}.json",
                                                                    work / f"episode{sfx}.mp4",
                                                                    ledger_path, gate.RENDER_REQUIRED)
    if render is None:
        return StageResult(ok=False, reason=f"render_not_accepted:{why}")
    for key, good in (("day", render.get("day") == day),
                      ("dry", render.get("dry") is ctx.dry),
                      ("script_sha256", render.get("script_sha256") == files.sha256_bytes(script_b)),
                      ("ledger_sha256", render.get("ledger_sha256") == files.sha256_bytes(ledger_b)
                       and files.sha256_bytes(render_ledger_b) == files.sha256_bytes(ledger_b))):
        if not good:
            return StageResult(ok=False, reason=f"render_not_accepted:{key}")
    subject = render["subject_sha256"]   # judged equal to the hash of `video`, the very bytes staged below

    staged = work / STAGING_NAME
    mode = visibility_mode()   # read once: the item records it and the child is told it explicitly
    item = {"id": f"daily-{day}", "day": day, "channel": settings.channel_id, "file": str(staged), "sha256": subject,
            "title": title.strip(), "description": description(script.get("description"), settings.disclosure),
            "synthetic": True, "visibility": mode}
    try:
        with _upload_lock(work):
            stop = _receipt(work, day, subject)
            if stop is not None:
                return stop
            if ctx.dry:   # a rehearsal needs no channel: its item is never handed to the driver
                files.write_json(work / "upload-item.json", {**item, "dry": True})
                return StageResult(ok=True, reason="dry_run")
            try:
                settings.require("channel_id")
            except SettingsError as e:
                return StageResult(ok=False, reason=str(e))
            _write_bytes(staged, video)
            if staged.is_symlink() or files.sha256_bytes(staged.read_bytes()) != subject:
                return StageResult(ok=False, reason="staging_mismatch")
            files.write_json(work / "upload-item.json", item)
    except UploadLocked:
        return StageResult(ok=False, reason="needs_you:upload_locked")
    except OSError:
        return StageResult(ok=False, reason="upload_item_unwritable")

    cmd = [sys.executable, "-u", str(STUDIO), "upload", str(work), "--visibility", mode]
    try:
        p = _run_child(runner, cmd, work, timeout=UPLOAD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _release(runner, work)   # a killed child cannot give the browser back itself
        return StageResult(ok=False, reason="studio_timeout")
    except Exception:
        _release(runner, work)
        return StageResult(ok=False, reason="studio_not_runnable")
    except BaseException:   # SIGTERM (run.py's RunTerminated) or Ctrl-C: subprocess.run killed the child mid-lease
        _release(runner, work)
        raise
    rc = p.returncode
    verified = _verified_video(work, day, subject)   # positive evidence beats the exit code (a dirty release after a save)
    problem = None
    if rc != 0:
        problem = "studio_signal" if (rc < 0 or rc == 143) else EXIT_REASONS.get(rc, f"studio_exit_{rc}")
        if rc == 4:
            problem = _needs_you_reason((p.stdout or "") + "\n" + (p.stderr or ""))
    try:
        with open(work / "publish.log", "a", encoding="utf-8") as log:
            log.write((p.stdout or "") + (p.stderr or ""))
            if problem is not None and verified is not None:
                log.write(f"\nrelease_problem:{problem}\n")
            switched = _SWITCH_LINE.findall(p.stdout or "")
            if switched and switched[-1] == "false":   # the browser was left on the upload channel (notify reads the receipt)
                log.write("\nswitched_back:false\n")
            closed = _CLOSE_LINE.findall(p.stdout or "")
            if closed and closed[-1] == "false":
                log.write("\nclosed_tab:false\n")
    except OSError:
        pass
    if verified is not None:
        vid, kind = verified
        return StageResult(ok=True, reason=f"uploaded_{kind}:{vid}")
    if problem is not None:
        return StageResult(ok=False, reason=problem)
    return StageResult(ok=False, reason="studio_receipt_missing")
