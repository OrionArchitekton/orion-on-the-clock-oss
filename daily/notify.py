"""Notify stage: tell the operator how the day's run ended, as ONE item per (day, outcome).

Where items go is pluggable (settings.toml `notify.command`):
- no command (the default): the item is written to <state>/notify/<item id>.json and printed as one JSON line;
- a command (an argument list, never a shell string): it receives one JSON message on stdin and answers with one JSON
  object on stdout. An "add" message carries the item and must be answered {"item_id": "<id>", "state": "open" |
  "acked" | "expired" | "done"}; an "expire" message names an item id and needs only exit 0. docs/runbook.md has the
  contract and an example that forwards items to a chat webhook or a desktop notification.

Items that ask something of the operator (a ready episode to review, a stop only they can clear) are kind `approval`;
every other outcome is kind `fyi`.

Outcomes (the key is daily:<day>:<outcome>):
- ready (approval): the episode waits PRIVATE in Studio. Pointer is the Studio URL, built from the video id in the
  Studio driver's receipt (stage private_verified, this day); meta carries the episode title (only from the
  gate-verified script), the render evidence (duration, LUFS, one decimal), the contact sheet path and the gate check
  counts.
- published (fyi): public mode put the episode live and verified it (receipt stage public_verified, this day). Nothing
  is asked: the title is "Published: <episode title>" (the gate-verified, cleaned title, or "daily episode <day>" when
  it is unsafe), the pointer is the watch link built from the receipt's video id, and meta carries the render evidence
  and the switch-back note when there is one.
- needs_you (approval): publish stopped for something only you can do (a signed-out browser, another channel, a second
  render). Pointer is the work directory.
- quiet, failed-<stage>, config_error (fyi): pointer is the work directory; stopped items carry the rerun command
  (absolute python3 and run.py paths).
Any agent the ledger marks unavailable is named in the meta by display name.

A day that gets a new outcome supersedes its earlier open items: the new item is added first, then each earlier item
is expired (reason superseded). A failed expire is recorded and retried on the next run; it never blocks the new item.
Returning to an outcome whose item was expired adds a REVISION item (key suffix :r2, :r3, ...). An add answering state
expired or done for a key the receipt never recorded was closed outside this stage (the crash window between add and
receipt): it is recorded closed_outside and left closed, no revision. Any other answer that is not open or acked (or
not JSON) is a failure, never a receipt.

The text is built from fixed phrases, the day, stage names, short reason CODES, agent display names, numbers, the
Studio URL, the work directory and a cleaned episode title. Never ledger text or raw exception text: a reason that is
not a short code is left out, an unsafe episode title is dropped, and no long dash is ever written.

Receipt <work>/notify.json maps each key to {"sent": "ok", "item_id", "at", "expired"?, "expire_failed"?}, written
only after the item was accepted, so a refusal fails the stage (exit 1) and the next run sends it again. Every
read-modify-write of it holds <work>/notify.lock (shared with daily/unit_failed.py).
Reasons: notified: <outcome>, already notified: <outcome>, closed outside: <outcome> (the three paths.NOTIFY_REPORTED
prefixes), dry run: nothing sent, nothing_to_report, settings_missing, upload_receipt_invalid, notify_exit_<n>,
notify_timeout, notify_not_runnable, notify_bad_answer, notify_receipt_unwritable, notify_busy."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

from daily import files, gate, paths
from daily.stage import Ctx, StageResult  # noqa: F401  (Ctx documents run_stage's argument)

RUN_PY = paths.PACKAGE / "run.py"
KEY_PREFIX = "daily"
SCHEMA = "orion-on-the-clock/notify/v1"
RECEIPT = "notify.json"
LOCK = "notify.lock"
SINK_DIR = "notify"   # <state>/notify/: the default sink's items
ORDER = ("collect", "write", "gate", "voice", "render", "publish")
TIMEOUT_S = 60
LOCK_WAIT_S = 90   # the shared notify.json lock (also taken by unit_failed): longer than one notify call
STUDIO_URL = "https://studio.youtube.com/video/{vid}/edit"
WATCH_URL = "https://youtube.com/shorts/{vid}"
ASKS = ("ready", "needs_you")                 # kind approval
STATES = ("open", "acked", "expired", "done")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
CODE = re.compile(r"[A-Za-z0-9_]+(?::[A-Za-z0-9_.-]+){0,2}")
SHORT_CODE = re.compile(r"[a-z0-9_]{1,40}")
UNKNOWN_AGENT = "unknown agent"
TITLE_OK = re.compile(r"[^A-Za-z0-9 .,:;!?'()&%+#_-]")
CHAR_MAP = {0x2013: "-", 0x2014: "-", 0x2015: "-", 0x2018: "'", 0x2019: "'"}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
PHONE = re.compile(r"(?<![\w.])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)")
SEPARATED_DIGITS = re.compile(r"(?:\d[^A-Za-z0-9]?){10}")    # ten digits split by any one separator each
NUMBER_S = re.compile(r"(\d{1,6}(?:\.\d+)?) s\b")
NUMBER_LUFS = re.compile(r"(-?\d{1,4}(?:\.\d+)?) LUFS\b")
EVIDENCE_MAX = 300
SWITCH_NOTE = "the browser was not switched back to the home channel"


def _read(path) -> dict:
    try:
        data = files.read_json(path)
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def outcome(status) -> dict:
    """The run's outcome from status.json: ready, quiet, failed, config_error, needs_you, or incomplete (nothing to
    report yet). The first stage in order that failed, was skipped or went quiet decides."""
    stages = (status or {}).get("stages") if isinstance(status, dict) else None
    if not isinstance(stages, dict):
        return {"outcome": "incomplete", "stage": "", "reason": ""}
    for name in ORDER:
        s = stages.get(name)
        state = s.get("state") if isinstance(s, dict) else None
        if state not in ("failed", "skipped", "quiet"):
            continue
        reason = str(s.get("reason", ""))
        if state == "quiet":
            return {"outcome": "quiet", "stage": name, "reason": reason}
        if name == "collect" and reason.startswith("config:"):
            return {"outcome": "config_error", "stage": name, "reason": reason}
        if name == "publish" and (reason == "needs_you" or reason.startswith("needs_you:")):
            return {"outcome": "needs_you", "stage": name, "reason": reason}
        return {"outcome": "failed", "stage": name, "reason": reason}
    publish = stages.get("publish")
    if isinstance(publish, dict) and publish.get("state") == "done":
        return {"outcome": "ready", "stage": "publish", "reason": ""}
    return {"outcome": "incomplete", "stage": "", "reason": ""}


def _code(reason) -> str:
    """The reason when it is one short code (no spaces, no paths), else '': free text never reaches an item."""
    return reason if isinstance(reason, str) and len(reason) <= 60 and CODE.fullmatch(reason) else ""


def _count(path) -> str:
    checks = _read(path).get("checks")
    checks = checks if isinstance(checks, list) else []
    good = sum(1 for c in checks if isinstance(c, dict) and c.get("pass") is True)
    return f"{good}/{len(checks)}" if checks else "unreadable"


def _evidence(work: Path, name: str, rx, unit: str) -> str:
    """'<number to one decimal> <unit>' from the leading number of the named render check's evidence (render.py writes
    the raw float, e.g. '61.733333 s (timeline ...)'), or '' when the evidence does not start with a number."""
    checks = _read(work / "render.json").get("checks")
    for c in checks if isinstance(checks, list) else []:
        if isinstance(c, dict) and c.get("name") == name and isinstance(c.get("evidence"), str):
            m = rx.match(c["evidence"][:EVIDENCE_MAX])
            return f"{float(m.group(1)):.1f} {unit}" if m else ""
    return ""


def clean_title(raw) -> str | None:
    """A short single-line episode title in a safe alphabet, or None when it holds an email address or a phone
    number or nothing is left. Long dashes become a hyphen, curly apostrophes straight ones; other symbols become
    spaces."""
    if not isinstance(raw, str) or EMAIL.search(raw) or PHONE.search(raw) or SEPARATED_DIGITS.search(raw):
        return None
    text = unicodedata.normalize("NFKC", raw).translate(CHAR_MAP)
    text = " ".join(TITLE_OK.sub(" ", text).split())[:80].rstrip()
    return text if text and not PHONE.search(text) else None


def _episode_title(work: Path):
    """The title of the gate-verified script (the exact bytes the text verdict binds), else None."""
    verdict, script_b, _ledger_b, _why = gate.read_verified_full(work / "verdict.json", work / "script.json",
                                                                  work / "ledger.json", gate.REQUIRED)
    if verdict is None:
        return None
    try:
        script = json.loads(script_b.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return clean_title(script.get("title")) if isinstance(script, dict) else None


def coverage_note(work: Path, display: dict) -> str:
    """The agents the day's ledger marks unavailable, as 'Display Name (reason code); ...' in `display` order (agent id
    -> display name: the built-in readers and your adapters), or '' when every agent was covered or no ledger exists
    (a config error stops collect before it writes one). An id `display` does not know reads 'unknown agent'; a reason
    shows only when it is a short lowercase code, otherwise the reason is dropped."""
    cov = _read(Path(work) / "ledger.json").get("coverage")
    if not isinstance(cov, dict):
        return ""
    known, other = {}, False
    for harness, state in cov.items():
        if state == "ok":
            continue
        if harness not in display:
            other = True
            continue
        if isinstance(state, str) and state.startswith("unavailable"):
            reason = state.partition(":")[2]
            known[harness] = f" ({reason})" if SHORT_CODE.fullmatch(reason) else ""
        else:
            known[harness] = " (unknown state)"
    parts = [display[h] + known[h] for h in display if h in known]
    return "; ".join(parts + ([UNKNOWN_AGENT] if other else []))


def _verified_id(work: Path, day: str, stage: str):
    """The receipt's video id when it is at `stage` for this day, names that stage's visibility, and has a well-formed
    id, else None. Links are always built from this id: a URL field in the receipt is never forwarded."""
    rec = _read(work / "upload.json")
    vid = rec.get("video_id")
    if rec.get("day") != day or rec.get("stage") != stage:
        return None
    # the stage counts only with the visibility it names: public must say PUBLIC; private says PRIVATE or (from before
    # public mode) nothing
    if not (rec.get("visibility") == "PUBLIC" if stage == "public_verified"
            else "visibility" not in rec or rec["visibility"] == "PRIVATE"):
        return None
    return vid if isinstance(vid, str) and VIDEO_ID.fullmatch(vid) else None


def _studio_url(work: Path, day: str):
    vid = _verified_id(work, day, "private_verified")
    return STUDIO_URL.format(vid=vid) if vid else None


def _watch_url(work: Path, day: str):
    vid = _verified_id(work, day, "public_verified")
    return WATCH_URL.format(vid=vid) if vid else None


def _published(oc: dict, work: Path, day: str) -> dict:
    """A ready outcome whose receipt proves a public_verified upload for this day is `published` (the receipt decides,
    not the status text)."""
    return {**oc, "outcome": "published"} if oc["outcome"] == "ready" and _watch_url(work, day) else oc


def _render_numbers(work: Path) -> dict:
    meta = {}
    for name, rx, unit in (("duration", NUMBER_S, "s"), ("loudness", NUMBER_LUFS, "LUFS")):
        value = _evidence(work, name, rx, unit)
        if value:
            meta[name] = value
    return meta


def _contact_sheet(work: Path):
    """The absolute path of the day's contact-sheet.jpg when it is a regular file (not a symlink) directly inside the
    work directory, else None. Look at it from the ready item before opening Studio."""
    root = Path(work).resolve()
    p = root / "contact-sheet.jpg"
    try:
        if p.is_symlink() or not p.is_file() or p.resolve().parent != root:
            return None
    except OSError:
        return None
    return str(p)


def _rerun(day: str, stage: str) -> str:
    return f"{sys.executable} {RUN_PY} --day {day} --from-stage {stage}"


def compose(day, oc: dict, work: Path, display: dict | None = None) -> dict | None:
    """The item for this outcome (None when a ready or published outcome has no verified upload receipt)."""
    d = day.isoformat()
    kind = oc["outcome"]
    key_name = f"failed-{oc['stage']}" if kind == "failed" else kind
    item = {"key": f"{KEY_PREFIX}:{d}:{key_name}", "kind": "approval" if kind in ASKS else "fyi", "outcome": kind,
            "day": d, "severity": None, "rerun": None, "meta": {}, "pointer": str(work)}
    code = _code(oc["reason"])
    if kind == "ready":
        url = _studio_url(work, d)
        if url is None:
            return None
        meta = {}
        episode = _episode_title(work)
        if episode:
            meta["episode"] = episode
        meta.update(_render_numbers(work))
        if _read(work / "upload.json").get("switched_back") is False:
            meta["browser"] = SWITCH_NOTE
        sheet = _contact_sheet(work)
        if sheet:
            meta["contact_sheet"] = sheet
        meta["checks"] = f"text gate {_count(work / 'verdict.json')}, render gate {_count(work / 'render.json')}"
        item.update(pointer=url, meta=meta,
                    title=f"Daily episode {d}: ready in Studio (private), review and publish or delete")
    elif kind == "published":
        url = _watch_url(work, d)
        if url is None:
            return None
        meta = _render_numbers(work)
        if _read(work / "upload.json").get("switched_back") is False:
            meta["browser"] = SWITCH_NOTE
        episode = _episode_title(work)
        item.update(pointer=url, meta=meta, title=f"Published: {episode}" if episode else f"Published: daily episode {d}")
    elif kind == "quiet":
        item.update(severity="fyi", title=f"Daily episode {d}: quiet day, no episode")
    elif kind == "config_error":
        item.update(title=f"Daily episode {d}: config error ({code}), nothing ran" if code
                    else f"Daily episode {d}: config error, nothing ran",
                    rerun=_rerun(d, "collect"))
    elif kind == "needs_you":
        item.update(title=f"Daily episode {d}: needs you before publishing" + (f" ({code})" if code else ""))
    else:
        item.update(title=f"Daily episode {d}: stopped at {oc['stage']}" + (f" ({code})" if code else ""),
                    rerun=_rerun(d, oc["stage"]))
    note = coverage_note(work, display or {})
    if note:
        item["meta"] = {**item["meta"], "unavailable": note}
    return item


# ---- delivery: the operator's command, or the default sink ----

def sink_id(key: str) -> str:
    """The default sink's item id: n- and 12 hex of the key's sha1 (stable, so a resend finds the same file)."""
    return "n-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _sink(message: dict) -> tuple:
    """The default sink: (exit code, answer). An add writes <state>/notify/<id>.json (state open) and prints it as one
    JSON line, unless that file already says expired or done (closed by hand: answered as it is, never reopened); an
    expire marks the file expired."""
    d = paths.state_root() / SINK_DIR
    d.mkdir(parents=True, exist_ok=True)
    if message.get("op") == "add":
        item = message["item"]
        iid = sink_id(item["key"])
        path = d / f"{iid}.json"
        old = _read(path) if path.is_file() else {}
        if old.get("state") in ("expired", "done"):
            return 0, json.dumps({"item_id": iid, "state": old["state"]})
        record = {"item_id": iid, "state": "open", "at": files.now_iso(), **item}
        files.write_json(path, record)
        print(json.dumps(record, ensure_ascii=False))
        return 0, json.dumps({"item_id": iid, "state": "open"})
    iid = message.get("item_id")
    path = d / f"{iid}.json"
    old = _read(path) if ITEM_ID.fullmatch(str(iid)) and path.is_file() else {}
    if old:
        record = {**old, "state": "expired", "reason": message.get("reason"), "expired_at": files.now_iso()}
        files.write_json(path, record)
        print(json.dumps({"item_id": iid, "state": "expired", "reason": message.get("reason")}))
    return 0, "{}"


def send(message: dict, settings, runner=subprocess.run) -> tuple:
    """Deliver one message: (exit code, answer text). With `notify.command` set the command gets the message as JSON on
    stdin (no shell; a timeout); without one the default sink takes it. Raises subprocess.TimeoutExpired or the
    runner's own exception for a command that cannot run."""
    message = {"schema": SCHEMA, **message}
    command = tuple(getattr(settings, "notify_command", ()) or ())
    if not command:
        return _sink(message)
    p = runner(list(command), input=json.dumps(message, ensure_ascii=False), capture_output=True, text=True,
               timeout=TIMEOUT_S)
    return p.returncode, p.stdout


