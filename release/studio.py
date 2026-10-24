#!/usr/bin/env python3
"""Upload ONE daily episode to your channel (settings.toml channel.id), PRIVATE or PUBLIC, fail closed, through a
signed-in Chrome on this machine driven over its DevTools endpoint. Run unattended by the publish stage.

Private or public, never unlisted or scheduled, in code: VISIBILITIES is exactly (PRIVATE, PUBLIC). The visibility
comes only from the explicit `--visibility private|public` argument (absent: PRIVATE); no config file or environment
variable is read for it. One setter refuses anything else before the browser; one evaluated script proves the chosen
value alone (the other two choices false, no schedule picker, an enabled button reading exactly Save for private or
Publish for public) and only then presses it; a public run then closes the "Video published" dialog by its own Close
button. No scheduler, and nothing typed into a sign-in page. A private upload is not a post: you make it public
yourself after watching it. A public one (public mode) needs an exact Public label on a fresh read and an oEmbed 200
whose body is oEmbed for this video within OEMBED_TRIES tries OEMBED_WAIT_S apart; any stop after the save attempt is
exit 4 public_unconfirmed, with no second save. A draft keeps the visibility it began with (exit 4
visibility_mismatch otherwise). A signed-out browser or Studio on another channel stops with exit 4 for you.

The browser: settings.toml browser.endpoint, a Chrome DevTools endpoint on the loopback only (anything else is
refused: a DevTools port is full control of the signed-in browser). With browser.lease and browser.release set, the
browser is leased from a pool instead (the lease command answers {"ok": true, "data": {"endpoint": ...}}, or
{"ok": false, "code": "BUSY"} while another holder has it). The driver attaches agent-browser
(https://github.com/vercel-labs/agent-browser) under a session of its own, in a socket folder of its own, and proves
it drives a headed Chrome that does not report navigator.webdriver. It never closes the browser itself: when it is
done it stops its own agent-browser session, and with a pool it gives the lease back. Built on the tab-pinned
primitives in release/pinned.py (same folder).

Usage (from the repository root):
  python3 release/studio.py preflight                    open my own Studio tab and prove the channel
  python3 release/studio.py upload <work-dir> [--visibility private|public]
                                                         upload <work>/upload-item.json, then verify
  python3 release/studio.py verify <work-dir> [--visibility private|public]
                                                         read only: fresh Studio read + oEmbed of the receipt
  python3 release/studio.py look                         read-only discovery of the open upload dialog
  python3 release/studio.py release                      give the browser back (after a kept abort)
The upload opens a NEW tab of its own, marked at creation, on the channel's Studio home (exit 4 wrong_channel if
Studio does not land there), and acts only in that tab. With channel.home_id set it afterwards puts that tab on the
home channel, printing SWITCHED_BACK true|false and recording switched_back in the receipt, then closes its tab,
printing CLOSED_TAB true|false. Neither changes the exit code. A channel read that comes back empty gets one re-open
of that home first. When the switch-in read still fails (empty after the re-open, or another channel), a screenshot
of this run's own tab goes to <state>/_studio-stops/ (local only, newest 20 kept) before the stop, printing
`studio-stop screenshot: <path>` or `... none (<reason>)`.
Receipt: <work>/upload.json (a rerun resumes the same draft and never uploads a second copy). The work dir is named
for the day (YYYY-MM-DD) and the whole command holds <work>/upload.lock.
Receipt stages: upload_started -> uploaded -> details -> save_clicked -> saved -> private_verified or public_verified
(terminal: a rerun is a no-op in either mode, so an upload is never flipped). save_clicked is written just before
the done-button press is sent: a rerun from it or from saved never reopens the wizard or presses again, it only
verifies. The receipt and the uploads ledger (<state>/uploads/ledger.jsonl) name the visibility; a terminal stage
counts only with its own (public_verified must say PUBLIC; private_verified says PRIVATE or nothing).
Exit codes: 0 done, 1 abort (fail closed; also a held upload.lock or bad settings), 2 usage, 3 the browser stayed
busy past the wait (a leased pool only), 4 needs you (signed out, Studio on another channel, a public upload oEmbed
never confirmed, or a receipt that is corrupt, for another day or render, begun with another visibility, or a started
upload with no video id: the receipt stops all come before any browser). ORION_CLOCK_KEEP_ON_ABORT=1 keeps the
browser after a failure so an attended `look` can read the dialog; finish with `release`.
"""
import base64
import contextlib
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import pinned as H  # noqa: E402
from daily import paths  # noqa: E402
from daily.settings import LOOPBACK_ENDPOINT, SettingsError, load_settings  # noqa: E402

STUDIO_CHANNEL = "https://studio.youtube.com/channel/"   # the channel homes are this plus a channel id from settings
# Set by configure() from settings.toml before any command runs.
CHAN = None              # channel.id: the channel every upload goes to and every guard pins
CHANNEL_NAME = None      # channel.name (or the id): for messages only
HOME_CHAN = None         # channel.home_id: the channel to put the browser back on afterwards; None skips the switch
DAILY_HOME_URL = None    # the upload channel's Studio home (switch in)
HOME_URL = None          # the home channel's Studio home (switch back), when one is set
ENDPOINT = None          # browser.endpoint
LEASE_CMD = ()           # browser.lease (argv); empty: attach to ENDPOINT directly
RELEASE_CMD = ()         # browser.release (argv)
SESSION_PREFIX = "oclock-"   # agent-browser session names of mine, in my own socket folder
SESSION_RE = re.compile(r"oclock-[0-9a-f]{8}")
UNATTACHED = "oclock-unattached"   # nothing is attached to this name: a browser call before attach drives nothing real
BUSY_WAIT_S = 40 * 60
BUSY_POLL_S = 60
POOL_CALL_TIMEOUT_S = 120
DEFAULT_VISIBILITY = "PRIVATE"   # the fail-closed default: an upload is private unless the argument says otherwise
VISIBILITIES = (DEFAULT_VISIBILITY, "PUBLIC")   # the only two values this driver ever saves; never unlisted or scheduled
VERIFIED = {"PRIVATE": "private_verified", "PUBLIC": "public_verified"}   # terminal receipt stage and ledger event
TERMINAL = tuple(VERIFIED.values())
DONE_LABELS = {"PRIVATE": "Save", "PUBLIC": "Publish"}   # the exact done-button label Studio shows for each value
SAVE_CLICKED = "save_clicked"   # receipt stage written just before the done-button press is sent
LIMIT_TITLE, LIMIT_DESC = 100, 5000
LONG_DASHES = (chr(0x2013), chr(0x2014), chr(0x2015))
EXIT_ABORT, EXIT_USAGE, EXIT_BUSY, EXIT_NEEDS_YOU = 1, 2, 3, 4
ARROW = chr(0x2192)

H.BACKEND = "endpoint"
H.S = UNATTACHED
D = H.DIALOG
_LEASED = False        # holding the browser (leased, or attached directly): release() must run
_LOCK_FD = None
_MARK = None           # the window.name mark of that tab: the only way it is ever found again (never by URL)
_OPENED_TAB = False    # this run opened its own Studio tab (never set for a tab it found): it closes that one tab
_SWITCHED_IN = False   # set once my tab has been put on the upload channel: the browser then needs switching back


def die_code(code, step, why):
    print(f"ABORT at {step}: {why}")
    sys.exit(code)


# Every exit 4 ends with one machine-readable last line, NEEDS_YOU <code>, printed by main() after any release output
# (daily/publish.py parses it). The code comes from this fixed set; an unlisted one is printed as plain needs_you.
NEEDS_YOU_CODES = ("signed_out", "wrong_channel", "receipt_corrupt", "receipt_other_day", "receipt_other_render",
                   "receipt_identity_changed", "upload_started_no_video", "receipt_not_resumable", "bad_video_id",
                   "ledger_has_day", "unreadable_ledger", "draft_gone", "visibility_mismatch", "public_unconfirmed",
                   "needs_you")
_NEEDS_CODE = ["needs_you"]


def needs_you(step, why, code="needs_you"):
    _NEEDS_CODE[0] = code if code in NEEDS_YOU_CODES else "needs_you"
    print(f"NEEDS YOU at {step}: {why}")
    sys.exit(EXIT_NEEDS_YOU)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- configuration (settings.toml) ----------

def configure(s):
    """Take the channel, the browser and the optional lease from a loaded daily.settings.Settings. A missing
    channel.id raises SettingsError(missing:channel.id); nothing here falls back to another channel."""
    global CHAN, CHANNEL_NAME, HOME_CHAN, DAILY_HOME_URL, HOME_URL, ENDPOINT, LEASE_CMD, RELEASE_CMD
    CHAN = s.require("channel_id")
    CHANNEL_NAME = s.channel_name or CHAN
    HOME_CHAN = s.home_channel_id if s.home_channel_id and s.home_channel_id != CHAN else None
    DAILY_HOME_URL = STUDIO_CHANNEL + CHAN
    HOME_URL = STUDIO_CHANNEL + HOME_CHAN if HOME_CHAN else None
    ENDPOINT = s.browser_endpoint
    LEASE_CMD, RELEASE_CMD = tuple(s.browser_lease), tuple(s.browser_release)
    H.CHAN = CHAN
    H.ENV["AGENT_BROWSER_SOCKET_DIR"] = str(socket_dir())


def load_config():
    """configure() from <config_dir>/settings.toml, or exit 1 with the settings code (never a value)."""
    try:
        configure(load_settings())
    except SettingsError as e:
        H.die("settings", str(e))