def answer(stdout):
    """(item id or None, state or None) from the JSON an add answered. The state must be one of STATES."""
    try:
        data = json.loads(stdout or "")
    except (ValueError, RecursionError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    iid, state = data.get("item_id"), data.get("state")
    return (iid if isinstance(iid, str) and ITEM_ID.fullmatch(iid) else None,
            state if state in STATES else None)


def _supersede(rec: dict, current: str, settings, runner) -> bool:
    """Expire every other open item of the day recorded in the receipt. True when the receipt changed. A failure is
    recorded (expire_failed) and retried next run; it never raises."""
    changed = False
    for key, entry in rec.items():
        if key == current or not isinstance(entry, dict) or entry.get("sent") != "ok" or entry.get("expired") is True:
            continue
        iid = entry.get("item_id")
        if not isinstance(iid, str) or not ITEM_ID.fullmatch(iid):
            continue
        try:
            ok = send({"op": "expire", "item_id": iid, "key": key, "reason": "superseded"}, settings, runner)[0] == 0
        except Exception:
            ok = False
        if ok:
            entry["expired"] = True
            entry.pop("expire_failed", None)
            changed = True
        elif entry.get("expire_failed") is not True:
            entry["expire_failed"] = True
            changed = True
    return changed


def _live_key(rec: dict, base: str) -> str:
    """The key to use for this outcome: the base key, or the first revision (base:r2, base:r3, ...) whose earlier item
    this receipt does not show as expired."""
    key, n = base, 1
    while isinstance(rec.get(key), dict) and rec[key].get("sent") == "ok" and rec[key].get("expired") is True:
        n += 1
        key = f"{base}:r{n}"
    return key


def run_stage(ctx, runner=subprocess.run) -> StageResult:
    if ctx.dry:
        return StageResult(ok=True, reason="dry run: nothing sent")
    settings = getattr(ctx, "settings", None)
    if settings is None:
        return StageResult(ok=False, reason="settings_missing")
    work = Path(ctx.work)
    try:   # one lock for every notify.json read-modify-write, held across the sends and the atomic write
        with files.file_lock(work / LOCK, LOCK_WAIT_S):
            return _report(ctx, settings, work, runner)
    except files.LockBusy:
        return StageResult(ok=False, reason="notify_busy")


def _report(ctx, settings, work: Path, runner) -> StageResult:
    oc = _published(outcome(_read(work / "status.json")), work, ctx.day.isoformat())
    if oc["outcome"] == "incomplete":
        return StageResult(ok=True, reason="nothing_to_report")
    item = compose(ctx.day, oc, work, settings.harness_display())
    if item is None:
        return StageResult(ok=False, reason="upload_receipt_invalid")
    rec = _read(work / RECEIPT)
    item["key"] = _live_key(rec, item["key"])
    entry = rec.get(item["key"])
    already = isinstance(entry, dict) and entry.get("sent") in ("ok", "closed_outside")
    changed, closed = False, False
    if not already:
        try:
            rc, out = send({"op": "add", "item": item}, settings, runner)
        except subprocess.TimeoutExpired:
            return StageResult(ok=False, reason="notify_timeout")
        except Exception:
            return StageResult(ok=False, reason="notify_not_runnable")
        if rc != 0:
            return StageResult(ok=False, reason=f"notify_exit_{rc}")
        iid, state = answer(out)
        if state in ("open", "acked"):
            new = {"sent": "ok", "at": files.now_iso()}
            if iid:
                new["item_id"] = iid
            rec[item["key"]] = new
            changed = True
        elif state in ("expired", "done"):   # a key this receipt never recorded, closed outside: leave it closed
            rec[item["key"]] = {"sent": "closed_outside", "at": files.now_iso()}
            changed, closed = True, True
        else:
            return StageResult(ok=False, reason="notify_bad_answer")
    changed = _supersede(rec, item["key"], settings, runner) or changed
    if changed:
        try:
            files.write_json(work / RECEIPT, rec)
        except OSError:
            return StageResult(ok=False, reason="notify_receipt_unwritable")
    if closed:
        return StageResult(ok=True, reason=f"{paths.NOTIFY_REPORTED[2]}: {oc['outcome']}")
    return StageResult(ok=True, reason=f"{paths.NOTIFY_REPORTED[1 if already else 0]}: {oc['outcome']}")