# ---------- pure rules (tested without a browser) ----------

DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
ITEM_FIELDS = ("id", "channel", "file", "sha256", "title", "description")


class ReceiptError(Exception):
    """A receipt or lock refusal. `code` is a short code (no paths, no text from the page)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def check_item(item, day):
    """Problems with an upload item for `day`; an empty list means it may be uploaded."""
    if not isinstance(day, str) or not DAY_RE.fullmatch(day):
        return ["expected day is not YYYY-MM-DD"]
    if not isinstance(item, dict):
        return ["item is not an object"]
    p = [f"{k} missing" for k in ITEM_FIELDS if not isinstance(item.get(k), str)]
    if p:
        return p
    if item["id"] != f"daily-{day}":
        p.append(f"id {item['id']!r} is not daily-{day}")
    if "day" in item and item["day"] != day:
        p.append(f"item day {item['day']!r} is not {day}")
    if not CHAN or item["channel"] != CHAN:
        p.append(f"channel {item['channel']} is not {CHAN} ({CHANNEL_NAME})")
    f = Path(item["file"])
    if f.is_symlink():
        p.append("file is a symlink")
    elif not f.is_file():
        p.append(f"file {f} missing")
    elif sha256_file(f) != item["sha256"]:
        p.append("file sha256 differs from the item (re-rendered after the item was written?)")
    t, d = item["title"], item["description"]
    if not t.strip():
        p.append("title is blank")
    if len(t) > LIMIT_TITLE:
        p.append(f"title length {len(t)} over {LIMIT_TITLE}")
    nbytes = len(d.encode("utf-8", "replace"))
    if nbytes > LIMIT_DESC:
        p.append(f"description is {nbytes} bytes, over {LIMIT_DESC}")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in t):
        p.append("title has a control, format or line-separator character")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") and c not in "\n\r\t" for c in d):
        p.append("description has a control or format character")
    for name, text in (("title", t), ("description", d)):
        if "<" in text or ">" in text:
            p.append(f"{name} has an angle bracket (YouTube refuses them)")
        if any(c in text for c in LONG_DASHES):
            p.append(f"{name} has a long dash (house style)")
    return p


def visibility_ok(state, want):
    """Problems with the Visibility step read-back for `want` (a plain str in VISIBILITIES); empty only on positive
    evidence that `want` alone is chosen: its radio reads exactly "true", the other two exactly "false", no schedule
    picker is open and the button is enabled and reads exactly DONE_LABELS[want] (Save for private, Publish for
    public: each value refuses the other's label)."""
    if type(want) is not str or want not in VISIBILITIES:
        return [f"{want!r} is not one of {VISIBILITIES}"]
    if not isinstance(state, dict):
        return [f"no visibility read-back ({state!r})"]
    p = []
    mine = want.lower()
    if state.get(mine) != "true":
        p.append(f"{want} reads {state.get(mine)!r}")
    for other in ("private", "public", "unlisted"):
        if other != mine and state.get(other) != "false":
            p.append(f"{other.upper()} reads {state.get(other)!r}, wanted 'false'")
    if state.get("scheduler_open") is not False:
        p.append("a schedule picker is open or unreadable")
    label = DONE_LABELS[want]
    if state.get("done_label") != label:
        p.append(f"the button reads {state.get('done_label')!r}, wanted {label!r}")
    if state.get("done_disabled") is not False:
        p.append(f"the {label} button is disabled or unreadable")
    return p


NOT_PUBLIC = r"(?i)publish|not public|unlisted|private|schedul"   # words a public visibility label must not carry


def verify_checks(item, got, oembed_status, vid=None, want=DEFAULT_VISIBILITY, oembed_valid=False):
    """A fresh Studio read and the public oEmbed status, against the item and the visibility `want` that was saved.
    PRIVATE: oEmbed must refuse (a private video answers 401, 403 or 404; 200 means public and None, no answer, is
    not evidence) and the visibility text must say Private and none of Public, Unlisted or Scheduled. PUBLIC: oEmbed
    must answer 200 with a body that is oEmbed for this video (`oembed_valid`, from oembed_body_ok), and the label,
    whitespace normalized, must start with the word Public and carry none of NOT_PUBLIC. Any other `want` fails
    both. The page must be this video's and both details flags must read back "true" (an unreadable flag is a
    failure, never a pass)."""
    got = got if isinstance(got, dict) else {}
    vis = got.get("visibility") or ""
    flags = got.get("flags") if isinstance(got.get("flags"), dict) else {}
    norm = " ".join(vis.split())
    rules = {"PRIVATE": ("Private", "Private" in vis and not re.search(r"(?i)public|unlisted|schedul", vis),
                         "oEmbed refuses (not public)", oembed_status in (401, 403, 404)),
             "PUBLIC": ("Public", re.match(r"Public\b", norm) is not None and not re.search(NOT_PUBLIC, norm),
                        "oEmbed answers 200 (public)", oembed_status == 200 and oembed_valid is True)}
    fail = ("the saved value", False, "oEmbed confirms the saved value", False)
    label, shows, oembed_name, oembed_good = rules.get(want, fail) if type(want) is str else fail
    return {"title": got.get("title") == item["title"],
            "description": (got.get("description") or "").rstrip("\n") == item["description"],
            "page is this video's": isinstance(vid, str) and bool(vid) and f"/video/{vid}/" in (got.get("location") or ""),
            f"visibility shows {label}": shows,
            "not made for kids": flags.get("kids") == "true",
            "altered content is yes": flags.get("altered") == "true",
            oembed_name: oembed_good}


def signed_out(u):
    return u.startswith("https://accounts.google.com") or "ServiceLogin" in u


def receipt(work):
    """The receipt dict ({} when none yet), or the short code "receipt_corrupt" when the file is unreadable or not an
    object. Callers must check isinstance(..., dict); a corrupt receipt is never guessed at or overwritten."""
    p = Path(work) / "upload.json"
    if not p.exists():
        return {}
    try:
        r = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "receipt_corrupt"
    return r if isinstance(r, dict) else "receipt_corrupt"


IDENTITY_KEYS = ("day", "subject_sha256", "visibility")


def save(work, **kw):
    """Merge fields into the receipt, durably (temp file fsynced, atomic replace, directory fsynced). `day`,
    `subject_sha256` and `visibility` may be set once and never changed: a second video for the same day, or the same
    draft saved with another visibility, is a needs-you stop."""
    r = receipt(work)
    if not isinstance(r, dict):
        raise ReceiptError(r)
    for k in IDENTITY_KEYS:
        if k in kw and k in r and kw[k] != r[k]:
            raise ReceiptError("receipt_identity_changed")
    r.update(kw, updated=now_iso())
    d = Path(work)
    fd, tmp = tempfile.mkstemp(prefix=".upload-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(r, indent=1, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, d / "upload.json")
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    dfd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


@contextlib.contextmanager
def work_lock(work):
    """Exclusive, non-blocking lock on the day's work dir for the whole upload step. A second holder (a rerun while
    the first is mid-upload) gets ReceiptError("upload_locked") at once instead of racing it. Released on exit,
    on an exception, and by the kernel if the process dies."""
    fd = os.open(Path(work) / "upload.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ReceiptError("upload_locked") from None
        yield
    finally:
        os.close(fd)


# ---------- the uploads ledger (outside every work dir; survives a deleted receipt) ----------

def ledger_path():
    return paths.state_root() / "uploads" / "ledger.jsonl"


@contextlib.contextmanager
def ledger_lock():
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield p
    finally:
        os.close(fd)


def _ledger_rows(p):
    """Every row, or exit 4 when the ledger is unreadable: it cannot rule out an earlier upload."""
    if not p.exists():
        return []
    try:
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        rows = None
    if rows is None or not all(isinstance(r, dict) for r in rows):
        needs_you("ledger", "the uploads ledger is unreadable: check Studio by hand before any upload", "unreadable_ledger")
    return rows


def _ledger_append(p, **kw):
    """Append one row: every byte is written (os.write may take fewer than it is given) before the fsync; a write that
    fails or makes no progress rolls the file back to its size before the row, so a partial row is never made durable
    (the reader still fails closed on a torn row left by a crash)."""
    data = (json.dumps(dict(kw, at=now_iso()), ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        start = os.fstat(fd).st_size
        try:
            view, done = memoryview(data), 0
            while done < len(data):
                n = os.write(fd, view[done:])
                if not isinstance(n, int) or n <= 0:
                    raise OSError(errno.EIO, "the ledger write made no progress")
                done += n
            os.fsync(fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.ftruncate(fd, start)
            raise
    finally:
        os.close(fd)
    dfd = os.open(p.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _ledger_stop(day):
    needs_you("ledger", f"the uploads ledger already has an entry for {day}. Never upload a second video for the same "
                        "day: check Studio, then decide by hand", "ledger_has_day")


def ledger_guard(day):
    """Read only: exit 4 when any upload (any render) was ever recorded for `day`."""
    with ledger_lock() as p:
        if any(r.get("day") == day for r in _ledger_rows(p)):
            _ledger_stop(day)


def ledger_claim(day, subject, item_id):
    """Atomically (under the ledger lock) refuse a day that has any entry, else record upload_started."""
    with ledger_lock() as p:
        if any(r.get("day") == day for r in _ledger_rows(p)):
            _ledger_stop(day)
        _ledger_append(p, event="upload_started", day=day, subject_sha256=subject, id=item_id)


def ledger_verified(day, subject, item_id, vid, vis):
    with ledger_lock() as p:
        _ledger_append(p, event=VERIFIED[vis], day=day, subject_sha256=subject, id=item_id, video_id=vid)


# ---------- visibility: one of VISIBILITIES, from the explicit argument only ----------

def set_visibility(name):
    """The only visibility setter. Anything but a plain str in VISIBILITIES is refused before the browser is touched;
    the upload flow passes the visibility it was given on the command line, nothing else."""
    if type(name) is not str or name not in VISIBILITIES:
        H.die("refused", f"this driver only ever saves one of {VISIBILITIES}; asked for {name!r}")
    v = H.ev(f"(() => {{ const r = {D}.querySelector('#privacy-radios tp-yt-paper-radio-button[name={name}]');"
             " if (!r) return 'missing'; r.scrollIntoView({block: 'center'});"
             " if (r.getAttribute('aria-checked') !== 'true') r.click(); return r.getAttribute('aria-checked'); })()")
    if v not in ("true", True):
        H.die("visibility", f"{name} radio reads {v!r}")
    H.ok("visibility", name)


# ---------- the browser: attach to the loopback endpoint (or a leased one) and let go of it afterwards ----------

def endpoint_ok(ep):
    """Only a DevTools endpoint on this machine's loopback is connected to (daily.settings.LOOPBACK_ENDPOINT)."""
    return isinstance(ep, str) and LOOPBACK_ENDPOINT.fullmatch(ep) is not None


def scrub(msg):
    """Remove any DevTools endpoint from text that may be printed."""
    return re.sub(r"(?:wss?|https?)://\S+", "<endpoint>", str(msg))


def attach_ok(out):
    """Positive evidence the session drives a headed Chrome: a parsed probe, a Chrome user agent that is not headless,
    and navigator.webdriver exactly false (a session that silently fell back to a browser of agent-browser's own, or
    one that marks itself automated, fails)."""
    lines = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("--- ")]
    try:
        v = json.loads(lines[0])
        v = json.loads(v) if isinstance(v, str) else v
    except (ValueError, IndexError):
        return False
    return (isinstance(v, dict) and isinstance(v.get("ua"), str) and "Chrome/" in v["ua"]
            and "Headless" not in v["ua"] and v.get("wd") is False)


def socket_dir():
    """The folder agent-browser keeps this driver's sessions in (AGENT_BROWSER_SOCKET_DIR), so they never mix with
    anyone else's: $XDG_RUNTIME_DIR/orion-on-the-clock-browser, or <state>/browser without a runtime folder."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    return Path(base) / "orion-on-the-clock-browser" if base else paths.state_root() / "browser"


def driver_lock_path():
    return paths.state_root() / "studio.lock"


def session_names():
    """My agent-browser sessions: the ones with a pid file in my socket folder."""
    d = socket_dir()
    try:
        names = {p.name[:-4] for p in d.glob("*.pid")}
    except OSError:
        return set()
    return {n for n in names if SESSION_RE.fullmatch(n)}


def _process_command(pid):
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        pass
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=10).stdout
    except Exception:
        return ""


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return True
    return not re.search(r"^State:\s+Z", state, re.M)   # a zombie has exited


def stop_session(name, sleep=time.sleep):
    """Stop one of my agent-browser sessions by ending its daemon (the pid in its pid file, when that process is
    agent-browser), never with `close`: on a browser it attached to, close can close the browser itself. The session's
    files are removed. True when a daemon was stopped."""
    d = socket_dir()
    try:
        pid = int((d / f"{name}.pid").read_text().strip())
    except (OSError, ValueError):
        pid = None
    stopped = False
    if pid and pid > 1 and "agent-browser" in _process_command(pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not _alive(pid):
                break
            sleep(0.1)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)   # a daemon stuck mid-handshake can ignore SIGTERM
        stopped = True
    for suffix in (".pid", ".sock", ".stream", ".version"):
        with contextlib.suppress(OSError):
            (d / (name + suffix)).unlink()
    return stopped


def stop_sessions():
    """Stop every session of mine (a crash can leave one). Never raises an Exception."""
    for name in sorted(session_names()):
        try:
            stop_session(name)
        except Exception:
            pass


def _pool(which):
    """One lease or release command (settings.toml browser.lease / browser.release); its JSON answer (never printed:
    it can carry the DevTools endpoint)."""
    argv = list(LEASE_CMD if which == "lease" else RELEASE_CMD)
    if not argv:
        return {"ok": False, "code": "NO_COMMAND", "message": f"no browser.{which} command is set"}
    try:
        out = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=POOL_CALL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": "TIMEOUT", "message": f"the {which} command did not answer in time"}
    except OSError as e:
        return {"ok": False, "code": "NOT_RUNNABLE", "message": type(e).__name__}
    try:
        r = json.loads(out.stdout)
    except ValueError:
        return {"ok": False, "code": "NO_JSON", "message": scrub((out.stderr or out.stdout)[-200:])}
    return r if isinstance(r, dict) else {"ok": False, "code": "BAD_ANSWER", "message": "the answer is not an object"}


def lease_with_wait(pool=None, sleep=time.sleep, clock=time.monotonic, wait_s=BUSY_WAIT_S, poll_s=BUSY_POLL_S):
    """Lease the browser, retrying while another holder has it (code BUSY) for up to wait_s, then exit 3. Any other
    refusal exits 1 at once. Returns the lease data."""
    pool = pool or _pool
    deadline = clock() + wait_s
    while True:
        r = pool("lease")
        if not isinstance(r, dict):
            die_code(EXIT_ABORT, "lease", "the lease command answered a non-object")
        if r.get("ok") is True:
            return r.get("data")
        if r.get("code") != "BUSY":
            die_code(EXIT_ABORT, "lease", scrub(f"{r.get('code')}: {r.get('message')}"))
        if clock() >= deadline:
            die_code(EXIT_BUSY, "lease", f"the browser stayed busy for {wait_s // 60} min")
        sleep(poll_s)


def attach():
    """Lock (one driver command at a time), lease the browser when a pool is set, connect my own session to the
    endpoint, and prove the session drives a headed Chrome."""
    global _LEASED, _LOCK_FD
    if _LOCK_FD is None:
        lock = driver_lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            H.die("lock", "another driver command is using the browser")
        _LOCK_FD = fd
    d = socket_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    stop_sessions()   # a session left by an earlier run that died: under the lock, anything here is stale
    if LEASE_CMD:
        lease = lease_with_wait()
        _LEASED = True
        endpoint = lease.get("endpoint") if isinstance(lease, dict) else None
    else:
        _LEASED = True
        endpoint = ENDPOINT
    if not endpoint_ok(endpoint):
        H.die("endpoint", "the browser endpoint is not a DevTools endpoint on the loopback")
    H.S = SESSION_PREFIX + secrets.token_hex(4)
    c = subprocess.run(["timeout", "90", "agent-browser", "--session", H.S, "connect", endpoint],
                       capture_output=True, text=True, env=H.ENV, stdin=subprocess.DEVNULL)
    if c.returncode != 0:   # never echo its output: it can carry the endpoint
        H.die("connect", f"exit {c.returncode} connecting to the browser")
    if not attach_ok(H._raw("eval", "JSON.stringify({ua: navigator.userAgent, wd: navigator.webdriver})", t=30)):
        H.die("attach", f"session {H.S} shows no proof it drives a headed Chrome that is not marked automated")
    H.ok("attach", "leased browser" if LEASE_CMD else "browser on the loopback endpoint")


def release():
    """Let go of the browser: with a pool, give the lease back first (Chrome closes cleanly and keeps its sign-ins);
    then stop my own sessions. The browser itself is never closed by this driver."""
    global _LEASED
    stop = None
    if RELEASE_CMD:
        r = _pool("release")
        if r.get("ok") is not True:
            H.die("release", scrub(f"{r.get('code')}: {r.get('message')}"))
        d = r.get("data")
        stop = d.get("stop") if isinstance(d, dict) else None
        if stop not in ("clean", "signal", "kill"):   # not known to have stopped: stop nothing
            H.die("release", f"the release reported stop {stop!r}; sessions left as they are, check the pool")
    _LEASED = False       # the browser is let go whatever the session cleanup does next
    H.S = UNATTACHED
    stop_sessions()
    if RELEASE_CMD and stop != "clean":
        H.die("release", f"the browser stopped {stop!r}, not clean: check its sign-in on the next lease")
    H.ok("release", "lease given back (stop: clean)" if RELEASE_CMD else "session stopped; the browser keeps running")


class _Terminated(SystemExit):
    """Raised by the SIGTERM handler so the normal finally path lets go of the browser."""


def _on_sigterm(signum, frame):
    raise _Terminated(128 + signum)


def with_browser(fn, *a):
    """Attach, run fn, and let go of the browser whatever happened (also on SIGTERM, e.g. systemd stop). Returns the
    exit code: fn's own when it stopped, else the release's. ORION_CLOCK_KEEP_ON_ABORT=1 keeps the browser after a
    failure for an attended `look`, but never after SIGTERM."""
    global _LEASED
    code = 0
    terminated = False
    old = signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        attach()
        fn(*a)
    except _Terminated as e:
        code, terminated = e.code, True
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else EXIT_ABORT
    finally:
        keep = code != 0 and not terminated and os.environ.get("ORION_CLOCK_KEEP_ON_ABORT") == "1"
        if _LEASED and not keep:
            try:
                release()
            except SystemExit as e:
                if code == 0:
                    code = e.code if isinstance(e.code, int) else EXIT_ABORT
            _LEASED = False
        signal.signal(signal.SIGTERM, old)
    return code


# ---------- page helpers ----------

VIS = "const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };"
VIDS = (f"(() => {{ const d = {D}; if (!d) return '[]'; return JSON.stringify([...new Set([...d.innerHTML.matchAll("
        "/(?:shorts\\/|youtu\\.be\\/)([\\w-]{11})/g)].map(m => m[1]))]); })()")
PROG = f"(() => ({D} && ({D}.querySelector('.progress-label, ytcp-video-upload-progress') || {{}}).innerText) || '')()"
READ_VIS = (f"(() => {{ const d = {D}; const st = n => {{ const r = d && d.querySelector('#privacy-radios tp-yt-paper-radio-button[name=' + n + ']');"
            " return r ? r.getAttribute('aria-checked') : 'missing'; };"
            " const sch = [...document.querySelectorAll('ytcp-uploads-dialog ytcp-visibility-scheduler ytcp-date-picker input')]"
            ".some(i => i.getBoundingClientRect().width > 0); const b = d && d.querySelector('#done-button');"
            " const s = { private: st('PRIVATE'), public: st('PUBLIC'), unlisted: st('UNLISTED'), scheduler_open: sch,"
            " done_label: b ? b.innerText.trim() : null,"
            " done_disabled: b ? (b.hasAttribute('disabled') || b.getAttribute('aria-disabled') === 'true') : true };"
            f" const labels = {json.dumps(DONE_LABELS)};"
            " const only = (k, label) => ['private', 'public', 'unlisted'].every(x => s[x] === (x === k ? 'true' : 'false'))"
            " && s.scheduler_open === false && s.done_label === label && s.done_disabled === false;"
            " s.safe_to_save = { PRIVATE: only('private', labels.PRIVATE), PUBLIC: only('public', labels.PUBLIC) };"
            " return JSON.stringify(s); })()")
PUBLISHED_JS = (f"(() => {{ {VIS} return [...document.querySelectorAll('tp-yt-paper-dialog, ytcp-dialog')].filter(vis)"
                ".map(x => x.innerText.trim().replace(/\\s+/g, ' ')).find(t => /Video published/i.test(t)) || null; })()")
CONF_JS = ("(() => [...document.querySelectorAll('tp-yt-paper-dialog, ytcp-dialog')].map(x => x.innerText.trim().replace(/\\s+/g, ' '))"
           ".find(t => /Video (saved|processing|uploaded)/i.test(t)) || null)()")
READ_EDIT = ("(() => { const t = s => { const e = document.querySelector(s); return e ? e.innerText.trim() : null; };"
             " const v = [...document.querySelectorAll('ytcp-video-metadata-visibility, #visibility-container, [id*=visibility]')]"
             ".map(e => e.innerText.replace(/\\s+/g, ' ').trim()).filter(Boolean)[0] || null;"
             " return JSON.stringify({ title: t('#title-textarea #textbox'), description: t('#description-textarea #textbox'), visibility: v,"
             " location: location.href }); })()")
READ_FLAGS = ("(() => { const r = n => { const m = [...document.querySelectorAll('tp-yt-paper-radio-button[name=\"' + n + '\"]')]"
              ".filter(e => !e.closest('#privacy-radios')); return m.length === 1 ? String(m[0].getAttribute('aria-checked')) : 'unreadable'; };"
              " return JSON.stringify({ kids: r('VIDEO_MADE_FOR_KIDS_NOT_MFK'), altered: r('VIDEO_HAS_ALTERED_CONTENT_YES') }); })()")


def button_in(root_expr, label):
    """JS expression: the visible button with exactly this label inside root_expr (or null)."""
    return (f"(() => {{ {VIS} const r = ({root_expr}); return r ? [...r.querySelectorAll('ytcp-button, button')].filter(vis)"
            f".find(b => b.innerText.trim() === {json.dumps(label)}) || null : null; }})()")


RADIOS = frozenset({"VIDEO_MADE_FOR_KIDS_NOT_MFK", "VIDEO_HAS_ALTERED_CONTENT_YES"})


def radio_selector_js(name):
    """JS expression: the one details radio with exactly this name, never one inside the privacy radio group (or null)."""
    sel = json.dumps(f"tp-yt-paper-radio-button[name={json.dumps(name)}]")
    return (f"(() => {{ const m = [...{D}.querySelectorAll({sel})].filter(r => !r.closest('#privacy-radios'));"
            " return m.length === 1 ? m[0] : null; })()")


def _click_radio(name):
    if name not in RADIOS:
        H.die("refused", f"radio {name!r} is not one of the details radios this driver sets")
    H.ev(f"(() => {{ const r = {radio_selector_js(name)}; if (!r) return false; r.scrollIntoView({{block: 'center'}}); r.click(); return true; }})()")


def radio(name, label):
    """Make one of the RADIOS checked and prove it by reading it back. Any other name is refused before the browser."""
    if name not in RADIOS:
        H.die("refused", f"radio {name!r} is not one of the details radios this driver sets")
    read = f"(() => {{ const r = {radio_selector_js(name)}; return r ? r.getAttribute('aria-checked') : 'missing'; }})()"
    v = H.ev(read)
    if v not in ("true", True):
        _click_radio(name)
        v = H.ev(read)
    if v not in ("true", True):
        H.die(label, v)
    H.ok(label)


def open_dialogs():
    """Visible popup dialogs other than the upload wizard: [{title, buttons}] (read only)."""
    return H.ev(f"(() => {{ {VIS} return JSON.stringify([...document.querySelectorAll('tp-yt-paper-dialog, ytcp-dialog, [role=dialog]')].filter(vis)"
                ".map(d => ({ title: d.innerText.trim().split('\\n')[0].slice(0, 80), buttons: [...d.querySelectorAll('ytcp-button, button')].filter(vis)"
                ".map(b => (b.innerText.trim() || b.getAttribute('aria-label') || '').slice(0, 30)).filter(Boolean).slice(0, 12) }))); })()") or []


def on_screen():
    if H.ev("(() => document.visibilityState)()") != "visible":
        H.die("visibility state", "my Studio tab is not in front; dialogs will not lay out")


def oembed(vid):
    url = f"https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v={vid}"
    try:
        return urllib.request.urlopen(url, timeout=20).status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return None


OEMBED_MAX_BYTES = 65536


def oembed_body_ok(body, vid):
    """True only when `body` parses as a JSON object with type "video" and an html or url field that carries `vid` as a
    whole id (not inside a longer token)."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError, RecursionError):
        return False
    if not isinstance(data, dict) or data.get("type") != "video" or not isinstance(vid, str):
        return False
    whole = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(vid) + r"(?![A-Za-z0-9_-])")
    return any(isinstance(data.get(k), str) and whole.search(data[k]) is not None for k in ("html", "url"))


def oembed_video(vid):
    """(status, confirmed) of the public oEmbed lookup for `vid`: confirmed only on a 200 whose body is oEmbed for this
    video (oembed_body_ok). A 200 alone is not enough: a redirect can land on a 200 page."""
    url = f"https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v={vid}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            status, body = r.status, r.read(OEMBED_MAX_BYTES)
    except urllib.error.HTTPError as e:
        return e.code, False
    except Exception:
        return None, False
    return status, status == 200 and oembed_body_ok(body, vid)


def channel_id():
    return H.ev("(() => (window.ytcfg && ytcfg.data_ && ytcfg.data_.CHANNEL_ID) || null)()")


def on_channel(ch):
    """Stop for you (wrong_channel) unless the channel read `ch` is this channel."""
    if not CHAN or ch != CHAN:
        needs_you("identity", f"Studio is on {ch}, wanted {CHAN} ({CHANNEL_NAME}). Switch the browser's YouTube account "
                              f"to {CHANNEL_NAME} by hand, then rerun", "wrong_channel")
    H.ok("identity", f"{CHAN} ({CHANNEL_NAME})")
    return ch


def identity():
    return on_channel(channel_id())


def ack_notice():
    """Click "Got it" on Studio's informational "still checking your content" notice (nothing else). True if clicked."""
    return bool(H.ev(f"(() => {{ {VIS} const d = [...document.querySelectorAll('tp-yt-paper-dialog, ytcp-dialog')].filter(vis)"
                     ".find(x => /still checking/i.test(x.innerText)); const b = d ? [...d.querySelectorAll('ytcp-button, button')].filter(vis)"
                     ".find(x => x.innerText.trim() === 'Got it') : null; if (!b) return false; b.click(); return true; })()"))


def wizard_open():
    """True/False from an explicit read of the upload wizard; None when the read failed (not evidence of anything)."""
    v = H.ev(f"(() => {{ const d = {D}; const r = d && d.getBoundingClientRect(); return r && r.width > 0 ? 'open' : 'gone'; }})()")
    return {"open": True, "gone": False}.get(v)


def commit_visibility(want, on_attempt=None):
    """Save only on proof of `want` (a plain str in VISIBILITIES: the visibility the upload was given). Anything else is
    refused before the browser. One evaluated script reads the Visibility step and clicks the done button only if that
    same read says `want` alone is chosen, with its exact label, and it is safe, so nothing can change between the proof
    and the click. Returns Studio's confirmation text (new since before the click), or "wizard closed" when Studio gave
    no text but the wizard is gone. PUBLIC reads and closes the "Video published" dialog (after_public_click).
    `on_attempt` runs just before the click script is dispatched, so the receipt records the attempt even when the
    click lands but its eval is lost (a timeout or a throw): a rerun then verifies and never clicks again."""
    if type(want) is not str or want not in VISIBILITIES:
        H.die("refused", f"this driver only ever saves one of {VISIBILITIES}; asked for {want!r}")
    public = want != DEFAULT_VISIBILITY
    before = H.ev(PUBLISHED_JS if public else CONF_JS)
    if on_attempt is not None:
        on_attempt()
    r = H.ev("(() => { const s = JSON.parse(" + READ_VIS + "); let clicked = false;"
             f" if (s.safe_to_save && s.safe_to_save[{json.dumps(want)}] === true) {{"
             f" {D}.querySelector('#done-button').click(); clicked = true; }}"
             " return JSON.stringify({ state: s, clicked: clicked }); })()")
    state = r.get("state") if isinstance(r, dict) else None
    clicked = isinstance(r, dict) and r.get("clicked") is True
    problems = visibility_ok(state, want)
    if problems:
        H.die("commit" if clicked else "pre-commit", ("SAVE WAS CLICKED; " if clicked else "") + "; ".join(problems))
    if not clicked:
        H.die("pre-commit", "the Save click did not happen")
    H.wait(6000)
    if public:
        return after_public_click(before)
    conf = H.ev(CONF_JS)
    if not conf or conf == before:
        # Studio can show an informational "still checking your content" notice whose only button is "Got it"
        if ack_notice():
            H.wait(5000)
            conf = H.ev(CONF_JS)
    if conf and conf != before:
        H.ok("saved", conf[:110])
        return conf
    if wizard_open() is False:
        H.ok("saved", "wizard closed, no confirmation text")
        return "wizard closed"
    H.die("commit", f"no new save confirmation; dialogs: {open_dialogs()}")


def close_published_dialog():
    """Dismiss the "Video published" dialog with ITS exactly-"Close" button: a visible button inside that visible dialog,
    nothing else. True if clicked."""
    return bool(H.ev(f"(() => {{ {VIS} const d = [...document.querySelectorAll('tp-yt-paper-dialog, ytcp-dialog')].filter(vis)"
                     ".find(x => /Video published/i.test(x.innerText)); const b = d ? [...d.querySelectorAll('ytcp-button, button')]"
                     f".filter(vis).find(x => x.innerText.trim() === {json.dumps('Close')}) : null; if (!b) return false;"
                     " b.click(); return true; })()"))


def after_public_click(before):
    """After the PUBLIC click: acknowledge the "still checking" notice if shown, then when a new visible "Video
    published" dialog is open, keep its first 160 characters (no other page text) and close it with its own Close
    button. With no such dialog, the wizard must be gone ("wizard closed"), else the run stops. Verify proves the
    result either way."""
    if ack_notice():
        H.wait(5000)
    text = H.ev(PUBLISHED_JS)
    if isinstance(text, str) and text and text != before:
        closed = close_published_dialog()
        H.ok("saved", text[:110] + ("" if closed else " (its Close button was not found)"))
        return text[:160]
    if wizard_open() is False:
        H.ok("saved", "wizard closed, no confirmation text")
        return "wizard closed"
    H.die("commit", f"no new confirmation after the save; dialogs: {open_dialogs()}")


# ---------- the upload wizard ----------
# Every action here is a fixed-purpose helper with one hard-coded target; there is no generic one.

ROW_RE = re.compile(r"\s*(?:" + ARROW + r"\s*)?\[(\d+)\]\s.*(https?://\S+|about:blank\S*)\s*$")
MARK_KEY = "orion-clock-mark"
SWITCH_IN_WAIT_MS, SWITCH_BACK_WAIT_MS = "7000", "6000"   # after opening a channel home, before reading it


def _tab_rows():
    """[(index, url)] of every tab, of any site (a tab on a chooser or sign-in page is still listed)."""
    return [(m.group(1), m.group(2)) for m in (ROW_RE.match(x) for x in H._raw("tab", "list", t=30).splitlines()) if m]


def _js_arg(js):
    return base64.b64encode(js.encode()).decode()


def _mark_set_js(mark, with_channel=False):
    read = (", ch: (window.ytcfg && ytcfg.data_ && ytcfg.data_.CHANNEL_ID) || null, href: location.href"
            if with_channel else "")
    ret = "JSON.stringify({ name: window.name" + read + " })" if with_channel else "window.name"
    return (f"(() => {{ window.name = {json.dumps(mark)}; try {{ sessionStorage.setItem({json.dumps(MARK_KEY)}, "
            f"{json.dumps(mark)}); }} catch (e) {{}} return {ret}; }})()")


def _mark_read_js():
    return (f"(() => {{ let s = null; try {{ s = sessionStorage.getItem({json.dumps(MARK_KEY)}); }} catch (e) {{}} "
            "return window.name || s || ''; })()")


def _mark_guard_js(mark):
    return (f"(() => {{ let s = null; try {{ s = sessionStorage.getItem({json.dumps(MARK_KEY)}); }} catch (e) {{}} "
            f"const n = window.name || s; if (n !== {json.dumps(mark)}) throw new Error('GUARD: not my tab'); "
            "return 'guard ok'; })()")


def _batch(steps):
    """One agent-browser JSON batch (stops at the first failed step): the list of step results, or None."""
    out = H._raw("batch", "--json", "--bail", stdin=json.dumps(steps), t=180)
    try:
        res = json.loads(out)
    except ValueError:
        return None
    return res if isinstance(res, list) and all(isinstance(r, dict) for r in res) else None


def _val(r):
    v = r.get("result")
    v = v.get("result") if isinstance(v, dict) and "result" in v else v
    if isinstance(v, str) and v[:1] in "[{":
        try:
            return json.loads(v)
        except ValueError:
            pass
    return v


def _token_guard_js(token):
    return (f"(() => {{ if (!location.href.includes({json.dumps(token)})) throw new Error('GUARD: not the new tab'); "
            "return 'guard ok'; })()")


def _open_own_tab():
    """Open MY Studio tab, always a new one (a tab that is already open is never reused), and identify it at creation
    by a unique token in its URL, not by position: open about:blank#<token>, find the single tab whose URL carries the
    token, mark it in a batch that first re-asserts the token (so a shifted index cannot mark someone else's tab), read
    the mark back, THEN navigate that tab to the channel's Studio home and re-mark it there (a load can clear the
    name). Returns the page URL it landed on. If the tab cannot be told apart with certainty nothing is kept and the
    run stops."""
    global _OPENED_TAB, _MARK
    token = f"oclock-studio-{os.getpid()}-{time.time_ns()}"
    _MARK = None
    H.MARK = None
    H._raw("tab", "new", f"about:blank#{token}", t=90)
    _OPENED_TAB = True
    H.wait(1500)
    ours = [n for n, u in _tab_rows() if token in u]
    if len(ours) != 1:
        H.die("new tab", f"{len(ours)} tabs carry my token: nothing is acted on")
    mark = token
    first = _batch([["tab", ours[0]], ["eval", "-b", _js_arg(_token_guard_js(token))],
                    ["eval", "-b", _js_arg(_mark_set_js(mark))]])
    if not (first and len(first) == 3 and all(r.get("success") for r in first) and _val(first[2]) == mark):
        H.die("new tab", "my tab could not be marked with certainty: nothing is acted on")
    _MARK = mark
    H.MARK = mark       # every pinned operation now requires the mark on its tab
    idx = _find_mark()
    if idx is None:
        H.die("new tab", "my marked tab is not certain: nothing is acted on")
    got = _open_marked(idx, DAILY_HOME_URL, SWITCH_IN_WAIT_MS)
    if got is None:
        H.die("new tab", "my tab did not load the channel home with its mark: nothing further is acted on")
    return got.get("href") or ""


def _open_marked(idx, url, wait_ms):
    """Navigate tab idx to url in ONE batch that first re-asserts this run's mark on that tab (a shifted index can only
    fail the guard), waits, then re-marks the tab and reads it back. The read-back {name, ch, href}, or None unless
    every step ran and the tab still carries the mark (also when idx is None: our tab is not certain)."""
    if idx is None or not _MARK or not url:
        return None
    res = _batch([["tab", idx], ["eval", "-b", _js_arg(_mark_guard_js(_MARK))], ["open", url], ["wait", wait_ms],
                  ["eval", "-b", _js_arg(_mark_set_js(_MARK, True))]])
    got = _val(res[4]) if res and len(res) == 5 and all(r.get("success") for r in res) else None
    return got if isinstance(got, dict) and got.get("name") == _MARK else None


def _find_mark():
    """Index of the one tab carrying this run's mark, or None unless exactly one does."""
    if not _MARK:
        return None
    hits = []
    for n, _u in _tab_rows():
        res = _batch([["tab", n], ["eval", "-b", _js_arg(_mark_read_js())]])
        if res and len(res) == 2 and res[1].get("success") and _val(res[1]) == _MARK:
            hits.append(n)
    return hits[0] if len(hits) == 1 else None


def _pinned_guard_js():
    """pinned.py's guard (the tab's URL is one of mine) plus: the tab carries this run's mark, in window.name or in
    sessionStorage (a load can clear the name). Replaces H._guard_js so every pinned operation requires the mark."""
    pre = ([f"https://studio.youtube.com/channel/{H.CHAN}"] if H.CHAN else []) + \
        ([f"https://studio.youtube.com/video/{H.VIDEO_ID}/"] if H.VIDEO_ID else []) + list(H.EXTRA)
    mark = (f"let s = null; try {{ s = sessionStorage.getItem({json.dumps(MARK_KEY)}); }} catch (e) {{}} "
            f"if ((window.name || s) !== {json.dumps(H.MARK)}) throw new Error('GUARD: tab not marked'); " if H.MARK else "")
    return (f"(() => {{ const ok = {json.dumps(pre)}.some(p => location.href.startsWith(p)); "
            "if (!ok) throw new Error('GUARD: not my Studio tab: ' + location.href.slice(0, 80)); "
            + mark + "return 'guard ok'; })()")


def _tab_mark_of(n):
    """The mark of tab n (window.name, else the session copy) for pinned.my_tab's tie-break; None if unreadable."""
    res = _batch([["tab", n], ["eval", "-b", _js_arg(_mark_read_js())]])
    return _val(res[1]) if res and len(res) == 2 and res[1].get("success") else None


H._guard_js = _pinned_guard_js
H._tab_name = _tab_mark_of


def studio_tab(rec=None):
    """Switch in: open my own Studio tab on the upload channel (always new, marked at creation), stop for you if the
    browser is signed out, then prove the channel. A URL the pin does not match is wrong_channel. An empty channel read
    (Studio showed no channel, not another one) gets ONE re-open on my tab; a stop at identity first keeps a private
    screenshot of my tab (stop_screenshot)."""
    global _SWITCHED_IN
    H.EXTRA = [f"https://studio.youtube.com/video/{rec['video_id']}/"] if rec and rec.get("video_id") else []
    _stop_if_signed_out(_open_own_tab())
    _SWITCHED_IN = True    # the browser is now on the upload channel's Studio (or you must switch it): put it back
    try:
        pinned = H.my_tab()
    except _Terminated:
        raise
    except SystemExit:
        stop_screenshot("wrong_channel")     # the first open landed off the channel (a chooser, a prompt): keep a picture
        needs_you("identity", f"Studio did not stay on {CHANNEL_NAME} Studio. Switch the browser's YouTube account to "
                              f"{CHANNEL_NAME} by hand, then rerun", "wrong_channel")
    ch = _read_if_pinned_to_mine(pinned)
    if not ch:      # Studio showed no channel (not another one): one re-open of the channel home on my tab, never a loop
        ch = _reopen_daily()
    if ch != CHAN:
        stop_screenshot("wrong_channel")     # a read of my tab only; it never changes the stop that follows
    on_channel(ch)


def _reopen_daily():
    """The one switch-in retry: re-open the upload channel's home on MY marked tab (the same guarded batch as at
    creation) and read the channel again. None when my tab is not certain (nothing more is read or navigated; the stop
    follows). A re-open batch that fails on my tab is a driver failure and aborts, as a failed first open does."""
    idx = _find_mark()
    if idx is None:
        return None
    got = _open_marked(idx, DAILY_HOME_URL, SWITCH_IN_WAIT_MS)
    if got is None:
        H.die("re-open", "my tab did not reload the channel home with its mark: nothing further is acted on")
    _stop_if_signed_out(got.get("href") or "")
    try:
        pinned = H.my_tab()  # the URL pin, as at the first read: off the channel's Studio is a stop, not an abort
    except _Terminated:
        raise
    except SystemExit:
        return None
    return _read_if_pinned_to_mine(pinned)


def _read_if_pinned_to_mine(pinned):
    """The channel read when pinned.py's URL pin (`pinned`, from H.my_tab) is MY marked tab; None when it found another
    tab. The pin checks the mark only when two tabs match the channel's Studio URL, so with my tab sent off the channel
    (a chooser) and your own tab on it, the pin finds YOUR tab: that counts as Studio showing no channel (one re-open,
    then the needs-you stop with a screenshot of my tab). Decided before the read, so nothing is caught: any failure
    of the read itself (a broken batch, a dead browser) still aborts, and a termination propagates."""
    mine = _find_mark()
    if mine is None or str(pinned) != str(mine):
        return None
    return channel_id()


def _stop_if_signed_out(landed):
    if signed_out(landed):
        needs_you("sign-in", "the browser is signed out of Google. Sign in by hand in that browser; an agent never "
                             "types a password or a code", "signed_out")


# ---------- a private local screenshot when the switch-in stops at identity ----------

STOPS_DIR = "_studio-stops"
STOPS_KEEP = 20


def prune_stops(d, keep=STOPS_KEEP):
    """Keep the newest `keep` screenshots in d (by modification time); delete only older *.png files directly in d."""
    shots = sorted((p for p in d.glob("*.png") if p.is_file()), key=lambda p: (p.stat().st_mtime_ns, p.name))
    for p in shots[:max(0, len(shots) - keep)]:
        with contextlib.suppress(OSError):
            p.unlink()


def stops_dir():
    """<state>/_studio-stops (the state folder: ORION_CLOCK_STATE or its default). Local only."""
    return Path(os.path.abspath(paths.state_root())) / STOPS_DIR


def _stop_shot(code):
    """(path, None) once the file is on disk, else (None, a short fixed reason). Never page text."""
    d = stops_dir()
    if d.is_symlink():      # the prune deletes files: never through a link to somewhere else
        return None, "the stop directory is a symlink"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    path = d / f"{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{code}.png"
    idx = _find_mark()
    if idx is None:
        return None, "my tab is not certain"
    res = _batch([["tab", idx], ["eval", "-b", _js_arg(_mark_guard_js(_MARK))], ["screenshot", str(path)]])
    if not (res and len(res) == 3 and all(r.get("success") for r in res)):
        return None, "the screenshot step failed"
    if not path.is_file():
        return None, "no file was written"
    os.chmod(path, 0o600)
    with contextlib.suppress(OSError):
        prune_stops(d)
    return path, None


def stop_screenshot(code):
    """Save a screenshot of MY marked tab only, in one batch that selects it by its mark and re-asserts the mark before
    the screenshot (a read: nothing is pressed, typed or saved in Studio). Prints one line: the path, or
    `none (<short reason>)`. A failure never raises an Exception and never changes the stop; a termination propagates."""
    try:
        path, why = _stop_shot(code)
    except Exception as e:
        path, why = None, f"unexpected {type(e).__name__}"
    print(f"studio-stop screenshot: {path}" if path else f"studio-stop screenshot: none ({why})")
    return path


def switch_back():
    """Put the browser back on the home channel (settings.toml channel.home_id) by navigating ONLY the tab this run
    opened (found by its mark, re-asserted in the same batch; never by URL) and reading the identity there. An empty
    channel read gets ONE re-open of the home the same way (a different channel does not). True only when it reads
    back the home channel; False when our tab is not certain or no home channel is set. Catches Exception only: a
    termination propagates."""
    global _SWITCHED_IN
    _SWITCHED_IN = False
    if not HOME_URL:
        return False
    try:
        got = _open_marked(_find_mark(), HOME_URL, SWITCH_BACK_WAIT_MS)
        if got is not None and not got.get("ch"):
            got = _open_marked(_find_mark(), HOME_URL, SWITCH_BACK_WAIT_MS)
        return got is not None and got.get("ch") == HOME_CHAN
    except Exception:
        return False


def close_my_tab():
    """Close ONLY the tab this run opened (found by its mark). True only when no tab carries the mark afterwards;
    False, closing nothing, when our tab is not certain. A termination propagates."""
    try:
        idx = _find_mark()
        if idx is None:
            return False
        # One batch: select the index, re-check our mark on THAT tab (the batch stops here if it does not match), then
        # close the selected tab. An index that shifted since the search can only fail the guard.
        res = _batch([["tab", idx], ["eval", "-b", _js_arg(_mark_guard_js(_MARK))], ["tab", "close"]])
        if not res or len(res) != 3 or not all(r.get("success") for r in res):
            return False
        return _find_mark() is None
    except Exception:
        return False


def switch_back_after(work, keep_for_look):
    """After the browser work: switch back (when the browser was navigated and a home channel is set), print
    `SWITCHED_BACK true|false` and record it in the day's receipt (only once an upload began: a receipt of its own
    would look like another day's to the next run), then close the tab this run opened and print `CLOSED_TAB
    true|false`. Neither changes an exit code. `keep_for_look` (an ordinary failure with ORION_CLOCK_KEEP_ON_ABORT=1)
    leaves the dialog for an attended look. A termination during either step does not skip the other; it propagates
    afterwards."""
    global _OPENED_TAB, _MARK, _SWITCHED_IN
    if keep_for_look and os.environ.get("ORION_CLOCK_KEEP_ON_ABORT") == "1":
        return
    stop = None
    if _SWITCHED_IN and not HOME_CHAN:
        _SWITCHED_IN = False   # no home channel set: nothing to switch back to, and nothing to record
    if _SWITCHED_IN:
        try:
            back = switch_back() is True
        except _Terminated as e:
            back, stop = False, e
        except Exception:
            back = False
        print(f"SWITCHED_BACK {'true' if back else 'false'}")
        if work is not None:
            try:
                rec = receipt(work)
                if isinstance(rec, dict) and rec.get("day"):
                    save(work, switched_back=back)
            except (ReceiptError, OSError):
                pass
    if _OPENED_TAB:
        try:
            closed = close_my_tab() is True
        except _Terminated as e:
            closed, stop = False, stop or e
        except Exception:
            closed = False
        print(f"CLOSED_TAB {'true' if closed else 'false'}")
        _OPENED_TAB, _MARK = False, None
    H.MARK = None
    if stop is not None:
        raise stop


def held(work, fn, *a):
    """Run fn on the attached browser, then always switch the browser back (see switch_back_after)."""
    failed = True
    try:
        fn(*a)
        failed = False
    except _Terminated:
        failed = False        # a termination never keeps the browser for an attended look
        raise
    finally:
        switch_back_after(work, failed)


def click_show_more():
    """Open the details step's "Show more" section (the audience and altered-content radios live there)."""
    return bool(H.ev(f"(() => {{ const b = {button_in(D, 'Show more')}; if (!b) return false; b.click(); return true; }})()"))


def click_page_show_more():
    """On a video's edit page, expand its "Show more" section (document scope; the flag radios may live there)."""
    return bool(H.ev(f"(() => {{ const b = {button_in('document', 'Show more')}; if (!b) return false; b.click(); return true; }})()"))


def read_flags():
    """Read-only fresh read of the two details flags on the edit page: {"kids", "altered"}, each "true", "false" or
    "unreadable". Expands "Show more" once when either is unreadable. Anything not exactly true/false is unreadable."""
    def once():
        v = H.ev(READ_FLAGS)
        v = v if isinstance(v, dict) else {}
        return {k: (v.get(k) if v.get(k) in ("true", "false") else "unreadable") for k in ("kids", "altered")}

    flags = once()
    if "unreadable" in flags.values() and click_page_show_more():
        H.wait(2000)
        flags = once()
    return flags


def click_edit_draft():
    """On a draft's edit page, reopen its upload wizard."""
    return bool(H.ev(f"(() => {{ const b = {button_in('document', 'Edit draft')}; if (!b) return false; b.click(); return true; }})()"))


def click_next():
    """The wizard stepper's Next button."""
    return bool(H.ev(f"(() => {{ const b = [...{D}.querySelectorAll('#next-button')].find(x => x.getBoundingClientRect().width > 0);"
                     " if (!b) return false; b.click(); return true; })()"))


def click_details_step():
    """Back to the wizard's Details step."""
    return bool(H.ev(f"(() => {{ const b = [...{D}.querySelectorAll('[role=tab], #step-badge-0')]"
                     f".find(x => x.getBoundingClientRect().width > 0 && x.innerText.trim().startsWith({json.dumps('Details')}));"
                     " if (!b) return false; b.click(); return true; })()"))


def close_saved_dialog():
    """Dismiss the "Video saved / processing" dialog with its Close button (nothing else)."""
    return bool(H.ev("(() => { const d = [...document.querySelectorAll('tp-yt-paper-dialog, ytcp-dialog')]"
                     ".find(x => /Video (saved|processing|uploaded)/i.test(x.innerText));"
                     f" const b = d ? [...d.querySelectorAll('ytcp-button, button')].find(x => x.innerText.trim() === {json.dumps('Close')}) : null;"
                     " if (!b) return false; b.click(); return true; })()"))


def upload_file(item, before_file):
    """Hand the file to Studio and return the new video's id. `before_file` runs just before the bytes are handed
    over, so the receipt claims the day first and a crash cannot leave an unrecorded upload."""
    H.ab("open", f"{DAILY_HOME_URL}/videos/upload?d=ud")
    H.wait(7000)
    on_screen()
    identity()
    before_file()
    H.set_file("ytcp-uploads-dialog ytcp-uploads-file-picker input[type=file]", item["file"], f"{item['id']} file")
    if sha256_file(item["file"]) != item["sha256"]:
        H.die("file", "the file changed while it was handed to Studio; the draft is recorded, check Studio by hand")
    for _ in range(40):
        H.wait(3000)
        ids = H.ev(VIDS) or []
        if len(ids) == 1:
            return ids[0]
        if len(ids) > 1:
            H.die("video id", f"several video links in the dialog: {ids}")
    H.die("video id", "no video link appeared in the upload dialog after 2 minutes")


def resume(vid):
    if H.ev(H.HEADING) and vid in (H.ev(VIDS) or []):
        H.ok("resume", f"wizard for {vid} is still open")
        return
    H.ab("open", f"https://studio.youtube.com/video/{vid}/edit?c={CHAN}")   # ?c= pins the channel
    H.wait(7000)
    on_screen()
    identity()
    if not click_edit_draft():
        needs_you("resume", f"{vid} has no 'Edit draft' button: it may have been saved or removed (a crash after Save?). "
                            "Check it in Studio, then decide by hand", "draft_gone")
    H.wait(5000)
    if vid not in (H.ev(VIDS) or []):
        H.die("resume", "the wizard that opened is not this video's")
    H.ok("resume", f"reopened draft {vid}")


def next_step(want):
    click_next()
    H.wait(2500)
    h = H.ev(H.HEADING)
    if h not in want:
        H.die("stepper", f"expected {want}, got {h!r}")
    return h


def details(item):
    """Title, description, then BOTH flags on every pass: not made for kids, and altered or synthetic content = yes
    (the host voice is synthetic). Each radio is read back checked or the run stops."""
    if H.ev(H.HEADING) != "Details":
        click_details_step()
        H.wait(2500)
    if H.ev(H.HEADING) != "Details":
        H.die("details", H.ev(H.HEADING))
    for sel, key in (("#title-textarea #textbox", "title"), ("#description-textarea #textbox", "description")):
        cur = H.ev(f"(() => {{ const e = {D}.querySelector({json.dumps(sel)}); return e ? e.innerText : null; }})()")
        if cur is not None and cur.rstrip("\n") == item[key]:
            H.ok(key, "already set (read back)")
        else:
            H.put_text(sel, item[key], key)
    click_show_more()
    H.wait(2000)
    radio("VIDEO_MADE_FOR_KIDS_NOT_MFK", "audience: not made for kids")
    radio("VIDEO_HAS_ALTERED_CONTENT_YES", "altered content: yes (synthetic host voice)")


def wait_uploaded():
    """The bytes must be up before anything commits; then give the checks up to about 90 s."""
    st = ""
    for _ in range(60):
        st = (H.ev(PROG) or "").strip()
        if st and not re.search(r"(?i)uploading", st):
            break
        H.wait(5000)
    else:
        H.die("upload", f"still uploading after 5 minutes: {st[:80]}")
    for _ in range(18):
        if re.search(r"(?i)checks complete", st):
            break
        H.wait(5000)
        st = (H.ev(PROG) or "").strip()
    H.ok("upload state", st.replace("\n", " ")[:80])


def to_visibility():
    for _ in range(6):
        h = H.ev(H.HEADING)
        if h == "Visibility":
            return
        if h not in ("Details", "Video elements", "Initial check", "Checks"):
            H.die("stepper", h)
        next_step(("Video elements", "Initial check", "Checks", "Visibility"))
    H.die("stepper", f"never reached Visibility (at {H.ev(H.HEADING)!r})")


OEMBED_TRIES, OEMBED_WAIT_S = 10, 30   # PUBLIC: oEmbed must answer 200 within about 4.5 min of a passing Studio read


def _pause(seconds):
    time.sleep(seconds)


def verify_one(item, vid, vis=DEFAULT_VISIBILITY):
    """Read only: a FRESH load of the video's Studio page and the oEmbed status, judged for `vis`. PRIVATE passes only
    on Private plus a refusing oEmbed, asked once. PUBLIC passes only on an exact Public label plus an oEmbed 200 whose
    body is oEmbed for this video: once every Studio check passes, oEmbed is asked up to OEMBED_TRIES times
    OEMBED_WAIT_S apart, and when that answer alone never came the result says unconfirmed. Nothing is pressed or saved
    and the visibility is never touched."""
    H.VIDEO_ID = vid
    H.EXTRA = [f"https://studio.youtube.com/video/{vid}/"]
    H.ab("open", f"https://studio.youtube.com/video/{vid}/edit?c={CHAN}")
    H.wait(6000)
    identity()
    got = H.ev(READ_EDIT) or {}
    if isinstance(got, dict):
        got = dict(got, flags=read_flags())
    public = vis != DEFAULT_VISIBILITY
    status, valid = oembed_video(vid) if public else (oembed(vid), False)
    checks = verify_checks(item, got, status, vid, vis, oembed_valid=valid)
    extra = {"oembed_valid": valid} if public else {}
    if public and all(ok for name, ok in checks.items() if not name.startswith("oEmbed")):
        tries = 1
        while not valid and tries < OEMBED_TRIES:
            _pause(OEMBED_WAIT_S)
            status, valid = oembed_video(vid)
            tries += 1
        checks = verify_checks(item, got, status, vid, vis, oembed_valid=valid)
        extra = {"oembed_valid": valid, "oembed_tries": tries, "unconfirmed": not valid}
    return {"pass": all(checks.values()), "checks": checks, "oembed": status,
            "visibility": (got.get("visibility") or "")[:80] if isinstance(got, dict) else "", "at": now_iso(), **extra}


def unconfirmed_stop(vid):
    """The stop for any public run that cannot prove the release after the save was attempted (a Studio read that does
    not match, a flag, or oEmbed never confirming within the bound): exit 4, code public_unconfirmed, an ask for you.
    Nothing is saved again and the visibility is not touched; you check the video by hand."""
    needs_you("verify", f"{vid}: after the save attempt the public release could not be proved (Studio read, details, or "
                        f"oEmbed within {OEMBED_TRIES} tries {OEMBED_WAIT_S} s apart). Nothing was saved again: check "
                        "the video by hand", "public_unconfirmed")


def _upload(work, item, day, vis):
    rec = receipt(work)
    studio_tab(rec)
    vid = rec.get("video_id")
    if vid:
        H.VIDEO_ID = vid
    attempted = rec.get("stage") in (SAVE_CLICKED, "saved")    # a save was attempted on an earlier run

    def attempt():
        nonlocal attempted
        save(work, stage=SAVE_CLICKED, visibility=vis)   # before the press is dispatched: a rerun never presses again
        attempted = True

    try:
        if rec.get("stage") in (None, "uploaded", "details"):
            if vid:
                resume(vid)
            else:
                def claim():
                    if sha256_file(item["file"]) != item["sha256"]:
                        H.die("file", "the file changed after it was checked: nothing was uploaded or recorded")
                    ledger_claim(day, item["sha256"], item["id"])    # the ledger first: a lost receipt still blocks a rerun
                    save(work, id=item["id"], day=day, subject_sha256=item["sha256"], stage="upload_started",
                         visibility=vis)

                vid = upload_file(item, claim)
                H.VIDEO_ID = vid
                H.EXTRA = [f"https://studio.youtube.com/video/{vid}/"]
                save(work, video_id=vid, stage="uploaded")
                H.ok("video id", f"{vid} (receipt written: a rerun resumes this draft)")
            details(item)
            save(work, stage="details")
            wait_uploaded()
            to_visibility()
            set_visibility(vis)
            conf = commit_visibility(vis, on_attempt=attempt)
            save(work, stage="saved", confirmation=conf[:160])
            close_saved_dialog()
        result = verify_one(item, vid, vis)
        save(work, verify=result)
        if not result["pass"]:
            if vis != DEFAULT_VISIBILITY:
                unconfirmed_stop(vid)
            H.die("verify", json.dumps(result["checks"]))
        save(work, stage=VERIFIED[vis], studio_url=f"https://studio.youtube.com/video/{vid}/edit")
        ledger_verified(day, item["sha256"], item["id"], vid, vis)
    except _Terminated:
        raise
    except (SystemExit, Exception) as e:   # a public run that stops after the attempt is an ask, never a failed FYI
        if vis != DEFAULT_VISIBILITY and attempted and not (isinstance(e, SystemExit) and e.code == EXIT_NEEDS_YOU):
            unconfirmed_stop(vid)
        raise
    H.ok("DONE", f"{item['id']} {vid} uploaded {vis} and verified")


# ---------- the receipt gate and the commands ----------

# upload_started is written just before the bytes go up and is never resumable: it means a draft may exist whose id
# was not recorded. Every resumable stage has a video id.
# save_clicked is written just before the done-button press is sent: a rerun from it (or from saved) only verifies.
RESUMABLE = ("uploaded", "details", SAVE_CLICKED, "saved") + TERMINAL


def day_of(work):
    day = Path(work).name
    if not DAY_RE.fullmatch(day) or not Path(work).is_dir():
        H.die("work dir", "the work directory must exist and be named for its day (YYYY-MM-DD)")
    return day


def load_item(work):
    p = Path(work) / "upload-item.json"
    if not p.is_file():
        H.die("item", "upload-item.json missing (the stage before this one writes it)")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        H.die("item", "upload-item.json is unreadable")


def receipt_gate(work, day, subject):
    """The receipt for `day` as a dict, or exit 4 (needs you) before any browser when it cannot be trusted: corrupt,
    for another day or another render, missing its identity, an unknown stage, or a started upload whose video id
    was never recorded (Studio may hold a draft; a second upload would be a second copy)."""
    rec = receipt(work)
    if not isinstance(rec, dict):
        needs_you("receipt", "upload.json is unreadable: check Studio for a draft before moving it aside",
                  "receipt_corrupt")
    if not rec:
        return rec
    if rec.get("day") != day or not isinstance(rec.get("subject_sha256"), str):
        needs_you("receipt", "upload.json is for another day or has no identity: check Studio by hand",
                  "receipt_other_day")
    if rec["subject_sha256"] != subject:
        needs_you("receipt", "this day already has an upload receipt for a different render. Never upload a second "
                             "video for the same day automatically: check Studio, then decide by hand",
                  "receipt_other_render")
    vid = rec.get("video_id")
    ok_id = isinstance(vid, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", vid)
    if rec.get("stage") not in RESUMABLE or not ok_id:
        code = ("upload_started_no_video" if not vid else "bad_video_id" if not ok_id else "receipt_not_resumable")
        needs_you("receipt", "the receipt is in a state this driver does not resume from: check Studio by hand "
                             "(an upload that started without a recorded video id may have left a draft)", code)
    if rec["stage"] in TERMINAL and VERIFIED.get(rec.get("visibility", DEFAULT_VISIBILITY)) != rec["stage"]:
        needs_you("receipt", "the receipt's finished stage does not match the visibility it records: check Studio by "
                             "hand", "receipt_not_resumable")
    return rec


def parse_visibility(args):
    """(work, visibility) from `<work-dir> [--visibility private|public]`; None for any other shape (a usage error).
    The argument is the only source of the visibility: no config file, no environment variable."""
    choices = {v.lower(): v for v in VISIBILITIES}
    if len(args) == 1:
        return args[0], DEFAULT_VISIBILITY
    if len(args) == 3 and args[1] == "--visibility" and args[2] in choices:
        return args[0], choices[args[2]]
    return None


def cmd_upload(work, vis=DEFAULT_VISIBILITY):
    if vis not in VISIBILITIES:
        H.die("visibility", f"{vis!r} is not one of {VISIBILITIES}")
    work = Path(work)
    day = day_of(work)
    with work_lock(work):
        item = load_item(work)
        subject = item.get("sha256") if isinstance(item, dict) else None
        if isinstance(subject, str):
            rec = receipt_gate(work, day, subject)
            if rec.get("stage") in TERMINAL:     # a finished day is never touched again, in either mode
                H.ok("receipt", f"{day} is already {rec['stage']} as {rec['video_id']}")
                return 0
            if not rec:
                ledger_guard(day)     # no receipt: a deleted one or another work dir must not buy a second copy
            elif rec.get("visibility", DEFAULT_VISIBILITY) != vis:   # a receipt from before visibilities was private
                needs_you("receipt", "this day's draft began with another visibility than this run was given. A draft "
                                     "keeps the visibility it began with: check Studio, then decide by hand",
                          "visibility_mismatch")
        problems = check_item(item, day)
        if problems:
            H.die("item", "; ".join(problems))
        return with_browser(held, work, _upload, work, item, day, vis)


def cmd_verify(work, vis=DEFAULT_VISIBILITY):
    if type(vis) is not str or vis not in VISIBILITIES:
        H.die("visibility", f"{vis!r} is not one of {VISIBILITIES}")
    work = Path(work)
    day = day_of(work)
    with work_lock(work):
        item = load_item(work)
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("sha256", "title", "description")):
            H.die("item", "upload-item.json lacks sha256, title or description")
        rec = receipt_gate(work, day, item["sha256"])
        if not rec.get("video_id"):
            H.die("verify", "no video_id in upload.json: nothing uploaded yet")
        if rec.get("stage") not in (SAVE_CLICKED, "saved") + TERMINAL:
            H.die("verify", f"the draft is only at stage {rec.get('stage')}: it is not saved yet, nothing to verify")
        if rec.get("visibility", DEFAULT_VISIBILITY) != vis:
            needs_you("receipt", "this video was saved with another visibility than this verify was given: rerun "
                                 "verify with the visibility the receipt records", "visibility_mismatch")

        def run():
            studio_tab(rec)
            result = verify_one(item, rec["video_id"], vis)
            save(work, verify=result)
            print(("PASS " if result["pass"] else "FAIL ") + json.dumps(result["checks"]))
            if not result["pass"]:
                if vis != DEFAULT_VISIBILITY:
                    unconfirmed_stop(rec["video_id"])
                sys.exit(EXIT_ABORT)

        return with_browser(held, work, run)


def cmd_preflight():
    studio_tab()
    on_screen()
    print(f"PREFLIGHT OK: the browser reaches {CHANNEL_NAME} Studio")


def look():
    print(json.dumps({"heading": H.ev(H.HEADING), "dialog": H.dialog_inputs(), "popups": open_dialogs(),
                      "visibility": H.ev(READ_VIS), "progress": (H.ev(PROG) or "")[:120]}, indent=1, ensure_ascii=False))


def release_only():
    try:
        release()
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else EXIT_ABORT
    return 0


def _dispatch(cmd, args):
    parsed = parse_visibility(args) if cmd in ("upload", "verify") else None
    known = parsed is not None or (cmd in ("preflight", "look", "release") and not args)
    if not known:
        print(__doc__)
        return EXIT_USAGE
    load_config()
    if parsed is not None:
        command = cmd_upload if cmd == "upload" else cmd_verify
        return command(Path(parsed[0]).resolve(), parsed[1])
    if cmd == "preflight":
        return with_browser(held, None, cmd_preflight)
    if cmd == "look":
        return with_browser(look)
    return release_only()


def _run(argv):
    cmd, args = (argv[1] if len(argv) > 1 else ""), argv[2:]
    try:
        return _dispatch(cmd, args)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else EXIT_ABORT
    except ReceiptError as e:   # a short code only; never the text of a page or an endpoint
        print(f"ABORT at receipt: {e.code}")
        if e.code in ("receipt_corrupt", "receipt_identity_changed"):
            _NEEDS_CODE[0] = e.code
            return EXIT_NEEDS_YOU
        return EXIT_ABORT
    except Exception as e:
        print(f"ABORT: unexpected {type(e).__name__}")
        return EXIT_ABORT


def main(argv):
    """Run one command. Exit 4 always ends with the one line `NEEDS_YOU <code>`, after any release output."""
    _NEEDS_CODE[0] = "needs_you"
    rc = _run(argv)
    if rc == EXIT_NEEDS_YOU:
        print(f"NEEDS_YOU {_NEEDS_CODE[0]}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
