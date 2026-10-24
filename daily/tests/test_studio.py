"""release/studio.py checks that need no browser: private or public, never unlisted or scheduled (refuses any other
visibility, takes PUBLIC only from the explicit argument, needs positive evidence of the chosen value before Save,
carries no scheduler), the upload item, the browser (a loopback endpoint or a lease; never closed), the channel pin,
the verify read-back (oEmbed refuses for private, answers 200 within a bound for public) and the always-release
wrapper. FakeBrowser stands in for agent-browser where a function reads the page; Chrome and PinnedChrome fake the
whole tab list. The page-script tests run the real scripts under node against a tiny fake DOM (skipped without
node)."""
import ast
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from daily import paths
from daily.settings import Settings

RELEASE = paths.REPO / "release"
sys.path.insert(0, str(RELEASE))
import studio as S  # noqa: E402

CHAN = "UC" + "d" * 22        # a synthetic upload channel
HOME_CHAN = "UC" + "h" * 22   # a synthetic home channel (the browser's other channel)
ARROW = chr(0x2192)
DASH = chr(0x2014)
DAY = "2026-10-16"
SETTINGS = Settings(timezone="America/Los_Angeles", series="Demo Agents on the Clock", host_name="Sam",
                    disclosure="Narrated with an AI voice; agent lines are verbatim from logs.", github=False,
                    channel_id=CHAN, channel_name="Demo Channel", home_channel_id=HOME_CHAN)
REAL_LOAD_CONFIG = S.load_config
REAL_EV = S.H.ev
REAL_OS = {"write": os.write, "replace": os.replace, "fsync": os.fsync}
NODE = shutil.which("node")
GOOD = {"private": "true", "public": "false", "unlisted": "false", "scheduler_open": False,
        "done_label": "Save", "done_disabled": False}
GOOD_PUBLIC = dict(GOOD, private="false", public="true", done_label="Publish")
DONE = {"PRIVATE": "Save", "PUBLIC": "Publish"}     # the exact done-button label Studio shows for each value
GOODS = {"PRIVATE": GOOD, "PUBLIC": GOOD_PUBLIC}
WANTS = ["PRIVATE", "PUBLIC"]
OTHER = {"PRIVATE": "PUBLIC", "PUBLIC": "PRIVATE"}
DAILY_HOME = "https://studio.youtube.com/channel/" + CHAN
HOME_HOME = "https://studio.youtube.com/channel/" + HOME_CHAN


def need_node():
    if not NODE:
        pytest.skip("node runs the page-script tests")


def restore_os(monkeypatch, *names):
    for name in names:
        monkeypatch.setattr(S.os, name, REAL_OS[name])


@pytest.fixture(autouse=True)
def stop_state(monkeypatch, tmp_path_factory):
    """Never the real state or runtime folders: the stop screenshots, the uploads ledger, the driver lock and the
    agent-browser socket folder all land in temp ones."""
    root = tmp_path_factory.mktemp("state")
    monkeypatch.setenv(paths.ENV_STATE, str(root))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path_factory.mktemp("run")))
    return root


@pytest.fixture(autouse=True)
def configured(stop_state, monkeypatch):
    """The driver configured from synthetic settings (configure() itself, so its derivations are what is tested);
    the commands re-apply them instead of reading a settings file."""
    S.configure(SETTINGS)
    monkeypatch.setattr(S, "load_config", lambda: S.configure(SETTINGS))
    yield


class Seq(list):
    """A scripted answer that changes over successive matching calls (the last one repeats)."""


class FakeBrowser:
    """Stands in for agent-browser: ev(js) returns the first scripted answer whose key is a substring of js (keys are
    tried in insertion order) and records every script. A script with no answer fails the test."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def ev(self, js):
        self.calls.append(js)
        for key, value in self.answers.items():
            if key in js:
                if isinstance(value, Seq):
                    return value.pop(0) if len(value) > 1 else value[0]
                return value
        raise AssertionError(f"unscripted browser call: {js[:100]}")


@pytest.fixture(autouse=True)
def clean_module_state():
    S._LEASED = False
    S.H.S = S.UNATTACHED
    yield
    S._LEASED = False
    S.H.S = S.UNATTACHED


@pytest.fixture
def ledger_env(stop_state):
    """The uploads ledger of this test's temp state folder (never the real one); its folder exists, the file not yet."""
    p = S.ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def browser(monkeypatch):
    def make(answers):
        fb = FakeBrowser(answers)
        monkeypatch.setattr(S.H, "ev", fb.ev)
        monkeypatch.setattr(S.H, "wait", lambda ms: None)
        return fb
    return make


def good_item(tmp_path):
    f = tmp_path / "episode.mp4"
    f.write_bytes(b"not really a video")
    return {"id": "daily-2026-10-16", "channel": CHAN, "file": str(f),
            "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
            "title": "Two agents, one browser pool", "description": "What got built yesterday."}


class Sneaky(str):
    """A str subclass equal to PUBLIC that formats as something else: never accepted."""

    def __format__(self, spec):
        return "UNLISTED"


def test_refuses_every_visibility_but_private_or_public(browser):
    fb = browser({})
    for name in ("UNLISTED", "SCHEDULED", "SCHEDULE", "public", "private", "Public", "PUBLIC ", "PRIVATE\n", "",
                 None, ["PUBLIC"], Sneaky("PUBLIC")):
        with pytest.raises(SystemExit) as e:
            S.set_visibility(name)
        assert e.value.code == 1, name
    assert fb.calls == []          # refused before any browser call


def test_the_visibilities_are_exactly_private_and_public():
    assert S.VISIBILITIES == ("PRIVATE", "PUBLIC") and S.DEFAULT_VISIBILITY == "PRIVATE"


@pytest.mark.parametrize("want", WANTS)
def test_visibility_ok_passes_only_on_positive_evidence(want):
    g, mine, other = GOODS[want], want.lower(), OTHER[want].lower()
    assert S.visibility_ok(g, want) == []
    bad = [dict(g, **{mine: "false"}), dict(g, **{mine: "missing"}), dict(g, **{other: "true"}),
           dict(g, unlisted="true"), dict(g, scheduler_open=True), dict(g, done_label=DONE[OTHER[want]]),
           dict(g, done_label="Schedule"), dict(g, done_disabled=True), GOODS[OTHER[want]], {}, None, "Save"]
    for state in bad:
        assert S.visibility_ok(state, want), state


@pytest.mark.parametrize("want", ["UNLISTED", "SCHEDULED", "private", "public", "", None, ["PUBLIC"]])
def test_visibility_ok_never_proves_anything_outside_the_two(want):
    for state in (GOOD, GOOD_PUBLIC, dict(GOOD, private="false", unlisted="true")):
        assert S.visibility_ok(state, want)


def test_visibility_ok_refuses_a_lookalike_str():
    assert S.visibility_ok(GOOD_PUBLIC, Sneaky("PUBLIC"))


def test_check_item(tmp_path):
    item = good_item(tmp_path)
    assert S.check_item(item, DAY) == []
    cases = [dict(item, channel=HOME_CHAN), dict(item, sha256="0" * 64),
             dict(item, file=str(tmp_path / "gone.mp4")), dict(item, title=""), dict(item, title="x" * 101),
             dict(item, title="a <b> title"), dict(item, description="d" * 5001),
             dict(item, title=f"one {DASH} two"), dict(item, description=f"one {DASH} two")]
    for bad in cases:
        assert S.check_item(bad, DAY), bad
    assert S.check_item({}, DAY)
    assert S.check_item("not an object", DAY)


VID = "vid12345678"
FLAGS_OK = {"kids": "true", "altered": "true"}


def good_read(**over):
    g = {"title": "T", "description": "D\n", "visibility": "Private", "flags": dict(FLAGS_OK),
         "location": f"https://studio.youtube.com/video/{VID}/edit?c={CHAN}"}
    g.update(over)
    return g


def test_verify_checks_need_private_and_a_refusing_oembed():
    item = {"title": "T", "description": "D"}
    got = good_read()
    vc = lambda g, st, v=VID: S.verify_checks(item, g, st, v)      # noqa: E731
    assert all(vc(got, 401).values()) and all(vc(got, 404).values())
    assert not all(vc(got, 200).values())      # public: never
    assert not all(vc(got, None).values())     # no answer is not evidence
    assert not all(vc(dict(got, title="other"), 401).values())
    assert not all(vc(dict(got, description="other"), 401).values())
    assert not all(vc(None, 401).values())
    assert not all(S.verify_checks(item, got, 401, None).values())


@pytest.mark.parametrize("vis", ["Public", "Unlisted", "Scheduled", "Private Public", "Private, scheduled for Oct 19",
                                 "Unlisted Private", ""])
def test_verify_visibility_rejects_any_other_visibility_text(vis):
    item = {"title": "T", "description": "D"}
    assert not S.verify_checks(item, good_read(visibility=vis), 401, VID)["visibility shows Private"]


@pytest.mark.parametrize("flags", [{"kids": "false", "altered": "true"}, {"kids": "true", "altered": "false"},
                                   {"kids": "unreadable", "altered": "true"}, {"kids": "true", "altered": "unreadable"},
                                   {}, None, "true", {"kids": True, "altered": True}])
def test_verify_needs_both_flags_read_back_true(flags):
    item = {"title": "T", "description": "D"}
    c = S.verify_checks(item, good_read(flags=flags), 401, VID)
    assert not all(c.values())
    assert not (c["not made for kids"] and c["altered content is yes"])


@pytest.mark.parametrize("loc", [None, "", "https://studio.youtube.com/video/OTHERVIDEO1/edit",
                                 "https://studio.youtube.com/channel/x/videos"])
def test_verify_needs_the_page_to_be_this_videos(loc):
    item = {"title": "T", "description": "D"}
    assert not S.verify_checks(item, good_read(location=loc), 401, VID)["page is this video's"]


def test_signed_out():
    assert S.signed_out("https://accounts.google.com/v3/signin/identifier?continue=x")
    assert S.signed_out("https://www.youtube.com/ServiceLogin?continue=x")
    assert not S.signed_out(f"https://studio.youtube.com/channel/{CHAN}/videos")


def test_receipt_roundtrip(tmp_path):
    assert S.receipt(tmp_path) == {}
    S.save(tmp_path, video_id="abc", stage="uploaded")
    S.save(tmp_path, stage="details")
    r = S.receipt(tmp_path)
    assert r["video_id"] == "abc" and r["stage"] == "details" and "updated" in r
    assert not (tmp_path / "upload.tmp").exists()


# ---------- static private-only pin (ast based) ----------

TOKENS = re.compile(r"public|unlisted|schedul|publish|done-button|radio|click", re.I)
# function -> lowercase tokens it may legitimately contain. Extending this is a reviewed decision, not a drive-by.
ALLOW = {
    "VISIBILITIES": {"public"},                # the only two values: PRIVATE (the default) and PUBLIC
    "VERIFIED": {"public"},                    # data only: the terminal receipt stage and ledger event per visibility
    "NEEDS_YOU_CODES": {"public"},             # data only: the code public_unconfirmed
    "DONE_LABELS": {"public", "publish"},      # data only: the exact done-button label per value (Save / Publish)
    "SAVE_CLICKED": {"click"},                 # data only: the receipt stage name written when the save is attempted
    "NOT_PUBLIC": {"publish", "public", "unlisted", "schedul"},   # data only: words a public label must NOT carry
    "PUBLISHED_JS": {"publish"},               # read-only text of the visible "Video published" dialog
    "close_published_dialog": {"publish", "click"},   # fixed purpose: the exactly-"Close" button of that dialog only
    "unconfirmed_stop": {"public"},            # the one needs-you stop with code public_unconfirmed; reads nothing
    "set_visibility": {"radio", "click"},      # the one visibility setter: privacy radios, one of VISIBILITIES only
    "visibility_ok": {"public", "unlisted", "schedul"},   # read-back keys and messages, reads only
    "verify_checks": {"public", "unlisted", "schedul"},   # "oEmbed refuses (not public)"; the visibility text must not say these
    "commit_visibility": {"done-button", "click"},   # Save: one script that proves the chosen value, clicks only if safe
    "READ_VIS": {"public", "unlisted", "schedul", "done-button", "radio"},   # read-only read-back; frozen by tests
    "ack_notice": {"click"},                   # fixed purpose: the "Got it" button of the checking notice
    "radio": {"radio"},                        # validates the name against RADIOS; no click in its own text
    "radio_selector_js": {"radio"},            # exact-name selector, outside the privacy radio group
    "_click_radio": {"radio", "click"},        # fixed purpose: click one RADIOS member
    "click_show_more": {"click"},              # fixed purpose, the details "Show more" button
    "click_edit_draft": {"click"},             # fixed purpose, reopen THIS draft's wizard
    "click_next": {"click"},                   # fixed purpose, the stepper's Next button
    "click_details_step": {"click"},           # fixed purpose, back to the Details step
    "click_page_show_more": {"click"},         # fixed purpose, "Show more" on the video edit page (document scope)
    "READ_FLAGS": {"radio"},                   # read-only aria-checked of the two named details radios
    "close_saved_dialog": {"click"},           # fixed purpose, Close on the saved/processing dialog
}
NEEDLES = ("datepicker-trigger", "date_js.sh", "time_js.sh", "sched_read.js", "VIDEO_HAS_ALTERED_CONTENT_NO",
           "name=PUBLIC", "name=UNLISTED", "DevToolsActivePort", 'ab("type"', 'ab("fill"')
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "getattr", "setattr", "globals", "vars"}
BANNED_AB = {"type", "fill", "press"}
RADIO_LITERALS = {"VIDEO_MADE_FOR_KIDS_NOT_MFK", "VIDEO_HAS_ALTERED_CONTENT_YES"}
# PUBLIC is reachable only from the explicit argument: the setter gets the threaded parameter `vis` (from
# parse_visibility via cmd_upload), called from the one upload flow, and VISIBILITIES is never indexed or used elsewhere.
VIS_CALLS = {"set_visibility", "commit_visibility"}
VIS_CALLERS = {"_upload"}
VIS_NAME_USERS = {"set_visibility", "parse_visibility", "cmd_upload", "visibility_ok", "commit_visibility",
                  "cmd_verify"}


def violations(src):
    tree = ast.parse(src)
    out = [f"needle {n}" for n in NEEDLES if n in src]
    seen = set()
    for top in tree.body:         # a later definition would silently replace a pinned one (READ_VIS, radio, ...)
        names = [top.name] if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else \
            [t.id for t in top.targets if isinstance(t, ast.Name)] if isinstance(top, ast.Assign) else []
        for n in names:
            if n in seen:
                out.append(f"duplicate top-level name {n}")
            seen.add(n)
    skip_mod = set()
    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant):
        skip_mod.add(id(tree.body[0].value))
    for top in tree.body:
        name = top.name if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else "<module>"
        if isinstance(top, ast.Assign) and len(top.targets) == 1 and isinstance(top.targets[0], ast.Name):
            name = top.targets[0].id      # a module constant is judged under its own name
        skip = set(skip_mod)
        for node in ast.walk(top):      # docstrings are prose, not behavior
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    skip.add(id(first.value))
        leaves = sorted((n for n in ast.walk(top) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                         and id(n) not in skip), key=lambda n: (n.lineno, n.col_offset))
        pieces = [n.value for n in leaves]
        allowed = ALLOW.get(name, set())
        for text in pieces + ["".join(pieces)]:       # each constant, and the whole function's constants joined
            for m in TOKENS.finditer(text):
                if m.group(0).lower() not in allowed:
                    out.append(f"{name}: forbidden text {m.group(0)!r}")
        for node in ast.walk(top):
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "VISIBILITIES":
                out.append(f"{name}: VISIBILITIES is indexed")
            if isinstance(node, ast.Name) and node.id == "VISIBILITIES" and isinstance(node.ctx, ast.Load) \
                    and name not in VIS_NAME_USERS:
                out.append(f"{name}: uses VISIBILITIES")
            if isinstance(node, ast.Name) and node.id == "vis" and not isinstance(node.ctx, ast.Load) \
                    and name in VIS_CALLERS:
                out.append(f"{name}: rebinds vis")
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            fname = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if isinstance(f, ast.Name) and fname in BANNED_CALLS:
                out.append(f"{name}: banned call {fname}")
            if fname in ("radio", "_click_radio") and name != "radio":    # callers pass one of the two literals
                a0 = node.args[0] if node.args else None
                if not (isinstance(a0, ast.Constant) and a0.value in RADIO_LITERALS):
                    out.append(f"{name}: {fname} not called with one of the two details radio names")
            if fname in VIS_CALLS:
                kws = [k.arg for k in node.keywords]
                if not (name in VIS_CALLERS and len(node.args) == 1 and isinstance(node.args[0], ast.Name)
                        and node.args[0].id == "vis" and kws in ([], ["on_attempt"] if fname == "commit_visibility" else [])):
                    out.append(f"{name}: {fname} not called with the threaded visibility argument")
            if fname in ("ab", "_raw"):
                args = node.args
                if not args or not (isinstance(args[0], ast.Constant) and isinstance(args[0].value, str)):
                    out.append(f"{name}: {fname} with a non-literal command")
                    continue
                for a in args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value.lower() in BANNED_AB:
                        out.append(f"{name}: {fname} {a.value!r} is never allowed")
    return out


def test_source_has_no_unlisted_or_schedule_path_and_public_only_from_the_argument():
    src = (RELEASE / "studio.py").read_text()
    assert violations(src) == []


@pytest.mark.parametrize("extra", ['"UNLISTED"', '"SCHEDULED"', "'UNLISTED'"])
def test_a_third_visibility_in_the_allowed_set_is_caught(extra):
    src = (RELEASE / "studio.py").read_text()
    line = 'VISIBILITIES = (DEFAULT_VISIBILITY, "PUBLIC")'
    assert src.count(line) == 1
    assert violations(src.replace(line, f'VISIBILITIES = (DEFAULT_VISIBILITY, "PUBLIC", {extra})'))


PROBES = {
    "split PUBLIC": "def sneak():\n    return 'PUB' + 'LIC'\n",
    "join PUBLIC": "def sneak():\n    return ''.join(['PU', 'BLIC'])\n",
    "variables PUBLIC": "def sneak():\n    a = 'PUB'\n    b = 'LIC'\n    return a + b\n",
    "fstring UNLISTED": "def sneak(x):\n    return f'UNLIST{x}ED' + f'UNLISTED{x}'\n",
    "schedule radio": "def sneak():\n    return '#schedule-radio-button'\n",
    "done button": "def sneak():\n    return '#done-button'\n",
    "Publish": "def sneak():\n    return 'Publish'\n",
    "single quote type": "def sneak():\n    H.ab('type', 'x')\n",
    "double quote fill": "def sneak():\n    H.ab(\"fill\", 'x')\n",
    "bare ab press": "def sneak():\n    ab('press', 'Enter')\n",
    "raw type": "def sneak():\n    H._raw('type', 'x')\n",
    "dynamic ab": "def sneak(c):\n    H.ab(c, 'x')\n",
    "radio click elsewhere": "def sneak():\n    return \"x.querySelector('tp-yt-paper-radio-button').click()\"\n",
    "eval": "def sneak(c):\n    return eval(c)\n",
    "getattr": "def sneak():\n    return getattr(H, 'a' 'b')\n",
    "needle": "X = 'datepicker-trigger'\n",
    "module constant PUBLIC": "OTHER = 'PUBLIC'\n",
    "module constant done button": "OTHER = '#done-button'\n",
    "READ_VIS reused name only": "READ_VIS2 = 'PUBLIC'\n",
    "radio dynamic name": "def sneak(item):\n    radio(item['vis'], 'x')\n",
    "radio unknown literal": "def sneak():\n    radio('SOMETHING_ELSE', 'x')\n",
    "click_radio dynamic": "def sneak(n):\n    _click_radio(n)\n",
    "duplicate READ_VIS": "READ_VIS = '(() => 1)()'\n",
    "duplicate radio": "def radio(name, label):\n    return 1\n",
    "js_click is gone for good": "def js_click(f):\n    return H.ev(f + '.click()')\n",
    "set_visibility literal": "def sneak():\n    set_visibility('PRIVATE')\n",
    "set_visibility indexed": "def sneak():\n    set_visibility(VISIBILITIES[-1])\n",
    "set_visibility unpacked": "def sneak():\n    a, vis = VISIBILITIES\n    set_visibility(vis)\n",
    "set_visibility keyword": "def _upload2(vis):\n    set_visibility(name=vis)\n",
    "set_visibility outside the flow": "def sneak(vis):\n    set_visibility(vis)\n",
    "vis rebound in the flow": "def _upload(work, item, day, vis):\n    vis = max(['PRIVATE'])\n",
    "second visibility set": "OTHER_VISIBILITIES = ('PRIVATE', 'UNLISTED')\n",
    "VISIBILITIES picked elsewhere": "def sneak():\n    return sorted(VISIBILITIES)[0]\n",
    "duplicate VISIBILITIES": "VISIBILITIES = ('PRIVATE', 'PUBLIC')\n",
    "another verified map": "TERMINALS = {'PUBLIC': 'public_verified'}\n",
    "verified stage picked by name": "def sneak():\n    return VERIFIED['PUBLIC']\n",
    "published dialog clicked elsewhere": "def sneak():\n    return \"[...d].find(x => /Video published/.test(x.innerText)).click()\"\n",
    "published text read elsewhere": "PUBLISHED_JS2 = '/Video published/'\n",
    "publish label elsewhere": "def sneak(s):\n    return s.get('done_label') == 'Publish'\n",
    "second label map": "LABELS = {'PUBLIC': 'Publish'}\n",
    "commit literal": "def _upload3():\n    commit_visibility('PUBLIC')\n",
    "commit outside the flow": "def sneak(vis):\n    return commit_visibility(vis)\n",
    "commit with another keyword": "def _upload6(vis):\n    commit_visibility(vis, want=vis)\n",
    "setter with the attempt hook": "def _upload7(vis):\n    set_visibility(vis, on_attempt=vis)\n",
    "commit with the old hook name": "def _upload8(vis):\n    commit_visibility(vis, on_click=vis)\n",
}


@pytest.mark.parametrize("name", sorted(PROBES))
def test_static_pin_catches_appended_probe(name):
    src = (RELEASE / "studio.py").read_text()
    assert violations(src) == []
    assert violations(src + "\n" + PROBES[name]), name


# ---------- set_visibility positive path ----------

@pytest.mark.parametrize("read", ["true", True])
def test_set_visibility_private_reaches_the_selector(browser, read, capsys):
    fb = browser({"privacy-radios": read})
    S.set_visibility("PRIVATE")
    assert len(fb.calls) == 1
    assert "#privacy-radios tp-yt-paper-radio-button[name=PRIVATE]" in fb.calls[0]
    assert "name=PUBLIC" not in fb.calls[0] and "name=UNLISTED" not in fb.calls[0]
    assert "ok   visibility PRIVATE" in capsys.readouterr().out


@pytest.mark.parametrize("read", ["true", True])
def test_set_visibility_public_reaches_only_its_own_selector(browser, read, capsys):
    fb = browser({"privacy-radios": read})
    S.set_visibility("PUBLIC")
    assert len(fb.calls) == 1
    assert "#privacy-radios tp-yt-paper-radio-button[name=PUBLIC]" in fb.calls[0]
    assert "name=PRIVATE" not in fb.calls[0] and "name=UNLISTED" not in fb.calls[0]
    assert "ok   visibility PUBLIC" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["PRIVATE", "PUBLIC"])
@pytest.mark.parametrize("read", [None, False, "false", "missing", "", 0])
def test_set_visibility_dies_without_a_true_readback(browser, read, name):
    browser({"privacy-radios": read})
    with pytest.raises(SystemExit) as e:
        S.set_visibility(name)
    assert e.value.code == 1


# ---------- surviving mutants ----------

@pytest.mark.parametrize("want", WANTS)
@pytest.mark.parametrize("val", [None, "x", True, "true", ""])
def test_visibility_ok_the_other_two_need_literal_false(want, val):
    for key in sorted({"private", "public", "unlisted"} - {want.lower()}):
        assert S.visibility_ok(dict(GOODS[want], **{key: val}), want), key


@pytest.mark.parametrize("want", WANTS)
@pytest.mark.parametrize("val", [None, "x", True, "false", "", "TRUE", "missing"])
def test_visibility_ok_the_chosen_radio_needs_literal_true(want, val):
    assert S.visibility_ok(dict(GOODS[want], **{want.lower(): val}), want)


@pytest.mark.parametrize("want", WANTS)
@pytest.mark.parametrize("key", ["public", "unlisted", "scheduler_open", "private", "done_label", "done_disabled"])
def test_visibility_ok_every_key_is_required(want, key):
    state = {k: v for k, v in GOODS[want].items() if k != key}
    assert S.visibility_ok(state, want)


@pytest.mark.parametrize("want", WANTS)
@pytest.mark.parametrize("val", [None, "x", "", 0, "false", "true", 1])
def test_visibility_ok_scheduler_open_must_be_exactly_false(want, val):
    assert S.visibility_ok(dict(GOODS[want], scheduler_open=val), want)
    assert S.visibility_ok(dict(GOODS[want], done_disabled=val), want)


@pytest.mark.parametrize("want", WANTS)
def test_visibility_ok_exact_label(want):
    mine = DONE[want]
    for label in (mine.lower(), mine.upper(), mine + " ", " " + mine, mine + "X", mine + "d", None, "", 5,
                  DONE[OTHER[want]], "Schedule", "Done"):
        assert S.visibility_ok(dict(GOODS[want], done_label=label), want), label


def test_each_mode_refuses_the_other_modes_label():
    assert S.visibility_ok(dict(GOOD, done_label="Publish"), "PRIVATE")
    assert S.visibility_ok(dict(GOOD_PUBLIC, done_label="Save"), "PUBLIC")
    assert S.visibility_ok(dict(GOOD_PUBLIC, done_disabled=True), "PUBLIC")
    assert S.visibility_ok(GOOD, "PRIVATE") == [] and S.visibility_ok(GOOD_PUBLIC, "PUBLIC") == []


def test_the_done_labels_are_exactly_save_and_publish():
    assert S.DONE_LABELS == {"PRIVATE": "Save", "PUBLIC": "Publish"}


def test_check_item_angle_brackets_each_way(tmp_path):
    item = good_item(tmp_path)
    for field in ("title", "description"):
        for ch in ("<", ">"):
            assert S.check_item(dict(item, **{field: f"a {ch} b"}), DAY), (field, ch)


def test_check_item_every_long_dash(tmp_path):
    item = good_item(tmp_path)
    for code in (0x2013, 0x2014, 0x2015):
        for field in ("title", "description"):
            assert S.check_item(dict(item, **{field: f"a {chr(code)} b"}), DAY), (field, hex(code))
    assert S.check_item(dict(item, title="a - b", description="well-known 8-10"), DAY) == []


def test_check_item_boundaries(tmp_path):
    item = good_item(tmp_path)
    assert S.check_item(dict(item, title="x"), DAY) == []
    assert S.check_item(dict(item, title="x" * 100), DAY) == []
    assert S.check_item(dict(item, description=""), DAY) == []
    assert S.check_item(dict(item, description="d" * 5000), DAY) == []


@pytest.mark.parametrize("field", ["id", "channel", "file", "sha256", "title", "description"])
@pytest.mark.parametrize("val", [None, 5, ["a"], {"a": 1}, True])
def test_check_item_non_string_fields(tmp_path, field, val):
    assert S.check_item(dict(good_item(tmp_path), **{field: val}), DAY)


@pytest.mark.parametrize("field", ["id", "channel", "file", "sha256", "title", "description"])
def test_check_item_missing_field(tmp_path, field):
    item = good_item(tmp_path)
    del item[field]
    assert S.check_item(item, DAY)


# ---------- minor item rules ----------

def test_check_item_title_shape(tmp_path):
    item = good_item(tmp_path)
    for bad in ("   ", "\t", "a\nb", "a\rb", "a\x00b", "a\x7fb", "a\u2028b", "a\u200bb", "a\tb"):
        assert S.check_item(dict(item, title=bad), DAY), repr(bad)


def test_check_item_description_allows_newlines_not_control_chars(tmp_path):
    item = good_item(tmp_path)
    assert S.check_item(dict(item, description="line one\n\nline two\tok"), DAY) == []
    assert S.check_item(dict(item, description="a\x00b"), DAY)
    assert S.check_item(dict(item, description="a\x1bb"), DAY)


def test_check_item_description_limit_is_bytes(tmp_path):
    item = good_item(tmp_path)
    assert S.check_item(dict(item, description="\u00e9" * 2500), DAY) == []      # 5000 bytes exactly
    assert S.check_item(dict(item, description="\u00e9" * 2501), DAY)            # 5002 bytes, 2501 chars


def test_check_item_rejects_symlinked_file(tmp_path):
    item = good_item(tmp_path)
    link = tmp_path / "link.mp4"
    link.symlink_to(item["file"])
    assert S.check_item(dict(item, file=str(link)), DAY)


def test_check_item_day_identity(tmp_path):
    item = good_item(tmp_path)
    assert S.check_item(item, DAY) == []
    assert S.check_item(item, "2026-10-17")                      # id says 10-16
    assert S.check_item(dict(item, id="daily-2026-10-17"), DAY)
    assert S.check_item(dict(item, id="whatever"), DAY)
    assert S.check_item(dict(item, day="2026-10-16"), DAY) == []
    assert S.check_item(dict(item, day="2026-10-17"), DAY)
    for bad_day in ("", "yesterday", "2026-1-16", None, 20261016):
        assert S.check_item(item, bad_day), bad_day


# ---------- receipt identity ----------

def test_receipt_corrupt_json_is_a_short_code(tmp_path):
    (tmp_path / "upload.json").write_text("{not json")
    assert S.receipt(tmp_path) == "receipt_corrupt"
    (tmp_path / "upload.json").write_text("[1, 2]")
    assert S.receipt(tmp_path) == "receipt_corrupt"
    (tmp_path / "upload.json").write_bytes(b"\xff\xfe\x00")
    assert S.receipt(tmp_path) == "receipt_corrupt"
    with pytest.raises(S.ReceiptError) as e:
        S.save(tmp_path, stage="x")
    assert e.value.code == "receipt_corrupt"
    assert (tmp_path / "upload.json").read_bytes() == b"\xff\xfe\x00"       # never overwritten


def test_save_refuses_to_change_day_or_subject(tmp_path):
    S.save(tmp_path, day=DAY, subject_sha256="a" * 64, stage="uploaded")
    S.save(tmp_path, day=DAY, subject_sha256="a" * 64, stage="details")        # same value is fine
    for kw in ({"day": "2026-10-17"}, {"subject_sha256": "b" * 64}, {"day": None}, {"subject_sha256": ""}):
        with pytest.raises(S.ReceiptError) as e:
            S.save(tmp_path, **kw)
        assert e.value.code == "receipt_identity_changed"
    r = S.receipt(tmp_path)
    assert r["day"] == DAY and r["subject_sha256"] == "a" * 64 and r["stage"] == "details"


def test_save_may_set_identity_once(tmp_path):
    S.save(tmp_path, stage="started")
    S.save(tmp_path, day=DAY)
    S.save(tmp_path, subject_sha256="c" * 64)
    with pytest.raises(S.ReceiptError):
        S.save(tmp_path, subject_sha256="d" * 64)


# ---------- durability and lock ----------

def test_save_fsyncs_file_then_dir_around_replace(tmp_path, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        events.append(("fsync", os.readlink(f"/proc/self/fd/{fd}")))
        return real_fsync(fd)

    def replace(a, b):
        events.append(("replace", str(a), str(b)))
        return real_replace(a, b)

    monkeypatch.setattr(S.os, "fsync", fsync)
    monkeypatch.setattr(S.os, "replace", replace)
    S.save(tmp_path, stage="x")
    kinds = [e[0] for e in events]
    assert kinds == ["fsync", "replace", "fsync"]
    tmp_name = events[1][1]
    assert events[0][1] == tmp_name                              # the temp file, before the swap
    assert events[1][2] == str(tmp_path / "upload.json")
    assert events[2][1] == str(tmp_path)                         # the directory, after
    assert not list(tmp_path.glob("*.tmp")) and not [p for p in tmp_path.iterdir() if p.name != "upload.json"]


def test_save_temp_names_are_unique(tmp_path, monkeypatch):
    names = []
    real = os.replace
    monkeypatch.setattr(S.os, "replace", lambda a, b: (names.append(str(a)), real(a, b))[1])
    S.save(tmp_path, stage="a")
    S.save(tmp_path, stage="b")
    assert len(set(names)) == 2


def test_save_failure_leaves_old_receipt_and_no_temp(tmp_path, monkeypatch):
    S.save(tmp_path, stage="old")

    def boom(a, b):
        raise OSError("disk")
    monkeypatch.setattr(S.os, "replace", boom)
    with pytest.raises(OSError):
        S.save(tmp_path, stage="new")
    restore_os(monkeypatch, "replace")
    assert S.receipt(tmp_path)["stage"] == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["upload.json"]


def test_work_lock_is_exclusive_and_released(tmp_path):
    with S.work_lock(tmp_path):
        with pytest.raises(S.ReceiptError) as e:
            with S.work_lock(tmp_path):
                pass
        assert e.value.code == "upload_locked"
    with S.work_lock(tmp_path):          # free again after a clean exit
        pass
    with pytest.raises(RuntimeError):
        with S.work_lock(tmp_path):
            raise RuntimeError("boom")
    with S.work_lock(tmp_path):          # and after an exception
        pass


def test_work_lock_does_not_touch_the_receipt(tmp_path):
    S.save(tmp_path, stage="x")
    with S.work_lock(tmp_path):
        S.save(tmp_path, stage="y")
    assert S.receipt(tmp_path)["stage"] == "y"


# ---------- the browser: a loopback endpoint, or a leased one; let go of it without ever closing it ----------

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.8059.39 Safari/537.36"
LEASED_EP = "ws://127.0.0.1:42287/devtools/browser/0b6e1c2a-77f0-4d1e-9a4b-1f2e3d4c5b6a"
LEASE_DATA = {"endpoint": LEASED_EP}
POOL = dataclasses.replace(SETTINGS, browser_lease=("pool", "--json", "lease", "b1"),
                           browser_release=("pool", "--json", "release", "b1"))


class FakePool:
    """Scripted lease command answers: each call takes the next one, and the last repeats."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, *a):
        self.calls.append(a)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def eval_out(value):
    return f"--- begin content ---\n{json.dumps(json.dumps(value))}\n--- end content ---\n"


def test_lease_waits_while_busy_then_returns():
    pool = FakePool([{"ok": False, "code": "BUSY"}, {"ok": False, "code": "BUSY"}, {"ok": True, "data": LEASE_DATA}])
    slept = []
    assert S.lease_with_wait(pool=pool, sleep=slept.append, clock=lambda: 0.0) == LEASE_DATA
    assert slept == [S.BUSY_POLL_S, S.BUSY_POLL_S]
    assert pool.calls[0] == ("lease",)


def test_lease_gives_up_after_the_wait_with_exit_3():
    pool = FakePool([{"ok": False, "code": "BUSY"}])
    t = [0.0]

    def sleep(s):
        t[0] += s

    with pytest.raises(SystemExit) as e:
        S.lease_with_wait(pool=pool, sleep=sleep, clock=lambda: t[0], wait_s=300, poll_s=60)
    assert e.value.code == 3
    assert len(pool.calls) == 6       # at t = 0, 60, 120, 180, 240, 300


def test_lease_other_refusal_aborts_at_once():
    pool = FakePool([{"ok": False, "code": "BROWSER_DOWN", "message": "down"}])
    with pytest.raises(SystemExit) as e:
        S.lease_with_wait(pool=pool, sleep=lambda s: None, clock=lambda: 0.0)
    assert e.value.code == 1 and len(pool.calls) == 1


@pytest.mark.parametrize("ep", ["http://127.0.0.1:9222", "http://localhost:9222/", "http://[::1]:9333", LEASED_EP])
def test_only_a_loopback_devtools_endpoint_is_ever_connected_to(ep):
    assert S.endpoint_ok(ep)


@pytest.mark.parametrize("ep", ["ws://10.0.0.5:42287/devtools/browser/x", "wss://127.0.0.1:42287/devtools/browser/x",
                                "http://0.0.0.0:9222", "http://192.168.1.4:9222", "https://127.0.0.1:9222",
                                "http://127.0.0.1.example.com:9222", "9222", "", None, 9222])
def test_any_other_endpoint_is_refused(ep):
    assert not S.endpoint_ok(ep)


def test_scrub_removes_every_endpoint():
    out = S.scrub(f"error near {LEASED_EP} and http://127.0.0.1:9222/json")
    assert "ws://" not in out and "http://" not in out and out.count("<endpoint>") == 2


def test_attach_needs_a_headed_chrome_that_is_not_marked_automated():
    assert S.attach_ok(eval_out({"ua": UA, "wd": False}))
    other_os = UA.replace("X11; Linux x86_64", "Macintosh; Intel Mac OS X 14_5")
    assert S.attach_ok(eval_out({"ua": other_os, "wd": False}))      # your own Chrome, on any desktop
    for out in (eval_out({"ua": UA, "wd": True}), eval_out({"ua": UA, "wd": None}),
                eval_out({"ua": UA.replace("Chrome/", "HeadlessChrome/"), "wd": False}), eval_out({"ua": UA}),
                eval_out({"ua": "Mozilla/5.0 Firefox/130.0", "wd": False}), eval_out("x"), ""):
        assert not S.attach_ok(out), out


def test_with_browser_releases_after_a_failure_and_keeps_its_code(monkeypatch):
    events = []

    def attach():
        events.append("attach")
        S._LEASED = True

    monkeypatch.setattr(S, "attach", attach)
    monkeypatch.setattr(S, "release", lambda: events.append("release"))
    assert S.with_browser(lambda: S.needs_you("identity", "wrong channel")) == 4
    assert events == ["attach", "release"]


def test_with_browser_never_lets_a_release_failure_hide_why_it_stopped(monkeypatch):
    monkeypatch.setattr(S, "attach", lambda: setattr(S, "_LEASED", True))
    monkeypatch.setattr(S, "release", lambda: S.H.die("release", "not clean"))
    assert S.with_browser(lambda: S.needs_you("x", "y")) == 4
    assert S.with_browser(lambda: None) == 1     # a clean run, then a dirty release: the release's failure counts


def test_with_browser_skips_release_when_never_held(monkeypatch):
    monkeypatch.setattr(S, "attach", lambda: S.die_code(3, "lease", "busy"))
    monkeypatch.setattr(S, "release", lambda: pytest.fail("released a browser never held"))
    assert S.with_browser(lambda: None) == 3


def test_keep_on_abort_leaves_the_browser_for_an_attended_look(monkeypatch):
    monkeypatch.setenv("ORION_CLOCK_KEEP_ON_ABORT", "1")
    monkeypatch.setattr(S, "attach", lambda: setattr(S, "_LEASED", True))
    monkeypatch.setattr(S, "release", lambda: pytest.fail("released despite keep"))
    assert S.with_browser(lambda: S.H.die("x", "y")) == 1


# ---------- subprocess discipline: argv lists, stdin DEVNULL, a timeout ----------

def test_a_lease_call_runs_the_configured_argv_with_devnull_stdin_and_a_timeout(monkeypatch):
    S.configure(POOL)
    seen = {}

    class R:
        stdout = json.dumps({"ok": True, "data": {}})
        stderr = ""

    def run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return R()

    monkeypatch.setattr(S.subprocess, "run", run)
    assert S._pool("release") == {"ok": True, "data": {}}
    assert seen["argv"] == ["pool", "--json", "release", "b1"] and "shell" not in seen["kw"]
    assert seen["kw"]["stdin"] == S.subprocess.DEVNULL and seen["kw"]["timeout"] > 0
    S._pool("lease")
    assert seen["argv"] == ["pool", "--json", "lease", "b1"]


def test_without_a_pool_there_is_no_lease_call(monkeypatch):
    monkeypatch.setattr(S.subprocess, "run", lambda *a, **k: pytest.fail("ran a command"))
    assert S._pool("lease")["ok"] is False and S._pool("release")["code"] == "NO_COMMAND"


@pytest.mark.parametrize("exc,code", [(S.subprocess.TimeoutExpired(["pool"], 1), "TIMEOUT"),
                                      (FileNotFoundError("pool"), "NOT_RUNNABLE")])
def test_a_lease_call_that_cannot_run_is_a_short_refusal_not_a_crash(monkeypatch, exc, code):
    S.configure(POOL)

    def run(argv, **kw):
        raise exc

    monkeypatch.setattr(S.subprocess, "run", run)
    r = S._pool("lease")
    assert r["ok"] is False and r["code"] == code


@pytest.mark.parametrize("answer", [[1], "x", None, 5])
def test_a_non_object_answer_is_a_refusal(monkeypatch, answer):
    S.configure(POOL)

    class R:
        stdout = json.dumps(answer)
        stderr = ""
    monkeypatch.setattr(S.subprocess, "run", lambda *a, **k: R())
    r = S._pool("lease")
    assert isinstance(r, dict) and r["ok"] is False
    with pytest.raises(SystemExit) as e:
        S.lease_with_wait(pool=lambda *a: answer, sleep=lambda s: None, clock=lambda: 0.0)
    assert e.value.code == 1


# ---------- attach, release, signal ----------

PROBE_OK = eval_out({"ua": UA, "wd": False})


class FakeProc:
    def __init__(self, rc=0, stdout=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, ""


class FakeShell:
    """Fake subprocess.run for attach: records argv lists; agent-browser connect answers `connect_rc`. Anything else
    (a close above all) fails the test."""

    def __init__(self, connect_rc=0):
        self.connect_rc = connect_rc
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if "connect" in argv:
            return FakeProc(self.connect_rc)
        raise AssertionError(f"unexpected subprocess: {argv}")

    def of(self, word):
        return [c for c, _ in self.calls if word in c]


@pytest.fixture
def attach_env(monkeypatch):
    monkeypatch.setattr(S, "_LOCK_FD", None)
    state = {"held_during_lease": None, "probe": PROBE_OK, "lease": LEASE_DATA, "stopped": 0}

    def lease():
        state["held_during_lease"] = S._LEASED
        return state["lease"]

    def stop_sessions():
        state["stopped"] += 1

    monkeypatch.setattr(S, "lease_with_wait", lease)
    monkeypatch.setattr(S, "stop_sessions", stop_sessions)
    monkeypatch.setattr(S.H, "_raw", lambda *a, **k: state["probe"])
    yield state
    if S._LOCK_FD is not None:
        os.close(S._LOCK_FD)
        S._LOCK_FD = None


def test_attach_connects_to_the_configured_endpoint_proves_it_and_names_its_own_session(attach_env, monkeypatch,
                                                                                         capsys):
    sh = FakeShell()
    monkeypatch.setattr(S.subprocess, "run", sh)
    S.attach()
    assert S._LEASED is True and attach_env["held_during_lease"] is None   # no pool: no lease call at all
    assert S.SESSION_RE.fullmatch(S.H.S)
    [c] = sh.of("connect")
    assert c[-1] == SETTINGS.browser_endpoint and S.H.S in c
    kw = sh.calls[0][1]
    assert kw["env"]["AGENT_BROWSER_SOCKET_DIR"] == str(S.socket_dir()) and kw["stdin"] == S.subprocess.DEVNULL
    assert attach_env["stopped"] == 1          # sessions an earlier run left behind are stopped first, under the lock
    assert "ok   attach" in capsys.readouterr().out


def test_two_attaches_never_share_a_session_name(attach_env, monkeypatch):
    monkeypatch.setattr(S.subprocess, "run", FakeShell())
    S.attach()
    first = S.H.S
    S.attach()
    assert S.H.S != first


def test_attach_with_a_pool_connects_to_the_leased_endpoint(attach_env, monkeypatch):
    S.configure(POOL)
    sh = FakeShell()
    monkeypatch.setattr(S.subprocess, "run", sh)
    S.attach()
    assert attach_env["held_during_lease"] is False and S._LEASED is True   # recorded only after the lease answered
    assert sh.of("connect")[0][-1] == LEASED_EP


@pytest.mark.parametrize("lease", [None, "x", {}, {"endpoint": "ws://10.0.0.5:1/devtools/browser/x"},
                                   {"endpoint": "wss://browsers.example.com/cdp?token=x"}])
def test_attach_refuses_a_lease_of_an_unexpected_shape_but_still_marks_it_held(attach_env, monkeypatch, lease):
    S.configure(POOL)
    sh = FakeShell()
    monkeypatch.setattr(S.subprocess, "run", sh)
    attach_env["lease"] = lease
    with pytest.raises(SystemExit) as e:
        S.attach()
    assert e.value.code == 1 and S._LEASED is True and sh.of("connect") == []
    assert S.H.S == S.UNATTACHED


def test_attach_refuses_an_endpoint_off_the_loopback_even_if_settings_were_bypassed(attach_env, monkeypatch):
    sh = FakeShell()
    monkeypatch.setattr(S.subprocess, "run", sh)
    monkeypatch.setattr(S, "ENDPOINT", "http://192.168.1.4:9222")
    with pytest.raises(SystemExit) as e:
        S.attach()
    assert e.value.code == 1 and sh.of("connect") == [] and S.H.S == S.UNATTACHED


def test_attach_connect_failure_aborts_and_never_echoes_output(attach_env, monkeypatch, capsys):
    monkeypatch.setattr(S.subprocess, "run", FakeShell(connect_rc=7))
    with pytest.raises(SystemExit) as e:
        S.attach()
    out = capsys.readouterr().out
    assert e.value.code == 1 and "exit 7" in out and "ws://" not in out and "http://" not in out


@pytest.mark.parametrize("probe", [eval_out({"ua": UA, "wd": True}), "", eval_out({"ua": "HeadlessChrome"})])
def test_attach_needs_the_proof(attach_env, monkeypatch, probe):
    monkeypatch.setattr(S.subprocess, "run", FakeShell())
    attach_env["probe"] = probe
    with pytest.raises(SystemExit) as e:
        S.attach()
    assert e.value.code == 1


def test_attach_second_command_is_locked_out(attach_env, monkeypatch):
    import fcntl
    monkeypatch.setattr(S.subprocess, "run", FakeShell())
    lock = S.driver_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    other = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(other, fcntl.LOCK_EX)
    try:
        with pytest.raises(SystemExit) as e:
            S.attach()
        assert e.value.code == 1 and S._LEASED is False and attach_env["stopped"] == 0
    finally:
        os.close(other)


def pool_answers(monkeypatch, answer):
    monkeypatch.setattr(S, "_pool", lambda *a: answer)


def stopped_sessions(monkeypatch):
    calls = []
    monkeypatch.setattr(S, "stop_sessions", lambda: calls.append(1))
    return calls


@pytest.mark.parametrize("stop", ["clean", "signal", "kill"])
def test_release_of_a_lease_gives_it_back_then_stops_my_sessions(monkeypatch, stop):
    S.configure(POOL)
    calls = stopped_sessions(monkeypatch)
    pool_answers(monkeypatch, {"ok": True, "data": {"stop": stop}})
    S._LEASED, S.H.S = True, "oclock-0123abcd"
    if stop == "clean":
        S.release()
    else:
        with pytest.raises(SystemExit):
            S.release()
    assert calls == [1] and S._LEASED is False and S.H.S == S.UNATTACHED


@pytest.mark.parametrize("data", [{"stop": "weird"}, {}, None, "clean", {"stop": None}])
def test_release_of_a_lease_with_an_unknown_stop_stops_nothing_and_fails(monkeypatch, data):
    S.configure(POOL)
    calls = stopped_sessions(monkeypatch)
    pool_answers(monkeypatch, {"ok": True, "data": data})
    S._LEASED = True
    with pytest.raises(SystemExit) as e:
        S.release()
    assert e.value.code == 1 and calls == [] and S._LEASED is True


def test_release_refusal_fails(monkeypatch):
    S.configure(POOL)
    pool_answers(monkeypatch, {"ok": False, "code": "NOT_HELD", "message": "no"})
    S._LEASED = True
    with pytest.raises(SystemExit) as e:
        S.release()
    assert e.value.code == 1


def test_release_without_a_pool_stops_my_session_and_leaves_the_browser_running(monkeypatch, capsys):
    calls = stopped_sessions(monkeypatch)
    monkeypatch.setattr(S, "_pool", lambda *a: pytest.fail("called a pool"))
    monkeypatch.setattr(S.subprocess, "run", lambda *a, **k: pytest.fail("ran a command"))
    S._LEASED, S.H.S = True, "oclock-0123abcd"
    S.release()
    assert calls == [1] and S._LEASED is False and S.H.S == S.UNATTACHED
    assert "the browser keeps running" in capsys.readouterr().out


def test_with_browser_keeps_its_code_when_cleanup_fails(monkeypatch):
    monkeypatch.setattr(S, "stop_session", lambda name: (_ for _ in ()).throw(OSError("stuck")))
    monkeypatch.setattr(S, "session_names", lambda: {"oclock-0123abcd"})
    monkeypatch.setattr(S, "attach", lambda: setattr(S, "_LEASED", True))
    assert S.with_browser(lambda: S.needs_you("x", "y")) == 4 and S._LEASED is False


def test_the_driver_never_closes_the_browser():
    """`agent-browser close` on a session attached to your own Chrome can close that Chrome and every tab in it: the
    driver ends its sessions by stopping their daemons and closes only the tab it opened (a batch step)."""
    src = (RELEASE / "studio.py").read_text() + (RELEASE / "pinned.py").read_text()
    assert not re.search(r'"agent-browser"[^\]\n]*"close"', src)
    assert not re.search(r'"(?:close|quit|exit)"\s*\]', src.replace('["tab", "close"]', ""))


# ---- my sessions: pid files in my own socket folder; stopped by their daemon, never closed ----

def make_session(name, pid):
    d = S.socket_dir()
    d.mkdir(parents=True, exist_ok=True)
    for suffix, text in ((".pid", f"{pid}\n"), (".sock", ""), (".stream", "9"), (".version", "0.25")):
        (d / (name + suffix)).write_text(text)
    return d


def test_the_socket_folder_is_my_own():
    assert S.socket_dir() == Path(os.environ["XDG_RUNTIME_DIR"]) / "orion-on-the-clock-browser"
    assert S.H.ENV["AGENT_BROWSER_SOCKET_DIR"] == str(S.socket_dir())


def test_without_a_runtime_folder_the_sessions_live_in_the_state_folder(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert S.socket_dir() == paths.state_root() / "browser"


def test_session_names_are_only_my_own_shape():
    make_session("oclock-0123abcd", 4242)
    make_session("oclock-unattached", 4243)
    make_session("someone-elses", 4244)
    (S.socket_dir() / "oclock-89abcdef.sock").write_text("")     # no pid file: not a session
    assert S.session_names() == {"oclock-0123abcd"}


def test_stopping_a_real_agent_browser_daemon_ends_it_and_removes_its_files():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "agent-browser-daemon"])
    try:
        d = make_session("oclock-0123abcd", proc.pid)
        assert S.stop_session("oclock-0123abcd") is True
        assert proc.wait(timeout=10) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert not list(d.glob("oclock-0123abcd.*"))


def test_a_pid_that_is_not_agent_browser_is_never_signalled():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        d = make_session("oclock-0123abcd", proc.pid)
        assert S.stop_session("oclock-0123abcd") is False
        assert proc.poll() is None                               # still running: a reused pid is someone else's
        assert not list(d.glob("oclock-0123abcd.*"))             # the stale files of my session are gone
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("pid", ["", "abc", "0", "1", "-5"])
def test_a_bad_pid_file_signals_nothing(monkeypatch, pid):
    monkeypatch.setattr(S.os, "kill", lambda *a: pytest.fail("signalled"))
    d = make_session("oclock-0123abcd", pid)
    assert S.stop_session("oclock-0123abcd") is False
    assert not list(d.glob("oclock-0123abcd.*"))


def test_a_daemon_that_ignores_sigterm_is_killed(monkeypatch):
    import signal as sig
    sent = []
    make_session("oclock-0123abcd", 4242)
    monkeypatch.setattr(S, "_process_command", lambda pid: "/opt/agent-browser/bin/agent-browser-linux-x64")
    monkeypatch.setattr(S, "_alive", lambda pid: True)
    monkeypatch.setattr(S.os, "kill", lambda pid, s: sent.append((pid, s)))
    slept = []
    assert S.stop_session("oclock-0123abcd", sleep=slept.append) is True
    assert sent == [(4242, sig.SIGTERM), (4242, sig.SIGKILL)] and len(slept) == 50


def test_stop_sessions_stops_every_session_of_mine_and_survives_a_failure(monkeypatch):
    make_session("oclock-0123abcd", 4242)
    make_session("oclock-89abcdef", 4243)
    seen = []

    def stop(name):
        seen.append(name)
        raise OSError("stuck")
    monkeypatch.setattr(S, "stop_session", stop)
    S.stop_sessions()
    assert seen == ["oclock-0123abcd", "oclock-89abcdef"]


def test_sigterm_releases_the_browser_and_exits(monkeypatch):
    import signal
    events = []
    monkeypatch.setattr(S, "attach", lambda: setattr(S, "_LEASED", True))
    monkeypatch.setattr(S, "release", lambda: events.append("release"))
    before = signal.getsignal(signal.SIGTERM)
    code = S.with_browser(lambda: os.kill(os.getpid(), signal.SIGTERM))
    assert events == ["release"] and code == 128 + signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) == before         # handler restored


def test_sigterm_releases_even_with_keep_on_abort(monkeypatch):
    import signal
    events = []
    monkeypatch.setenv("ORION_CLOCK_KEEP_ON_ABORT", "1")
    monkeypatch.setattr(S, "attach", lambda: setattr(S, "_LEASED", True))
    monkeypatch.setattr(S, "release", lambda: events.append("release"))
    S.with_browser(lambda: os.kill(os.getpid(), signal.SIGTERM))
    assert events == ["release"]


def test_wait_constants_are_pinned():
    assert (S.BUSY_WAIT_S, S.BUSY_POLL_S) == (40 * 60, 60)


# ---- configuration: everything channel and browser comes from settings.toml ----

def test_configure_takes_the_channels_and_the_browser_from_settings():
    assert (S.CHAN, S.CHANNEL_NAME, S.HOME_CHAN) == (CHAN, "Demo Channel", HOME_CHAN)
    assert (S.DAILY_HOME_URL, S.HOME_URL) == (DAILY_HOME, HOME_HOME)
    assert S.H.CHAN == CHAN and S.ENDPOINT == SETTINGS.browser_endpoint and S.LEASE_CMD == ()


def test_a_missing_channel_is_a_settings_stop_and_nothing_else_is_chosen():
    from daily.settings import SettingsError
    with pytest.raises(SettingsError) as e:
        S.configure(dataclasses.replace(SETTINGS, channel_id=None))
    assert str(e.value) == "missing:channel.id"


def test_no_home_channel_or_the_same_channel_means_no_switch_back():
    for home in (None, CHAN):
        S.configure(dataclasses.replace(SETTINGS, home_channel_id=home))
        assert S.HOME_CHAN is None and S.HOME_URL is None


def test_the_commands_load_settings_and_a_bad_file_is_exit_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(S, "load_config", REAL_LOAD_CONFIG)   # the real one, reading <config>/settings.toml
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv(paths.ENV_CONFIG, str(cfg))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert S.main(["x", "verify", str(tmp_path)]) == 1
    assert "ABORT at settings: settings_missing" in capsys.readouterr().out
    (cfg / "settings.toml").write_text('timezone = "UTC"\n\n[show]\nseries = "S"\nhost_name = "H"\ndisclosure = "D"\n'
                                       '\n[github]\nowner = "demo-owner"\n')
    assert S.main(["x", "verify", str(tmp_path)]) == 1
    assert "ABORT at settings: missing:channel.id" in capsys.readouterr().out


# ---------- channel pin, Save only on positive evidence ----------

@pytest.mark.parametrize("seen", [CHAN + "x", CHAN[:-1], CHAN[:5], CHAN.lower(), HOME_CHAN, "", None])
def test_identity_on_anything_but_this_channel_needs_you(browser, seen):
    browser({"CHANNEL_ID": seen})
    with pytest.raises(SystemExit) as e:
        S.identity()
    assert e.value.code == 4


def test_identity_on_this_channel_passes(browser):
    browser({"CHANNEL_ID": CHAN})
    assert S.identity() == CHAN


NODE_PRELUDE = """
const dom = %s; let clicks = 0; const rect = w => () => ({width: w, height: w});
const btn = dom.button && {innerText: dom.button.label, hasAttribute: n => n === 'disabled' && !!dom.button.disabled,
  getAttribute: n => (n === 'aria-disabled' && dom.button.disabled) ? 'true' : null, click() { clicks++; }};
const d = {getBoundingClientRect: rect(1), querySelector(sel) {
  if (sel === '#done-button') return btn || null;
  const m = sel.match(/\\[name=(\\w+)\\]/);
  if (m && sel.includes('#privacy-radios')) return m[1] in dom.radios ? {getAttribute: a => dom.radios[m[1]]} : null;
  return null; }};
global.document = {querySelector: s => s === 'ytcp-uploads-dialog' ? d : null,
  querySelectorAll: s => s.includes('ytcp-visibility-scheduler') ? [{getBoundingClientRect: rect(dom.sched ? 1 : 0)}] : []};
const result = eval(%s);
console.log(JSON.stringify({result, clicks}));
"""
SAFE = {"radios": {"PRIVATE": "true", "PUBLIC": "false", "UNLISTED": "false"}, "sched": False,
        "button": {"label": "Save", "disabled": False}}
SAFE_PUBLIC = {"radios": {"PRIVATE": "false", "PUBLIC": "true", "UNLISTED": "false"}, "sched": False,
               "button": {"label": "Publish", "disabled": False}}
SAFES = {"PRIVATE": SAFE, "PUBLIC": SAFE_PUBLIC}


def node_run(js, dom):
    need_node()
    prog = NODE_PRELUDE % (json.dumps(dom), json.dumps(js))
    r = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-300:]
    out = json.loads(r.stdout)
    if isinstance(out["result"], str):
        out["result"] = json.loads(out["result"])
    return out


def test_read_vis_is_read_only_text():
    banned = re.compile(r"click|dispatchEvent|setAttribute|focus|submit|\.value =|\.checked =")
    assert not banned.search(S.READ_VIS)


def dom_with(base=SAFE, **kw):
    d = json.loads(json.dumps(base))
    for k, v in kw.items():
        if k == "radios":
            d["radios"].update(v)
        elif k == "button" and v is not None:
            d["button"] = dict(d["button"], **v)
        else:
            d[k] = v
    return d


def test_read_vis_reads_each_key_from_its_own_source():
    base = node_run(S.READ_VIS, SAFE)
    assert base["clicks"] == 0 and base["result"] == {"private": "true", "public": "false", "unlisted": "false",
        "scheduler_open": False, "done_label": "Save", "done_disabled": False,
        "safe_to_save": {"PRIVATE": True, "PUBLIC": False}}
    pub = node_run(S.READ_VIS, SAFE_PUBLIC)
    assert pub["clicks"] == 0 and pub["result"] == {"private": "false", "public": "true", "unlisted": "false",
        "scheduler_open": False, "done_label": "Publish", "done_disabled": False,
        "safe_to_save": {"PRIVATE": False, "PUBLIC": True}}
    assert node_run(S.READ_VIS, dom_with(radios={"PRIVATE": "PV"}))["result"]["private"] == "PV"
    r = node_run(S.READ_VIS, dom_with(radios={"PUBLIC": "PB"}))["result"]
    assert (r["public"], r["private"], r["unlisted"]) == ("PB", "true", "false")
    r = node_run(S.READ_VIS, dom_with(radios={"UNLISTED": "UL"}))["result"]
    assert (r["unlisted"], r["private"], r["public"]) == ("UL", "true", "false")
    assert node_run(S.READ_VIS, dom_with(sched=True))["result"]["scheduler_open"] is True
    r = node_run(S.READ_VIS, dom_with(button={"label": "Publish"}))["result"]
    assert r["done_label"] == "Publish" and r["scheduler_open"] is False
    assert node_run(S.READ_VIS, dom_with(button={"disabled": True}))["result"]["done_disabled"] is True
    assert node_run(S.READ_VIS, dom_with(button=None, radios={}))["result"]["done_label"] is None


UNSAFE = [dom_with(radios={"PRIVATE": "false"}), dom_with(radios={"PUBLIC": "true"}), dom_with(radios={"UNLISTED": "true"}),
          dom_with(sched=True), dom_with(button={"label": "Publish"}), dom_with(button={"label": "Schedule"}),
          dom_with(button={"disabled": True}), dom_with(radios={"PRIVATE": "missing"}), dom_with(button={"label": ""})]
UNSAFE_PUBLIC = [dom_with(SAFE_PUBLIC, radios={"PUBLIC": "false"}), dom_with(SAFE_PUBLIC, radios={"PRIVATE": "true"}),
                 dom_with(SAFE_PUBLIC, radios={"UNLISTED": "true"}), dom_with(SAFE_PUBLIC, sched=True),
                 dom_with(SAFE_PUBLIC, button={"label": "Save"}), dom_with(SAFE_PUBLIC, button={"label": "Schedule"}),
                 dom_with(SAFE_PUBLIC, button={"label": ""}), dom_with(SAFE_PUBLIC, button={"label": "Publish now"}),
                 dom_with(SAFE_PUBLIC, button={"disabled": True}), dom_with(SAFE_PUBLIC, radios={"PUBLIC": "missing"}),
                 dom_with(SAFE_PUBLIC, button=None, radios={})]
UNSAFES = {"PRIVATE": UNSAFE, "PUBLIC": UNSAFE_PUBLIC}


@pytest.mark.parametrize("want,dom", [(w, d) for w in WANTS for d in UNSAFES[w]])
def test_safe_to_save_is_false_unless_everything_is_proven(want, dom):
    assert node_run(S.READ_VIS, dom)["result"]["safe_to_save"][want] is False


def test_unlisted_is_never_safe_to_save_and_has_no_entry():
    r = node_run(S.READ_VIS, dom_with(radios={"PRIVATE": "false", "UNLISTED": "true"}))["result"]
    assert r["safe_to_save"] == {"PRIVATE": False, "PUBLIC": False}


def saved(state=GOOD, clicked=True):
    return {"state": state, "clicked": clicked}


PUBLISHED = "Video published Share this video https://youtube.com/shorts/vid12345678 Close"


def commit_js(browser, want="PRIVATE"):
    fb = browser({'"Close"': True, "done_label": saved(GOODS[want]), "Video (saved": Seq([None, "Video saved Close"]),
                  "Video published": Seq([None, PUBLISHED]), "Got it": False, "'gone'": "open", "role=dialog": []})
    S.commit_visibility(want)
    return next(js for js in fb.calls if "done_label" in js)


@pytest.mark.parametrize("want", WANTS)
def test_commit_script_clicks_exactly_once_and_only_when_safe(browser, want):
    js = commit_js(browser, want)
    ok = node_run(js, SAFES[want])
    assert ok["clicks"] == 1 and ok["result"]["clicked"] is True and ok["result"]["state"][want.lower()] == "true"
    for dom in UNSAFES[want] + [SAFES[OTHER[want]]]:
        bad = node_run(js, dom)
        assert bad["clicks"] == 0 and bad["result"]["clicked"] is False, dom


@pytest.mark.parametrize("want", WANTS)
def test_commit_refuses_without_evidence_and_never_reads_a_confirmation(browser, want):
    g = GOODS[want]
    for state in (dict(g, **{OTHER[want].lower(): "true"}), dict(g, unlisted="true"), dict(g, done_label=DONE[OTHER[want]]),
                  dict(g, done_label="Schedule"), dict(g, done_label=""), dict(g, done_disabled=True),
                  dict(g, scheduler_open=True), GOODS[OTHER[want]], None):
        fb = browser({"done_label": saved(state, False), "Video (saved": None, "Video published": None})
        with pytest.raises(SystemExit) as e:
            S.commit_visibility(want)
        assert e.value.code == 1
        assert "done_label" in fb.calls[-1], state       # stopped right after the script


@pytest.mark.parametrize("want", WANTS)
def test_commit_dies_when_the_script_did_not_click(browser, want):
    browser({"done_label": saved(GOODS[want], False), "Video (saved": None, "Video published": None})
    with pytest.raises(SystemExit) as e:
        S.commit_visibility(want)
    assert e.value.code == 1


@pytest.mark.parametrize("want", WANTS)
def test_commit_dies_loudly_if_it_clicked_on_state_python_rejects(browser, want):
    for state in (dict(GOODS[want], **{OTHER[want].lower(): "true"}), GOODS[OTHER[want]]):
        fb = browser({"done_label": saved(state, True), "Video (saved": "Video saved Close", "Video published": None})
        with pytest.raises(SystemExit) as e:
            S.commit_visibility(want)
        assert e.value.code == 1 and "done_label" in fb.calls[-1]


@pytest.mark.parametrize("want", ["PRIVATE"])
def test_commit_saves_on_evidence_and_returns_the_new_confirmation(browser, want):
    fb = browser({"done_label": saved(GOODS[want]), "Video (saved": Seq(["Video saved OLD", "Video saved Close"])})
    assert S.commit_visibility(want) == "Video saved Close"
    assert sum("done_label" in js for js in fb.calls) == 1


@pytest.mark.parametrize("want", ["PRIVATE"])
def test_commit_confirmation_is_read_before_the_click(browser, want):
    fb = browser({"done_label": saved(GOODS[want]), "Video (saved": Seq([None, "Video saved Close"])})
    S.commit_visibility(want)
    first_conf = next(i for i, js in enumerate(fb.calls) if "Video (saved" in js)
    script = next(i for i, js in enumerate(fb.calls) if "done_label" in js)
    assert first_conf < script


@pytest.mark.parametrize("want", ["PRIVATE"])
def test_commit_a_stale_confirmation_is_not_a_save(browser, want):
    browser({"done_label": saved(GOODS[want]), "Video (saved": "Video saved OLD", "Got it": False, "'gone'": "open",
             "role=dialog": []})
    with pytest.raises(SystemExit) as e:
        S.commit_visibility(want)
    assert e.value.code == 1


@pytest.mark.parametrize("want", ["PRIVATE"])
def test_commit_no_confirmation_and_wizard_still_open_exits_1(browser, want):
    browser({"done_label": saved(GOODS[want]), "Video (saved": None, "Got it": False, "'gone'": "open", "role=dialog": []})
    with pytest.raises(SystemExit) as e:
        S.commit_visibility(want)
    assert e.value.code == 1


@pytest.mark.parametrize("want", ["PRIVATE"])
def test_commit_unknown_wizard_state_is_not_evidence(browser, want):
    browser({"done_label": saved(GOODS[want]), "Video (saved": None, "Got it": False, "'gone'": None, "role=dialog": []})
    with pytest.raises(SystemExit) as e:
        S.commit_visibility(want)
    assert e.value.code == 1


@pytest.mark.parametrize("want", ["PRIVATE"])
def test_commit_wizard_gone_counts_as_saved(browser, want):
    browser({"done_label": saved(GOODS[want]), "Video (saved": None, "Got it": False, "'gone'": "gone"})
    assert S.commit_visibility(want)


@pytest.mark.parametrize("want", ["PRIVATE"])
def test_commit_acknowledges_the_checking_notice_then_reads_the_new_confirmation(browser, want):
    fb = browser({"done_label": saved(GOODS[want]), "Video (saved": Seq([None, None, "Video saved Close"]), "Got it": True})
    assert S.commit_visibility(want) == "Video saved Close"
    assert any("Got it" in js for js in fb.calls)


@pytest.mark.parametrize("want", ["UNLISTED", "SCHEDULED", "public", "private", "", None, ["PUBLIC"]])
def test_commit_refuses_anything_outside_the_two_before_the_browser(browser, want):
    fb = browser({})
    with pytest.raises(SystemExit) as e:
        S.commit_visibility(want)
    assert e.value.code == 1 and fb.calls == []


def test_commit_refuses_a_lookalike_str_before_the_browser(browser):
    fb = browser({})
    with pytest.raises(SystemExit):
        S.commit_visibility(Sneaky("PUBLIC"))
    assert fb.calls == []


def test_js_click_is_gone():
    assert not hasattr(S, "js_click")


def test_the_private_only_names_are_gone():
    assert not hasattr(S, "private_ok") and not hasattr(S, "commit_private") and not hasattr(S, "VISIBILITY")


# ---------- radio: only the two named details radios ----------

KIDS, ALTERED = "VIDEO_MADE_FOR_KIDS_NOT_MFK", "VIDEO_HAS_ALTERED_CONTENT_YES"


def test_radios_are_exactly_two():
    assert S.RADIOS == frozenset({KIDS, ALTERED})


@pytest.mark.parametrize("name", ["PUBLIC", "UNLISTED", "SCHEDULE", "PRIVATE", "", "public",
                                  'x"], [name="PUBLIC', "VIDEO_HAS_ALTERED_CONTENT_NO", KIDS + " ", None])
def test_radio_refuses_any_other_name_before_the_browser(browser, name):
    fb = browser({})
    with pytest.raises(SystemExit) as e:
        S.radio(name, "x")
    assert e.value.code == 1 and fb.calls == []


def test_radio_refuses_in_the_click_helper_too(browser):
    fb = browser({})
    with pytest.raises(SystemExit):
        S._click_radio("PUBLIC")
    assert fb.calls == []


def test_radio_already_checked_does_not_click(browser):
    fb = browser({"aria-checked": "true"})
    S.radio(ALTERED, "altered")
    assert len(fb.calls) == 1
    assert json.dumps(f"tp-yt-paper-radio-button[name={json.dumps(ALTERED)}]") in fb.calls[0]
    assert "#privacy-radios" in fb.calls[0] and "closest" in fb.calls[0]


def test_radio_clicks_then_proves_it_checked(browser):
    fb = browser({"aria-checked": Seq(["false", "true"]), ".click()": True})
    S.radio(KIDS, "kids")
    assert any(".click()" in js for js in fb.calls)


def test_radio_dies_when_it_never_reads_checked(browser):
    browser({"aria-checked": "false", ".click()": True})
    with pytest.raises(SystemExit) as e:
        S.radio(KIDS, "kids")
    assert e.value.code == 1


def test_pin_allows_a_radio_call_with_a_named_literal():
    src = (RELEASE / "studio.py").read_text()
    assert violations(src + "\ndef ok():\n    radio('VIDEO_HAS_ALTERED_CONTENT_YES', 'x')\n") == []


ACK_PRELUDE = """
const dialogs = %s; let clicked = [];
const mk = (t, w) => ({innerText: t, getBoundingClientRect: () => ({width: w, height: w})});
const dlg = x => Object.assign(mk(x.text, x.w), {querySelectorAll: () => x.buttons.map(b =>
  Object.assign(mk(b.label, b.w === undefined ? 1 : b.w), {click() { clicked.push(x.text + '|' + b.label); }}))});
global.document = {querySelectorAll: () => dialogs.map(dlg)};
const result = eval(%s);
console.log(JSON.stringify({result, clicked}));
"""


def ack_run(browser, dialogs):
    need_node()
    fb = browser({"Got it": True})
    S.ack_notice()
    js = fb.calls[0]
    r = subprocess.run([NODE, "-e", ACK_PRELUDE % (json.dumps(dialogs), json.dumps(js))], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-300:]
    return json.loads(r.stdout)


def test_ack_clicks_only_got_it_on_the_checking_notice(browser):
    notice = {"text": "Still checking your content", "w": 1, "buttons": [{"label": "Got it"}, {"label": "Publish"}]}
    assert ack_run(browser, [notice])["clicked"] == ["Still checking your content|Got it"]
    other = {"text": "Publish your video?", "w": 1, "buttons": [{"label": "Got it"}, {"label": "Publish"}]}
    assert ack_run(browser, [other])["clicked"] == []
    hidden = dict(notice, w=0)
    assert ack_run(browser, [hidden])["clicked"] == []
    nobutton = {"text": "Still checking your content", "w": 1, "buttons": [{"label": "Publish"}]}
    r = ack_run(browser, [nobutton])
    assert r["clicked"] == [] and r["result"] is False


# ---------- the upload flow, verify and the CLI ----------

def day_dir(tmp_path, day=DAY):
    d = tmp_path / day
    d.mkdir(exist_ok=True)
    return d


def item_in(work, **over):
    f = work / "episode.mp4"
    f.write_bytes(b"not really a video")
    item = {"id": f"daily-{work.name}", "channel": CHAN, "file": str(f),
            "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
            "title": "Two agents, one browser pool", "description": "What got built yesterday."}
    item.update(over)
    (work / "upload-item.json").write_text(json.dumps(item))
    return item


def no_browser(monkeypatch):
    monkeypatch.setattr(S, "attach", lambda: pytest.fail("attached"))
    monkeypatch.setattr(S, "with_browser", lambda *a: pytest.fail("leased a browser"))


def test_main_usage_exits_2():
    assert S.main(["studio.py"]) == 2
    assert S.main(["studio.py", "upload"]) == 2
    assert S.main(["studio.py", "publish", "x"]) == 2
    assert S.main(["studio.py", "verify"]) == 2
    assert S.main(["studio.py", "upload", "a", "b"]) == 2


def test_upload_is_a_noop_once_verified(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item = item_in(w)
    S.save(w, day=DAY, subject_sha256=item["sha256"], video_id="vid12345678", stage="private_verified")
    no_browser(monkeypatch)
    assert S.cmd_upload(w) == 0
    Path(item["file"]).unlink()            # a finished day stays done after the staging copy is cleaned up
    assert S.cmd_upload(w) == 0


def test_same_day_other_subject_needs_you_before_any_browser(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w)
    S.save(w, day=DAY, subject_sha256="f" * 64, video_id="vid12345678", stage="uploaded")
    no_browser(monkeypatch)
    with pytest.raises(SystemExit) as e:
        S.cmd_upload(w)
    assert e.value.code == S.EXIT_NEEDS_YOU == 4


@pytest.mark.parametrize("rec", [{"day": "2026-10-15", "subject_sha256": "SAME"},
                                 {"stage": "uploaded", "video_id": "vid12345678"}])     # other day / no identity at all
def test_receipt_for_another_day_or_without_identity_needs_you(tmp_path, monkeypatch, rec):
    w = day_dir(tmp_path)
    item = item_in(w)
    S.save(w, **{k: (item["sha256"] if v == "SAME" else v) for k, v in rec.items()})
    no_browser(monkeypatch)
    with pytest.raises(SystemExit) as e:
        S.cmd_upload(w)
    assert e.value.code == 4


def test_corrupt_receipt_needs_you(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w)
    (w / "upload.json").write_text("{not json")
    no_browser(monkeypatch)
    with pytest.raises(SystemExit) as e:
        S.cmd_upload(w)
    assert e.value.code == 4


def test_a_started_upload_with_no_video_id_needs_you(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item = item_in(w)
    S.save(w, day=DAY, subject_sha256=item["sha256"], stage="upload_started")
    no_browser(monkeypatch)
    with pytest.raises(SystemExit) as e:
        S.cmd_upload(w)
    assert e.value.code == 4


def test_upload_refuses_a_bad_item_before_any_browser(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w, channel=HOME_CHAN)
    no_browser(monkeypatch)
    with pytest.raises(SystemExit) as e:
        S.cmd_upload(w)
    assert e.value.code == 1


def test_upload_needs_a_day_named_work_dir_and_an_item(tmp_path, monkeypatch):
    no_browser(monkeypatch)
    other = tmp_path / "scratch"
    other.mkdir()
    item_in(other)
    for w in (other, day_dir(tmp_path)):          # not a day / a day with no upload-item.json
        with pytest.raises(SystemExit) as e:
            S.cmd_upload(w)
        assert e.value.code == 1


def test_item_for_another_day_is_refused(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w, id="daily-2026-10-15")
    no_browser(monkeypatch)
    with pytest.raises(SystemExit) as e:
        S.cmd_upload(w)
    assert e.value.code == 1


def test_verify_needs_an_uploaded_video(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w)
    no_browser(monkeypatch)
    with pytest.raises(SystemExit) as e:
        S.cmd_verify(w)
    assert e.value.code == 1


def test_verify_refuses_a_receipt_for_another_subject(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w)
    S.save(w, day=DAY, subject_sha256="f" * 64, video_id="vid12345678", stage="saved")
    no_browser(monkeypatch)
    with pytest.raises(SystemExit) as e:
        S.cmd_verify(w)
    assert e.value.code == 4


# ---- the flow, with every page step faked ----

class Flow:
    """Records the order of the page steps; each is a fake that can also inspect the receipt at that moment."""

    def __init__(self, work, monkeypatch, verify_pass=True, vid="vid12345678", swap=False, unconfirmed=False,
                 kill_after_click=False):
        self.work, self.events, self.vid, self.verify_pass, self.swap = work, [], vid, verify_pass, swap
        self.unconfirmed, self.kill_after_click = unconfirmed, kill_after_click
        self.stage_at = {}
        m = monkeypatch
        m.setattr(S, "with_browser", self.with_browser)
        m.setattr(S, "studio_tab", lambda rec=None: self.events.append("tab"))
        m.setattr(S, "upload_file", self.upload_file)
        m.setattr(S, "resume", lambda v: self.events.append(f"resume {v}"))
        m.setattr(S, "details", self.details)
        m.setattr(S, "wait_uploaded", lambda: self.events.append("wait"))
        m.setattr(S, "to_visibility", lambda: self.events.append("to_vis"))
        m.setattr(S, "set_visibility", lambda n: self.events.append(f"vis {n}"))
        m.setattr(S, "commit_visibility", self.commit)
        m.setattr(S, "close_saved_dialog", lambda: self.events.append("close"))
        m.setattr(S, "verify_one", self.verify)
        S.H.VIDEO_ID = None

    @staticmethod
    def with_browser(fn, *a):        # the real one turns a stop into its exit code
        try:
            fn(*a)
        except SystemExit as e:
            return e.code
        return 0

    def upload_file(self, item, before_file=None):
        self.events.append("upload_file")
        assert before_file is not None
        if self.swap:
            Path(item["file"]).write_bytes(b"swapped after the check")
        before_file()
        self.stage_at["before_file"] = S.receipt(self.work).get("stage")
        self.stage_at["claimed_visibility"] = S.receipt(self.work).get("visibility")
        return self.vid

    def details(self, item):
        self.events.append("details")
        self.stage_at["details"] = dict(S.receipt(self.work))

    def commit(self, want, on_attempt=None):
        self.events.append("commit")
        self.stage_at["commit"] = S.receipt(self.work).get("stage")
        self.saved_as = want
        assert on_attempt is not None
        on_attempt()                                  # the real one calls it just before the click is dispatched
        self.stage_at["after_click"] = dict(S.receipt(self.work))
        if self.kill_after_click:
            raise S._Terminated(143)                  # SIGTERM between the click and the saved stage
        return "Video saved"

    def verify(self, item, vid, vis):
        self.events.append("verify")
        self.verified_as = vis
        if self.unconfirmed:
            return {"pass": False, "checks": {"x": True, "oEmbed answers 200 (public)": False}, "oembed": 404,
                    "visibility": "Public", "at": "t", "unconfirmed": True}
        return {"pass": self.verify_pass, "checks": {"x": self.verify_pass}, "oembed": 401, "visibility": "Private", "at": "t"}


def test_fresh_upload_runs_every_step_in_order_and_ends_private_verified(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item = item_in(w)
    f = Flow(w, monkeypatch)
    assert S.cmd_upload(w) == 0
    assert f.events == ["tab", "upload_file", "details", "wait", "to_vis", "vis PRIVATE", "commit", "close", "verify"]
    assert f.stage_at["before_file"] == "upload_started"      # the claim is on disk before any byte goes up
    assert f.stage_at["claimed_visibility"] == "PRIVATE"
    assert f.stage_at["commit"] == "details"
    r = S.receipt(w)
    assert (r["stage"], r["video_id"], r["day"], r["subject_sha256"]) == ("private_verified", f.vid, DAY, item["sha256"])
    assert r["verify"]["pass"] is True and r["studio_url"] == f"https://studio.youtube.com/video/{f.vid}/edit"


def test_a_rerun_of_a_finished_day_does_not_touch_a_browser(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w)
    Flow(w, monkeypatch)
    assert S.cmd_upload(w) == 0
    no_browser(monkeypatch)
    assert S.cmd_upload(w) == 0


def test_upload_resumes_a_draft_without_a_second_upload(tmp_path, monkeypatch):
    for stage in ("uploaded", "details"):
        w = day_dir(tmp_path, "2026-10-11" if stage == "details" else "2026-10-12")
        item = item_in(w)
        S.save(w, day=w.name, subject_sha256=item["sha256"], video_id="vid12345678", stage=stage)
        f = Flow(w, monkeypatch)
        assert S.cmd_upload(w) == 0
        assert "upload_file" not in f.events and "resume vid12345678" in f.events
        assert f.events.index("commit") < f.events.index("verify")


def test_a_saved_draft_is_only_verified_never_saved_again(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item = item_in(w)
    S.save(w, day=DAY, subject_sha256=item["sha256"], video_id="vid12345678", stage="saved")
    f = Flow(w, monkeypatch)
    assert S.cmd_upload(w) == 0
    assert f.events == ["tab", "verify"]
    assert S.receipt(w)["stage"] == "private_verified"


def test_a_failed_verify_exits_1_and_keeps_the_saved_stage(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w)
    Flow(w, monkeypatch, verify_pass=False)
    assert S.cmd_upload(w) == 1
    r = S.receipt(w)
    assert r["stage"] == "saved" and r["verify"]["pass"] is False
    f2 = Flow(w, monkeypatch, verify_pass=True)       # the rerun verifies only: no second upload, no second save
    assert S.cmd_upload(w) == 0
    assert f2.events == ["tab", "verify"]


def test_the_upload_holds_the_work_lock_for_its_whole_duration(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w)
    seen = []
    Flow(w, monkeypatch)

    def locked_run(fn, *a):
        with pytest.raises(S.ReceiptError) as e:
            with S.work_lock(w):
                pass
        seen.append(e.value.code)
        fn(*a)
        return 0

    monkeypatch.setattr(S, "with_browser", locked_run)
    assert S.cmd_upload(w) == 0
    assert seen == ["upload_locked"]
    with S.work_lock(w):          # and it is released afterwards
        pass


def test_a_second_run_while_locked_stops_at_once(tmp_path, monkeypatch, capsys):
    w = day_dir(tmp_path)
    item_in(w)
    no_browser(monkeypatch)
    with S.work_lock(w):
        assert S.main(["studio.py", "upload", str(w)]) == 1
    assert "upload_locked" in capsys.readouterr().out


def test_main_maps_receipt_errors_and_surprises_to_codes_without_leaking_text(tmp_path, monkeypatch, capsys):
    def ident(w, *vis):
        raise S.ReceiptError("receipt_identity_changed")

    def boom(w, *vis):
        raise RuntimeError("ws://127.0.0.1:1/devtools/browser/abc")

    monkeypatch.setattr(S, "cmd_upload", ident)
    assert S.main(["x", "upload", str(tmp_path)]) == 4
    monkeypatch.setattr(S, "cmd_upload", boom)
    assert S.main(["x", "upload", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "RuntimeError" in out and "devtools" not in out


def test_verify_command_records_the_read_and_exits_by_it(tmp_path, monkeypatch, capsys):
    w = day_dir(tmp_path)
    item = item_in(w)
    S.save(w, day=DAY, subject_sha256=item["sha256"], video_id="vid12345678", stage="saved")
    f = Flow(w, monkeypatch)
    assert S.cmd_verify(w) == 0
    assert f.events == ["tab", "verify"] and "PASS" in capsys.readouterr().out
    assert S.receipt(w)["verify"]["pass"] is True
    assert S.receipt(w)["stage"] == "saved"             # verify is a read: it never advances the stage
    Flow(w, monkeypatch, verify_pass=False)
    assert S.cmd_verify(w) == 1


# ---- verify_one: a FRESH read plus a refusing oEmbed ----

def verify_env(monkeypatch, got, status):
    calls = []
    monkeypatch.setattr(S.H, "ab", lambda *a, **k: calls.append(("ab", a)))
    monkeypatch.setattr(S.H, "wait", lambda ms: None)
    monkeypatch.setattr(S.H, "ev", lambda js: (calls.append(("ev", js)), got)[1])
    monkeypatch.setattr(S, "identity", lambda: calls.append(("identity",)))
    monkeypatch.setattr(S, "oembed", lambda v: (calls.append(("oembed", v)), status)[1])
    monkeypatch.setattr(S, "read_flags", lambda: (calls.append(("flags",)), dict(FLAGS_OK))[1])
    return calls


ITEM = {"title": "T", "description": "D"}
PRIVATE_READ = {"title": "T", "description": "D\n", "visibility": "Private",
                "location": f"https://studio.youtube.com/video/vid12345678/edit?c={CHAN}"}


def test_verify_one_reloads_the_page_before_reading_and_passes_on_private_and_refusing_oembed(monkeypatch):
    calls = verify_env(monkeypatch, PRIVATE_READ, 404)
    r = S.verify_one(ITEM, "vid12345678")
    assert r["pass"] is True and r["oembed"] == 404
    kinds = [c[0] for c in calls]
    assert kinds.index("ab") < kinds.index("identity") < kinds.index("ev")
    assert calls[0][1][0] == "open" and f"/video/vid12345678/edit?c={CHAN}" in calls[0][1][1]
    assert ("oembed", "vid12345678") in calls


@pytest.mark.parametrize("got,status", [(PRIVATE_READ, 200), (PRIVATE_READ, None),
                                        (dict(PRIVATE_READ, visibility="Public"), 404),
                                        (dict(PRIVATE_READ, visibility=""), 404), (None, 404),
                                        (dict(PRIVATE_READ, location="https://studio.youtube.com/video/OTHERVIDEO1/edit"), 404)])
def test_verify_one_fails_without_both_proofs(monkeypatch, got, status):
    verify_env(monkeypatch, got, status)
    assert S.verify_one(ITEM, "vid12345678")["pass"] is False


# ---- details: both flags every time ----

def details_env(monkeypatch, text_now):
    seq = []
    monkeypatch.setattr(S.H, "wait", lambda ms: None)

    def ev(js):
        if js == S.H.HEADING:
            return "Details"
        if "title-textarea" in js:
            return text_now["title"]
        if "description-textarea" in js:
            return text_now["description"]
        return None

    monkeypatch.setattr(S.H, "ev", ev)
    monkeypatch.setattr(S.H, "put_text", lambda sel, text, label, root=None: seq.append(("put", label, text)))
    monkeypatch.setattr(S, "click_show_more", lambda: seq.append(("more",)) or True)
    monkeypatch.setattr(S, "radio", lambda n, label: seq.append(("radio", n)))
    return seq


def test_details_sets_both_flags_every_time_even_when_text_is_already_there(monkeypatch):
    seq = details_env(monkeypatch, {"title": "T\n", "description": "D"})
    S.details({"title": "T", "description": "D"})
    assert not [x for x in seq if x[0] == "put"]
    assert [x for x in seq if x[0] == "radio"] == [("radio", KIDS), ("radio", ALTERED)]
    assert seq.index(("more",)) < seq.index(("radio", KIDS))


def test_details_types_what_differs(monkeypatch):
    seq = details_env(monkeypatch, {"title": "other", "description": None})
    S.details({"title": "T", "description": "D"})
    assert [x for x in seq if x[0] == "put"] == [("put", "title", "T"), ("put", "description", "D")]
    assert [x for x in seq if x[0] == "radio"] == [("radio", KIDS), ("radio", ALTERED)]


def test_details_dies_when_not_on_the_details_step(monkeypatch):
    monkeypatch.setattr(S.H, "wait", lambda ms: None)
    monkeypatch.setattr(S.H, "ev", lambda js: "Visibility" if js == S.H.HEADING else None)
    monkeypatch.setattr(S, "click_details_step", lambda: True)
    with pytest.raises(SystemExit) as e:
        S.details({"title": "T", "description": "D"})
    assert e.value.code == 1


# ---- stepper ----

def test_next_step_dies_on_an_unexpected_heading(monkeypatch):
    monkeypatch.setattr(S, "click_next", lambda: True)
    monkeypatch.setattr(S.H, "wait", lambda ms: None)
    monkeypatch.setattr(S.H, "ev", lambda js: "Elements")
    with pytest.raises(SystemExit):
        S.next_step(("Visibility",))
    assert S.next_step(("Elements",)) == "Elements"


def test_to_visibility_walks_forward_and_stops_on_a_strange_step(monkeypatch):
    heads = iter(["Details", "Video elements", "Checks", "Visibility"])
    monkeypatch.setattr(S.H, "ev", lambda js: next(heads))
    monkeypatch.setattr(S, "next_step", lambda want: None)
    S.to_visibility()
    monkeypatch.setattr(S.H, "ev", lambda js: "Surprise")
    with pytest.raises(SystemExit):
        S.to_visibility()


# ---- fixed-purpose click helpers ----

@pytest.mark.parametrize("fn,label", [("click_show_more", "Show more"), ("click_edit_draft", "Edit draft")])
def test_button_helpers_click_exactly_their_one_label(browser, fn, label):
    fb = browser({".click()": True})
    assert getattr(S, fn)() is True
    js = " ".join(fb.calls)
    assert json.dumps(label) in js and js.count(".click()") == 1
    for other in ("Save", "Publish", "Schedule", "Done"):
        assert other not in js


def test_click_helpers_return_false_when_there_is_nothing_to_click(browser):
    browser({".click()": False})
    assert S.click_show_more() is False and S.click_edit_draft() is False


def test_next_and_details_and_close_helpers_have_one_fixed_target(browser):
    fb = browser({".click()": True})
    assert S.click_next() and S.click_details_step() and S.close_saved_dialog()
    assert "#next-button" in fb.calls[0] and "#done-button" not in " ".join(fb.calls)
    assert '"Details"' in fb.calls[1] and '"Close"' in fb.calls[2]


def test_the_module_docstring_documents_every_exit_code():
    for c in ("0 done", "1 abort", "2 usage", "3 the browser stayed", "4 needs you"):
        assert c in S.__doc__


def test_cli_entry_prints_usage_with_exit_2():
    r = subprocess.run([sys.executable, str(RELEASE / "studio.py")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "Usage" in r.stdout



def upload_file_env(monkeypatch, tmp_path, ids=("abcdefghijk",), on_bytes=None):
    f = tmp_path / "episode.mp4"
    f.write_bytes(b"the real episode")
    item = {"id": "daily-x", "file": str(f), "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
    events = []
    monkeypatch.setattr(S.H, "ab", lambda *a, **k: events.append("open"))
    monkeypatch.setattr(S.H, "wait", lambda ms: None)
    monkeypatch.setattr(S, "on_screen", lambda: events.append("screen"))
    monkeypatch.setattr(S, "identity", lambda: events.append("identity"))

    def set_file(sel, path, label):
        events.append(("bytes", path))
        if on_bytes:
            on_bytes(f)

    monkeypatch.setattr(S.H, "set_file", set_file)
    monkeypatch.setattr(S.H, "ev", lambda js: list(ids))
    return item, events


def test_upload_file_claims_the_day_after_the_identity_proof_and_before_the_bytes_go_up(monkeypatch, tmp_path):
    item, events = upload_file_env(monkeypatch, tmp_path)
    assert S.upload_file(item, lambda: events.append("claim")) == "abcdefghijk"
    assert events == ["open", "screen", "identity", "claim", ("bytes", item["file"])]     # set_file gets item["file"]


def test_upload_file_refuses_several_video_ids(monkeypatch, tmp_path):
    item, _ = upload_file_env(monkeypatch, tmp_path, ids=("abcdefghijk", "lmnopqrstuv"))
    with pytest.raises(SystemExit) as e:
        S.upload_file(item, lambda: None)
    assert e.value.code == 1


def test_upload_file_rehashes_after_the_bytes_were_handed_over(monkeypatch, tmp_path):
    item, _ = upload_file_env(monkeypatch, tmp_path, on_bytes=lambda f: f.write_bytes(b"swapped mid-upload"))
    with pytest.raises(SystemExit) as e:
        S.upload_file(item, lambda: None)
    assert e.value.code == 1


# ---------- exact bytes ----------

def test_a_file_swapped_before_the_claim_dies_with_no_receipt_and_no_ledger(tmp_path, monkeypatch, ledger_env):
    w = day_dir(tmp_path)
    item_in(w)
    f = Flow(w, monkeypatch, swap=True)
    assert S.cmd_upload(w) == 1
    assert f.events == ["tab", "upload_file"]
    assert S.receipt(w) == {} and not ledger_env.exists()


# ---------- flags read back in verify ----------

NODE_FLAGS = """
const radios = %s;
const mk = (n, v, inPriv) => ({getAttribute: a => a === 'aria-checked' ? v : null, closest: s => (s === '#privacy-radios' && inPriv) ? {} : null});
global.document = {querySelectorAll: sel => { const m = sel.match(/name="([^"]+)"/); return m ? (radios[m[1]] || []).map(r => mk(m[1], r[0], r[1])) : []; }};
console.log(eval(%s));
"""


def flags_js(radios):
    need_node()
    prog = NODE_FLAGS % (json.dumps(radios), json.dumps(S.READ_FLAGS))
    r = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-300:]
    return json.loads(r.stdout)


def test_read_flags_script_reads_each_named_radio_and_says_unreadable_otherwise():
    assert flags_js({KIDS: [["true", False]], ALTERED: [["true", False]]}) == {"kids": "true", "altered": "true"}
    assert flags_js({KIDS: [["false", False]], ALTERED: [["true", False]]})["kids"] == "false"
    assert flags_js({ALTERED: [["true", False]]})["kids"] == "unreadable"                      # missing
    assert flags_js({KIDS: [["true", False], ["true", False]], ALTERED: [["true", False]]})["kids"] == "unreadable"   # ambiguous
    assert flags_js({KIDS: [["true", True]], ALTERED: [["true", False]]})["kids"] == "unreadable"     # privacy group only


def test_read_flags_script_is_read_only_text():
    assert not re.search(r"click|dispatchEvent|setAttribute|focus|submit|\.value =|\.checked =", S.READ_FLAGS)


def test_read_flags_returns_what_it_reads(browser):
    browser({"VIDEO_HAS_ALTERED": {"kids": "true", "altered": "true"}})
    assert S.read_flags() == {"kids": "true", "altered": "true"}


def test_read_flags_expands_show_more_once_when_unreadable_then_rereads(monkeypatch):
    answers = iter([{"kids": "unreadable", "altered": "unreadable"}, {"kids": "true", "altered": "true"}])
    clicks = []
    monkeypatch.setattr(S.H, "ev", lambda js: next(answers))
    monkeypatch.setattr(S.H, "wait", lambda ms: None)
    monkeypatch.setattr(S, "click_page_show_more", lambda: clicks.append(1) or True)
    assert S.read_flags() == {"kids": "true", "altered": "true"}
    assert clicks == [1]


def test_read_flags_that_stay_unreadable_are_unreadable_never_true(monkeypatch):
    monkeypatch.setattr(S.H, "ev", lambda js: {"kids": "unreadable", "altered": "true"})
    monkeypatch.setattr(S.H, "wait", lambda ms: None)
    monkeypatch.setattr(S, "click_page_show_more", lambda: False)
    assert S.read_flags() == {"kids": "unreadable", "altered": "true"}
    for junk in (None, "x", [], {"kids": "TRUE", "altered": 1}, {"kids": "true"}):
        monkeypatch.setattr(S.H, "ev", lambda js, j=junk: j)
        r = S.read_flags()
        assert "true" not in r.values() or r == {"kids": "true", "altered": "unreadable"}, junk


def test_verify_one_puts_the_flags_into_the_judgement(monkeypatch):
    verify_env(monkeypatch, PRIVATE_READ, 404)
    monkeypatch.setattr(S, "read_flags", lambda: {"kids": "true", "altered": "unreadable"})
    r = S.verify_one(ITEM, "vid12345678")
    assert r["pass"] is False and r["checks"]["altered content is yes"] is False


def test_click_page_show_more_is_document_scoped_and_one_label(browser):
    fb = browser({".click()": True})
    assert S.click_page_show_more() is True
    js = " ".join(fb.calls)
    assert json.dumps("Show more") in js and js.count(".click()") == 1 and "ytcp-uploads-dialog" not in js


# ---------- the per-channel uploads ledger ----------

def ledger_rows(path):
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_ledger_records_the_claim_and_the_verified_upload(tmp_path, monkeypatch, ledger_env):
    w = day_dir(tmp_path)
    item = item_in(w)
    Flow(w, monkeypatch)
    assert S.cmd_upload(w) == 0
    rows = ledger_rows(ledger_env)
    assert [r["event"] for r in rows] == ["upload_started", "private_verified"]
    assert all(r["day"] == DAY and r["subject_sha256"] == item["sha256"] for r in rows)
    assert "video_id" not in rows[0] and rows[1]["video_id"] == "vid12345678"


def test_a_deleted_receipt_cannot_buy_a_second_upload(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w)
    Flow(w, monkeypatch)
    assert S.cmd_upload(w) == 0
    (w / "upload.json").unlink()
    no_browser(monkeypatch)
    with pytest.raises(SystemExit) as e:
        S.cmd_upload(w)
    assert e.value.code == 4


def test_a_second_work_dir_for_the_same_day_needs_you(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    w1, w2 = day_dir(a), day_dir(b)
    item_in(w1)
    Flow(w1, monkeypatch)
    assert S.cmd_upload(w1) == 0
    item_in(w2)
    no_browser(monkeypatch)
    with pytest.raises(SystemExit) as e:
        S.cmd_upload(w2)
    assert e.value.code == 4


def test_a_claim_with_a_lost_receipt_still_blocks(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w)
    Flow(w, monkeypatch, verify_pass=False)
    S.cmd_upload(w)
    (w / "upload.json").unlink()
    no_browser(monkeypatch)
    with pytest.raises(SystemExit) as e:
        S.cmd_upload(w)
    assert e.value.code == 4


def test_ledger_entries_for_other_days_do_not_block(tmp_path, monkeypatch, ledger_env):
    ledger_env.write_text(json.dumps({"event": "private_verified", "day": "2026-10-15", "subject_sha256": "a" * 64}) + "\n")
    w = day_dir(tmp_path)
    item_in(w)
    Flow(w, monkeypatch)
    assert S.cmd_upload(w) == 0


def test_a_corrupt_ledger_needs_you(tmp_path, monkeypatch, ledger_env):
    for junk in ("{not json\n", "[1]\n", '"x"\n'):
        ledger_env.write_text(junk)
        w = day_dir(tmp_path)
        item_in(w)
        no_browser(monkeypatch)
        assert S.main(["x", "upload", str(w)]) == 4


def test_the_claim_itself_rechecks_the_ledger_under_its_lock(tmp_path, ledger_env):
    S.ledger_claim(DAY, "a" * 64, "daily-x")
    with pytest.raises(SystemExit) as e:
        S.ledger_claim(DAY, "b" * 64, "daily-x")        # a racing second process loses at the claim, not only the pre-check
    assert e.value.code == 4
    S.ledger_claim("2026-10-17", "b" * 64, "daily-y")
    assert len(ledger_rows(ledger_env)) == 2


def test_ledger_appends_are_fsynced_file_then_directory(tmp_path, monkeypatch, ledger_env):
    synced = []
    real = os.fsync
    monkeypatch.setattr(S.os, "fsync", lambda fd: (synced.append(os.fstat(fd).st_mode), real(fd))[1])
    S.ledger_claim(DAY, "a" * 64, "daily-x")
    assert len(synced) >= 2


def test_the_ledger_lives_in_the_state_folder_outside_every_work_dir():
    p = S.ledger_path()
    assert p == paths.state_root() / "uploads" / "ledger.jsonl"
    assert not S.DAY_RE.fullmatch(p.parent.name) and p.parent.parent == paths.state_root()


# ---------- minors ----------

def test_a_draft_that_is_gone_after_a_crash_needs_you(monkeypatch):
    monkeypatch.setattr(S.H, "ev", lambda js: None)
    monkeypatch.setattr(S.H, "ab", lambda *a, **k: None)
    monkeypatch.setattr(S.H, "wait", lambda ms: None)
    monkeypatch.setattr(S, "on_screen", lambda: None)
    monkeypatch.setattr(S, "identity", lambda: None)
    monkeypatch.setattr(S, "click_edit_draft", lambda: False)
    with pytest.raises(SystemExit) as e:
        S.resume("vid12345678")
    assert e.value.code == 4


def test_resume_dies_when_the_wizard_that_opened_is_not_this_videos(monkeypatch):
    answers = iter([None, ["lmnopqrstuv"]])
    monkeypatch.setattr(S.H, "ev", lambda js: next(answers, None))
    monkeypatch.setattr(S.H, "ab", lambda *a, **k: None)
    monkeypatch.setattr(S.H, "wait", lambda ms: None)
    monkeypatch.setattr(S, "on_screen", lambda: None)
    monkeypatch.setattr(S, "identity", lambda: None)
    monkeypatch.setattr(S, "click_edit_draft", lambda: True)
    with pytest.raises(SystemExit) as e:
        S.resume("vid12345678")
    assert e.value.code == 1


@pytest.mark.parametrize("vid", ["short", "has space!!", "../../etc/pa", "x" * 12, 12345678901, ""])
def test_a_bad_video_id_in_the_receipt_needs_you(tmp_path, monkeypatch, vid):
    w = day_dir(tmp_path)
    item = item_in(w)
    S.save(w, day=DAY, subject_sha256=item["sha256"], video_id=vid, stage="saved")
    no_browser(monkeypatch)
    with pytest.raises(SystemExit) as e:
        S.cmd_upload(w)
    assert e.value.code == 4


@pytest.mark.parametrize("stage", ["uploaded", "details"])
def test_verify_cannot_pass_below_saved(tmp_path, monkeypatch, stage):
    w = day_dir(tmp_path)
    item = item_in(w)
    S.save(w, day=DAY, subject_sha256=item["sha256"], video_id="vid12345678", stage=stage)
    no_browser(monkeypatch)
    with pytest.raises(SystemExit) as e:
        S.cmd_verify(w)
    assert e.value.code == 1


def test_the_uploaded_stage_is_on_disk_before_the_details_step(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w)
    f = Flow(w, monkeypatch)
    assert S.cmd_upload(w) == 0
    assert f.stage_at["details"]["stage"] == "uploaded" and f.stage_at["details"]["video_id"] == f.vid


def test_oembed_asks_about_exactly_the_verified_id(monkeypatch):
    seen = []

    class R:
        status = 200

    monkeypatch.setattr(S.urllib.request, "urlopen", lambda url, timeout=0: (seen.append(url), R())[1])
    assert S.oembed("vid12345678") == 200
    assert seen[0].endswith("watch?v=vid12345678") and seen[0].startswith("https://www.youtube.com/oembed")


def test_verify_one_asks_oembed_about_the_id_it_read(monkeypatch):
    calls = verify_env(monkeypatch, PRIVATE_READ, 404)
    S.verify_one(ITEM, "vid12345678")
    assert ("oembed", "vid12345678") in calls


def test_cmd_verify_refuses_a_receipt_for_another_day(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item = item_in(w)
    S.save(w, day="2026-10-15", subject_sha256=item["sha256"], video_id="vid12345678", stage="saved")
    no_browser(monkeypatch)
    with pytest.raises(SystemExit) as e:
        S.cmd_verify(w)
    assert e.value.code == 4


def test_cmd_verify_holds_the_work_lock(tmp_path, monkeypatch, capsys):
    w = day_dir(tmp_path)
    item = item_in(w)
    S.save(w, day=DAY, subject_sha256=item["sha256"], video_id="vid12345678", stage="saved")
    no_browser(monkeypatch)
    with S.work_lock(w):
        assert S.main(["x", "verify", str(w)]) == 1
    assert "upload_locked" in capsys.readouterr().out
    seen = []
    Flow(w, monkeypatch)

    def locked_run(fn, *a):
        with pytest.raises(S.ReceiptError):
            with S.work_lock(w):
                pass
        seen.append(1)
        fn(*a)
        return 0

    monkeypatch.setattr(S, "with_browser", locked_run)
    assert S.cmd_verify(w) == 0 and seen == [1]


def test_day_of_needs_a_real_dir_named_for_a_day(tmp_path):
    assert S.day_of(day_dir(tmp_path)) == DAY
    for bad in (tmp_path, tmp_path / "2026-10-17", tmp_path / "2026-1-17"):
        (tmp_path / "2026-1-17").mkdir(exist_ok=True)
        with pytest.raises(SystemExit) as e:
            S.day_of(bad)
        assert e.value.code == 1


def test_a_corrupt_receipt_through_main_is_exit_4(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w)
    (w / "upload.json").write_text("not json")
    no_browser(monkeypatch)
    assert S.main(["x", "upload", str(w)]) == 4
    assert S.main(["x", "verify", str(w)]) == 4


# ---------- every exit 4 ends with one machine-readable NEEDS_YOU <code> line ----------

def _last_line(capsys):
    return capsys.readouterr().out.rstrip("\n").splitlines()[-1]


def test_needs_you_default_code_and_final_line(tmp_path, monkeypatch, capsys):
    def stop(w, *vis):
        S.needs_you("x", "y")
    monkeypatch.setattr(S, "cmd_upload", stop)
    assert S.main(["x", "upload", str(tmp_path)]) == 4
    assert _last_line(capsys) == "NEEDS_YOU needs_you"


def test_the_code_of_the_stop_is_the_one_printed_not_a_stale_one(tmp_path, monkeypatch, capsys):
    calls = iter(["signed_out", None])

    def stop(w, *vis):
        code = next(calls)
        if code:
            S.needs_you("x", "y", code)
        S.needs_you("x", "y")
    monkeypatch.setattr(S, "cmd_upload", stop)
    S.main(["x", "upload", str(tmp_path)])
    assert _last_line(capsys) == "NEEDS_YOU signed_out"
    S.main(["x", "upload", str(tmp_path)])
    assert _last_line(capsys) == "NEEDS_YOU needs_you"


def test_the_line_is_last_even_when_the_release_prints_after_the_stop(tmp_path, monkeypatch, capsys):
    def stop(w, *vis):
        return S.with_browser(lambda: S.needs_you("identity", "no", "wrong_channel"))
    monkeypatch.setattr(S, "attach", lambda: setattr(S, "_LEASED", True))
    monkeypatch.setattr(S, "release", lambda: print("released the browser"))
    monkeypatch.setattr(S, "cmd_upload", stop)
    assert S.main(["x", "upload", str(tmp_path)]) == 4
    out = capsys.readouterr().out.splitlines()
    assert "released the browser" in out and out[-1] == "NEEDS_YOU wrong_channel"


def test_no_needs_you_line_for_other_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(S, "cmd_upload", lambda w, *vis: 0)
    assert S.main(["x", "upload", str(tmp_path)]) == 0
    monkeypatch.setattr(S, "cmd_upload", lambda w, *vis: (_ for _ in ()).throw(RuntimeError("x")))
    assert S.main(["x", "upload", str(tmp_path)]) == 1
    assert "NEEDS_YOU" not in capsys.readouterr().out


def _code_of(tmp_path, monkeypatch, capsys, arrange):
    w = day_dir(tmp_path)
    item = item_in(w)
    arrange(w, item)
    no_browser(monkeypatch)
    capsys.readouterr()
    assert S.main(["x", "upload", str(w)]) == 4
    return _last_line(capsys)


def test_receipt_codes(tmp_path, monkeypatch, capsys):
    def mk(name, fn):
        d = tmp_path / name
        d.mkdir()
        return _code_of(d, monkeypatch, capsys, fn)
    assert mk("a", lambda w, i: (w / "upload.json").write_text("{not json")) == "NEEDS_YOU receipt_corrupt"
    assert mk("b", lambda w, i: S.save(w, day="2026-10-15", subject_sha256=i["sha256"], video_id="vid12345678",
                                       stage="uploaded")) == "NEEDS_YOU receipt_other_day"
    assert mk("c", lambda w, i: S.save(w, day=DAY, subject_sha256="f" * 64, video_id="vid12345678",
                                       stage="uploaded")) == "NEEDS_YOU receipt_other_render"
    assert mk("d", lambda w, i: S.save(w, day=DAY, subject_sha256=i["sha256"],
                                       stage="upload_started")) == "NEEDS_YOU upload_started_no_video"
    assert mk("e", lambda w, i: S.save(w, day=DAY, subject_sha256=i["sha256"], video_id="short",
                                       stage="saved")) == "NEEDS_YOU bad_video_id"


def test_ledger_codes(tmp_path, monkeypatch, capsys, ledger_env):
    ledger_env.write_text("{not json\n")
    assert _code_of(tmp_path, monkeypatch, capsys, lambda w, i: None) == "NEEDS_YOU unreadable_ledger"
    ledger_env.write_text(json.dumps({"event": "private_verified", "day": DAY, "subject_sha256": "a" * 64}) + "\n")
    d = tmp_path / "two"
    d.mkdir()
    assert _code_of(d, monkeypatch, capsys, lambda w, i: None) == "NEEDS_YOU ledger_has_day"


@pytest.mark.parametrize("seen,code", [(None, "wrong_channel"), ("UCother", "wrong_channel")])
def test_identity_code(browser, capsys, seen, code):
    browser({"CHANNEL_ID": seen})
    with pytest.raises(SystemExit):
        S.identity()
    assert S._NEEDS_CODE[0] == code


def test_draft_gone_code(monkeypatch):
    monkeypatch.setattr(S.H, "ev", lambda js: None)
    monkeypatch.setattr(S.H, "ab", lambda *a, **k: None)
    monkeypatch.setattr(S.H, "wait", lambda ms: None)
    monkeypatch.setattr(S, "on_screen", lambda: None)
    monkeypatch.setattr(S, "identity", lambda: None)
    monkeypatch.setattr(S, "click_edit_draft", lambda: False)
    with pytest.raises(SystemExit):
        S.resume("vid12345678")
    assert S._NEEDS_CODE[0] == "draft_gone"


def test_identity_changed_receipt_error_is_a_code(tmp_path, monkeypatch, capsys):
    def ident(w, *vis):
        raise S.ReceiptError("receipt_identity_changed")
    monkeypatch.setattr(S, "cmd_upload", ident)
    assert S.main(["x", "upload", str(tmp_path)]) == 4
    assert _last_line(capsys) == "NEEDS_YOU receipt_identity_changed"


def test_every_needs_you_call_site_passes_a_known_code():
    src = Path(S.__file__).read_text()
    import ast as _ast
    codes = set()
    for n in _ast.walk(_ast.parse(src)):
        if isinstance(n, _ast.Call) and getattr(n.func, "id", "") == "needs_you" and len(n.args) >= 3 and isinstance(n.args[2], _ast.Constant):
            codes.add(n.args[2].value)
    assert codes <= set(S.NEEDS_YOU_CODES)
    assert {"signed_out", "wrong_channel", "draft_gone", "ledger_has_day", "unreadable_ledger"} <= codes


# ---------- the browser is switched to the upload channel and back to the home channel (channel.home_id) ----------



@pytest.fixture(autouse=True)
def switch_state():
    S._SWITCHED_IN = False
    S.H.EXTRA = []
    yield
    S._SWITCHED_IN = False
    S.H.EXTRA = []


def test_the_two_studio_channel_homes_come_from_settings():
    assert (S.CHAN, S.HOME_CHAN) == (CHAN, HOME_CHAN)
    assert (S.DAILY_HOME_URL, S.HOME_URL) == (DAILY_HOME, HOME_HOME)


class Held:
    """The flow with the page steps faked and switch_back scripted; events show the order."""

    def __init__(self, work, monkeypatch, back=True, verify_pass=True, tab_stop=None, navigated=True):
        self.work, self.events, self.back, self.tab_stop, self.navigated = work, [], back, tab_stop, navigated
        m = monkeypatch
        m.setattr(S, "with_browser", lambda fn, *a: Flow.with_browser(fn, *a))
        m.setattr(S, "studio_tab", self.tab)
        m.setattr(S, "upload_file", lambda item, before_file=None: (before_file(), "vid12345678")[1])
        m.setattr(S, "details", lambda item: None)
        m.setattr(S, "wait_uploaded", lambda: None)
        m.setattr(S, "to_visibility", lambda: None)
        m.setattr(S, "set_visibility", lambda n: None)
        m.setattr(S, "commit_visibility", lambda want, on_attempt=None: (on_attempt and on_attempt(), "Video saved")[1])
        m.setattr(S, "close_saved_dialog", lambda: None)
        m.setattr(S, "verify_one", lambda item, vid, vis: {"pass": verify_pass, "checks": {"x": verify_pass},
                                                           "oembed": 401, "visibility": "Private", "at": "t"})
        m.setattr(S, "switch_back", self.switch_back)
        S.H.VIDEO_ID = None

    def tab(self, rec=None):
        self.events.append("tab")
        S._SWITCHED_IN = self.navigated
        if self.tab_stop:
            self.tab_stop()

    def switch_back(self):
        self.events.append("switch_back")
        S._SWITCHED_IN = False
        if isinstance(self.back, Exception):
            raise self.back
        return self.back


def test_switch_back_runs_after_the_verified_upload_and_the_receipt_carries_it(tmp_path, monkeypatch, capsys):
    w = day_dir(tmp_path)
    item_in(w)
    h = Held(w, monkeypatch, back=True)
    assert S.cmd_upload(w) == 0
    assert h.events == ["tab", "switch_back"]
    r = S.receipt(w)
    assert r["stage"] == "private_verified" and r["switched_back"] is True
    assert "SWITCHED_BACK true" in capsys.readouterr().out.splitlines()


def test_a_failed_switch_back_is_recorded_and_never_changes_a_verified_upload(tmp_path, monkeypatch, capsys):
    w = day_dir(tmp_path)
    item_in(w)
    Held(w, monkeypatch, back=False)
    assert S.cmd_upload(w) == 0
    assert S.receipt(w)["switched_back"] is False and S.receipt(w)["stage"] == "private_verified"
    assert "SWITCHED_BACK false" in capsys.readouterr().out.splitlines()


def test_a_switch_back_that_raises_is_recorded_false_and_changes_nothing(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w)
    Held(w, monkeypatch, back=RuntimeError("boom"))
    assert S.cmd_upload(w) == 0
    assert S.receipt(w)["switched_back"] is False


def test_switch_back_runs_after_a_verify_failure_and_keeps_the_exit_code(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w)
    h = Held(w, monkeypatch, verify_pass=False)
    assert S.cmd_upload(w) == 1
    assert h.events == ["tab", "switch_back"]
    assert S.receipt(w)["switched_back"] is True


def test_switch_back_runs_after_a_needs_you_stop_and_a_failed_one_keeps_exit_4(tmp_path, monkeypatch, capsys):
    w = day_dir(tmp_path)
    item_in(w)
    h = Held(w, monkeypatch, back=False, tab_stop=lambda: S.needs_you("identity", "no", "wrong_channel"))
    assert S.cmd_upload(w) == 4
    assert h.events == ["tab", "switch_back"]
    assert "SWITCHED_BACK false" in capsys.readouterr().out
    assert S.receipt(w) == {}      # no upload began: the note must not create a receipt the next run would refuse


def test_the_note_is_printed_before_the_final_needs_you_line(tmp_path, monkeypatch, capsys):
    w = day_dir(tmp_path)
    item_in(w)
    Held(w, monkeypatch, back=False, tab_stop=lambda: S.needs_you("identity", "no", "wrong_channel"))
    assert S.main(["x", "upload", str(w)]) == 4
    out = capsys.readouterr().out.splitlines()
    assert "SWITCHED_BACK false" in out and out[-1] == "NEEDS_YOU wrong_channel"


def test_no_switch_back_when_the_browser_was_never_navigated(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w)
    h = Held(w, monkeypatch, navigated=False, tab_stop=lambda: S.H.die("x", "y"))
    assert S.cmd_upload(w) == 1
    assert h.events == ["tab"]


def test_keep_on_abort_does_not_navigate_away_from_the_dialog(tmp_path, monkeypatch):
    monkeypatch.setenv("ORION_CLOCK_KEEP_ON_ABORT", "1")
    w = day_dir(tmp_path)
    item_in(w)
    h = Held(w, monkeypatch, verify_pass=False)
    assert S.cmd_upload(w) == 1
    assert h.events == ["tab"]


def test_verify_and_preflight_switch_back_too(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w)
    S.save(w, day=DAY, subject_sha256=hashlib.sha256(b"not really a video").hexdigest(), video_id="vid12345678",
           stage="saved")
    h = Held(w, monkeypatch)
    assert S.cmd_verify(w) == 0
    assert h.events == ["tab", "switch_back"] and S.receipt(w)["switched_back"] is True
    h.events.clear()
    monkeypatch.setattr(S, "on_screen", lambda: None)
    assert S._dispatch("preflight", []) == 0
    assert h.events == ["tab", "switch_back"]


def test_no_channel_is_written_into_the_driver():
    """Every channel the driver navigates to is built from settings (STUDIO_CHANNEL plus channel.id or
    channel.home_id); no channel id is a literal in the code."""
    src = (RELEASE / "studio.py").read_text() + (RELEASE / "pinned.py").read_text()
    assert re.findall(r"UC[A-Za-z0-9_-]{22}", src) == []
    assert re.findall(r"https://studio\.youtube\.com/channel/[A-Za-z0-9_-]+", src) == []
    assert src.count('STUDIO_CHANNEL + ') == 2   # the two homes: the upload channel and the home channel


# ---------- close the tab this run opened, after the switch back ----------

@pytest.fixture(autouse=True)
def opened_state():
    S._OPENED_TAB, S._MARK = False, None
    yield
    S._OPENED_TAB, S._MARK = False, None


class HeldClose(Held):
    def __init__(self, *a, close=True, opened=True, **k):
        super().__init__(*a, **k)
        self.close, self.opened = close, opened
        S._OPENED_TAB = False
        monkeypatch = a[1]
        monkeypatch.setattr(S, "close_my_tab", self.close_my_tab)

    def tab(self, rec=None):
        S._OPENED_TAB = self.opened
        super().tab(rec)

    def close_my_tab(self):
        self.events.append("close")
        if isinstance(self.close, Exception):
            raise self.close
        return self.close


def test_the_tab_is_closed_after_the_switch_back_on_success(tmp_path, monkeypatch, capsys):
    w = day_dir(tmp_path)
    item_in(w)
    h = HeldClose(w, monkeypatch)
    assert S.cmd_upload(w) == 0
    assert h.events == ["tab", "switch_back", "close"]
    assert "CLOSED_TAB true" in capsys.readouterr().out.splitlines()
    assert S._OPENED_TAB is False


def test_the_tab_is_closed_after_a_verify_failure_and_after_needs_you(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w)
    h = HeldClose(w, monkeypatch, verify_pass=False)
    assert S.cmd_upload(w) == 1 and h.events == ["tab", "switch_back", "close"]
    w2 = day_dir(tmp_path, "2026-10-17")
    item_in(w2)
    h2 = HeldClose(w2, monkeypatch, tab_stop=lambda: S.needs_you("identity", "no", "wrong_channel"))
    assert S.cmd_upload(w2) == 4 and h2.events == ["tab", "switch_back", "close"]


def test_a_failed_or_raising_close_is_recorded_and_changes_no_exit_code(tmp_path, monkeypatch, capsys):
    for how in (False, RuntimeError("boom")):
        w = day_dir(tmp_path, f"2026-10-1{1 if how is False else 2}")
        item_in(w)
        HeldClose(w, monkeypatch, close=how)
        assert S.cmd_upload(w) == 0
        assert "CLOSED_TAB false" in capsys.readouterr().out.splitlines()


def test_no_close_when_we_never_opened_a_tab(tmp_path, monkeypatch, capsys):
    w = day_dir(tmp_path)
    item_in(w)
    h = HeldClose(w, monkeypatch, opened=False)
    assert S.cmd_upload(w) == 0
    assert h.events == ["tab", "switch_back"] and "CLOSED_TAB" not in capsys.readouterr().out


def test_a_signed_out_stop_still_tries_to_close_the_tab_it_opened(tmp_path, monkeypatch, capsys):
    w = day_dir(tmp_path)
    item_in(w)
    h = HeldClose(w, monkeypatch, navigated=False, close=False,
                  tab_stop=lambda: S.needs_you("sign-in", "no", "signed_out"))
    assert S.cmd_upload(w) == 4
    assert h.events == ["tab", "close"] and "CLOSED_TAB false" in capsys.readouterr().out


def test_keep_on_abort_keeps_the_tab(tmp_path, monkeypatch):
    monkeypatch.setenv("ORION_CLOCK_KEEP_ON_ABORT", "1")
    w = day_dir(tmp_path)
    item_in(w)
    h = HeldClose(w, monkeypatch, verify_pass=False)
    assert S.cmd_upload(w) == 1 and h.events == ["tab"]


# ---------- our tab is identified at creation and is the only tab ever acted on ----------

import base64  # noqa: E402
import signal  # noqa: E402

CHOOSER = "https://www.youtube.com/channel_switcher"
OWN_TAB = f"https://studio.youtube.com/channel/{HOME_CHAN}/videos"


class Chrome:
    """A fake browser behind H._raw: a tab list with window names, `tab new/close`, and JSON batches whose steps
    select a tab, run a guard, navigate or read/set the mark. `redirect` maps a requested URL to where it lands."""

    def __init__(self, monkeypatch, tabs=(), redirect=None, new_tabs_per_new=1, on_new=None, on_batch=None, shot="ok"):
        self.tabs = [{"url": u, "name": "", "ses": ""} for u in tabs]
        self.active, self.redirect, self.nav, self.closed = 0, dict(redirect or {}), [], []
        self.new_per, self.on_new, self.on_batch = new_tabs_per_new, on_new, on_batch
        self.new_urls, self.acted = [], []       # acted: (tab url, step) for every step that is not a plain read
        # shot: "ok" writes the file, "fail" fails the step, "nofile" succeeds and writes nothing
        self.shot, self.shots = shot, []         # shots: (tab url, tab mark, path) for every screenshot step
        self.ch_reads = []        # answers for the next channel read-backs (then the URL decides), e.g. [None]
        self.batches = []
        monkeypatch.setattr(S.H, "_raw", self.raw)
        monkeypatch.setattr(S.H, "wait", lambda ms: None)
        monkeypatch.setattr(S.H, "url", lambda: self.tabs[self.active]["url"])

    def landing(self, url):
        return self.redirect.get(url, url)

    def raw(self, *a, stdin=None, t=90):
        if a[:2] == ("tab", "list"):
            return "\n".join(f"{ARROW + ' ' if i == self.active else '  '}[{i}] Title {x['url']}" for i, x in enumerate(self.tabs))
        if a[:2] == ("tab", "new"):
            for _ in range(self.new_per):
                self.tabs.append({"url": self.landing(a[2]), "name": "", "ses": ""})
            self.active = len(self.tabs) - 1
            self.new_urls.append(a[2])
            if self.on_new:
                self.on_new(self)
                self.active = max(i for i, x in enumerate(self.tabs) if "about:blank" in x["url"])
            return ""
        if a[:2] == ("tab", "close"):
            i = int(a[2])
            self.closed.append(self.tabs[i]["url"])
            del self.tabs[i]
            self.active = min(self.active, len(self.tabs) - 1)
            return ""
        if a and a[0] == "batch":
            return json.dumps(self.batch(json.loads(stdin)))
        return ""

    def batch(self, steps):
        if self.on_batch:
            hook, self.on_batch = self.on_batch, None
            hook(self)
        self.batches.append(steps)
        out = []
        for st in steps:
            r = self.step(st)
            out.append(r)
            if not r["success"]:
                break
        return out

    def step(self, st):
        t = self.tabs[self.active] if self.tabs else None
        if st[0] == "tab" and st[1] == "close":
            self.acted.append((t["url"], "close"))
            self.closed.append(t["url"])
            del self.tabs[self.active]
            self.active = max(0, min(self.active, len(self.tabs) - 1))
            return {"success": True}
        if st[0] == "tab":
            self.active = int(st[1])
            return {"success": True}
        if st[0] == "open":
            self.acted.append((t["url"], "open"))
            t["url"] = self.landing(st[1])
            self.nav.append((self.active, st[1]))
            return {"success": True}
        if st[0] == "wait":
            return {"success": True}
        if st[0] == "screenshot":
            self.shots.append((t["url"], t["name"] or t["ses"], st[1]))
            if self.shot == "fail":
                return {"success": False, "error": "screenshot failed"}
            if self.shot == "ok":
                Path(st[1]).write_bytes(b"\x89PNG not really")
            return {"success": True, "result": {"path": st[1]}}
        js = base64.b64decode(st[2]).decode() if st[1] == "-b" else st[1]
        if "GUARD" in js and "location.href.includes" in js:
            tok = json.loads(re.search(r"includes\((\"[^\"]*\")\)", js).group(1))
            if tok not in t["url"]:
                return {"success": False, "error": "GUARD: not the new tab"}
            return {"success": True, "result": "guard ok"}
        if "GUARD" in js:
            m = re.search(r"!== (\"[^\"]*\")", js)
            if not m or json.loads(m.group(1)) not in (t["name"], t["ses"]):
                return {"success": False, "error": "GUARD: not my tab"}
            return {"success": True, "result": "guard ok"}
        if "window.name = " in js:
            mark = json.loads(re.search(r"window\.name = (\"[^\"]*\")", js).group(1))
            t["name"] = t["ses"] = mark
            if "CHANNEL_ID" in js:
                m = re.match(r"https://studio\.youtube\.com/channel/([A-Za-z0-9_-]+)", t["url"])
                ch = self.ch_reads.pop(0) if self.ch_reads else m.group(1) if m else None
                return {"success": True, "result": json.dumps({"name": t["name"], "ch": ch, "href": t["url"]})}
            self.acted.append((t["url"], "mark"))
            return {"success": True, "result": t["name"]}
        return {"success": True, "result": t["name"] or t["ses"] or ""}


def _studio_tab_in(monkeypatch, chrome, channel=CHAN):
    monkeypatch.setattr(S.H, "ev", lambda js: channel)
    S.studio_tab()


def test_the_new_tab_is_recorded_and_marked_at_creation(monkeypatch):
    c = Chrome(monkeypatch, tabs=[OWN_TAB])
    _studio_tab_in(monkeypatch, c)
    assert S._OPENED_TAB is True and S._SWITCHED_IN is True and S._MARK
    assert [x["url"] for x in c.tabs] == [OWN_TAB, DAILY_HOME]
    assert c.tabs[1]["name"] == S._MARK and c.tabs[0]["name"] == ""


def test_your_own_home_channel_tab_is_never_navigated_or_closed(monkeypatch, capsys):
    """Our tab ends on a chooser; your own tab sits on the home channel."""
    c = Chrome(monkeypatch, tabs=[OWN_TAB], redirect={DAILY_HOME: CHOOSER})
    S._NEEDS_CODE[0] = "needs_you"
    with pytest.raises(SystemExit) as e:
        _studio_tab_in(monkeypatch, c)
    assert e.value.code == 4 and S._NEEDS_CODE[0] == "wrong_channel"
    S.switch_back_after(None, False)
    out = capsys.readouterr().out.splitlines()
    assert "SWITCHED_BACK true" in out and "CLOSED_TAB true" in out
    assert [n for n in c.nav if n[0] == 0] == []                 # nothing ever navigated your tab
    assert [x["url"] for x in c.tabs] == [OWN_TAB] and c.closed == [HOME_HOME]


def test_a_redirect_on_switch_in_is_exit_4_and_still_switches_back_and_closes(tmp_path, monkeypatch, capsys):
    w = day_dir(tmp_path)
    item_in(w)
    c = Chrome(monkeypatch, tabs=[OWN_TAB], redirect={DAILY_HOME: CHOOSER})
    monkeypatch.setattr(S.H, "ev", lambda js: CHAN)
    monkeypatch.setattr(S, "with_browser", lambda fn, *a: Flow.with_browser(fn, *a))
    assert S.cmd_upload(w) == 4
    out = capsys.readouterr().out.splitlines()
    assert "SWITCHED_BACK true" in out and "CLOSED_TAB true" in out
    assert [x["url"] for x in c.tabs] == [OWN_TAB]


def test_switch_back_that_lands_on_a_chooser_is_false_but_our_tab_is_still_closed_and_yours_is_not(monkeypatch, capsys):
    c = Chrome(monkeypatch, tabs=[OWN_TAB], redirect={HOME_HOME: CHOOSER})
    _studio_tab_in(monkeypatch, c)
    S.switch_back_after(None, False)
    out = capsys.readouterr().out.splitlines()
    assert "SWITCHED_BACK false" in out and "CLOSED_TAB true" in out
    assert [n for n in c.nav if n[0] == 0] == [] and [x["url"] for x in c.tabs] == [OWN_TAB]


def test_other_home_channel_tabs_do_not_stop_our_switch_back(monkeypatch):
    c = Chrome(monkeypatch, tabs=[OWN_TAB, OWN_TAB + "?x=1"])
    _studio_tab_in(monkeypatch, c)
    assert S.switch_back() is True


def test_if_our_tab_cannot_be_identified_nothing_is_navigated_or_closed(monkeypatch, capsys):
    c = Chrome(monkeypatch, tabs=[OWN_TAB], new_tabs_per_new=2)       # two tabs carry the token: which is ours?
    monkeypatch.setattr(S.H, "ev", lambda js: CHAN)
    with pytest.raises(SystemExit) as e:
        S.studio_tab()
    assert e.value.code == 1 and S._MARK is None and S.H.MARK is None
    S.switch_back_after(None, False)
    out = capsys.readouterr().out.splitlines()
    assert "CLOSED_TAB false" in out and "SWITCHED_BACK true" not in out
    assert c.nav == [] and c.closed == []


def test_a_lost_mark_means_no_close_and_never_a_url_match(monkeypatch, capsys):
    c = Chrome(monkeypatch, tabs=[OWN_TAB])
    _studio_tab_in(monkeypatch, c)
    for t in c.tabs:
        t["name"] = t["ses"] = ""
    c.nav.clear()
    S.switch_back_after(None, False)
    out = capsys.readouterr().out.splitlines()
    assert "SWITCHED_BACK false" in out and "CLOSED_TAB false" in out
    assert c.nav == [] and c.closed == []


def test_two_tabs_carrying_the_mark_are_not_certain(monkeypatch):
    c = Chrome(monkeypatch, tabs=[OWN_TAB])
    _studio_tab_in(monkeypatch, c)
    c.tabs[0]["name"] = S._MARK
    assert S.switch_back() is False and S.close_my_tab() is False and c.closed == []


def test_a_signed_out_stop_closes_our_tab_but_is_not_a_switch(monkeypatch, capsys):
    c = Chrome(monkeypatch, tabs=[OWN_TAB], redirect={DAILY_HOME: "https://accounts.google.com/v3/signin"})
    S._NEEDS_CODE[0] = "needs_you"
    with pytest.raises(SystemExit):
        _studio_tab_in(monkeypatch, c)
    assert S._NEEDS_CODE[0] == "signed_out" and S._SWITCHED_IN is False
    S.switch_back_after(None, False)
    out = capsys.readouterr().out.splitlines()
    assert "SWITCHED_BACK true" not in out and "SWITCHED_BACK false" not in out and "CLOSED_TAB true" in out
    assert [x["url"] for x in c.tabs] == [OWN_TAB]


def test_close_failure_from_the_driver_is_false_not_raised(monkeypatch):
    c = Chrome(monkeypatch, tabs=[OWN_TAB])
    _studio_tab_in(monkeypatch, c)
    real = c.raw

    def raw(*a, **k):
        if a[:1] == ("batch",) and any(st[:2] == ["tab", "close"] for st in json.loads(k["stdin"])):
            raise OSError("x")
        return real(*a, **k)
    monkeypatch.setattr(S.H, "_raw", raw)
    assert S.close_my_tab() is False


def test_sigterm_during_the_switch_back_still_closes_the_tab_then_propagates(monkeypatch, capsys):
    S._SWITCHED_IN, S._OPENED_TAB = True, True
    calls = []
    monkeypatch.setattr(S, "switch_back", lambda: (_ for _ in ()).throw(S._Terminated(143)))
    monkeypatch.setattr(S, "close_my_tab", lambda: calls.append("close") or True)
    with pytest.raises(S._Terminated) as e:
        S.switch_back_after(None, False)
    assert e.value.code == 143 and calls == ["close"]


def test_sigterm_during_the_close_propagates(monkeypatch):
    S._SWITCHED_IN, S._OPENED_TAB = True, True
    monkeypatch.setattr(S, "switch_back", lambda: True)
    monkeypatch.setattr(S, "close_my_tab", lambda: (_ for _ in ()).throw(S._Terminated(143)))
    with pytest.raises(S._Terminated):
        S.switch_back_after(None, False)


def test_helpers_do_not_swallow_a_termination(monkeypatch):
    c = Chrome(monkeypatch, tabs=[OWN_TAB])
    _studio_tab_in(monkeypatch, c)
    monkeypatch.setattr(S.H, "_raw", lambda *a, **k: (_ for _ in ()).throw(S._Terminated(143)))
    with pytest.raises(S._Terminated):
        S.switch_back()
    with pytest.raises(S._Terminated):
        S.close_my_tab()


def test_with_browser_exits_143_when_sigterm_hits_during_cleanup(monkeypatch):
    S._SWITCHED_IN, S._OPENED_TAB = True, True
    calls = []
    monkeypatch.setattr(S, "attach", lambda: setattr(S, "_LEASED", True))
    monkeypatch.setattr(S, "release", lambda: calls.append("release"))
    monkeypatch.setattr(S, "switch_back", lambda: (_ for _ in ()).throw(S._Terminated(143)))
    monkeypatch.setattr(S, "close_my_tab", lambda: calls.append("close") or True)
    assert S.with_browser(S.held, None, lambda: None) == 143
    assert calls == ["close", "release"]


def test_keep_on_abort_does_not_hold_back_a_sigterm_switch_back(monkeypatch):
    monkeypatch.setenv("ORION_CLOCK_KEEP_ON_ABORT", "1")
    S._SWITCHED_IN, S._OPENED_TAB = True, True
    calls = []
    monkeypatch.setattr(S, "attach", lambda: setattr(S, "_LEASED", True))
    monkeypatch.setattr(S, "release", lambda: calls.append("release"))
    monkeypatch.setattr(S, "switch_back", lambda: calls.append("back") or True)
    monkeypatch.setattr(S, "close_my_tab", lambda: calls.append("close") or True)
    assert S.with_browser(S.held, None, lambda: os.kill(os.getpid(), signal.SIGTERM)) == 143
    assert calls == ["back", "close", "release"]


def test_an_oserror_writing_the_receipt_note_keeps_a_verified_uploads_exit_code(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w)
    Held(w, monkeypatch, back=True)
    real = S.save

    def save(work, **kw):
        if "switched_back" in kw:
            raise OSError("disk full")
        return real(work, **kw)
    monkeypatch.setattr(S, "save", save)
    assert S.cmd_upload(w) == 0
    assert S.receipt(w)["stage"] == "private_verified"


# ---------- always our own tab, found by a URL token, and H.MARK pinned for the whole run ----------

OWN_DAILY_TAB = f"https://studio.youtube.com/channel/{CHAN}/videos"


def test_a_pre_existing_daily_channel_tab_is_never_reused_acted_on_or_closed(monkeypatch, capsys):
    c = Chrome(monkeypatch, tabs=[OWN_DAILY_TAB])
    _studio_tab_in(monkeypatch, c)
    assert S._OPENED_TAB is True and S._MARK and S.H.MARK == S._MARK
    assert c.new_urls and c.new_urls[0].startswith("about:blank#oclock-studio-")
    assert c.tabs[0]["name"] == "" and c.tabs[1]["name"] == S._MARK
    S.switch_back_after(None, False)
    assert "CLOSED_TAB true" in capsys.readouterr().out
    assert [x["url"] for x in c.tabs] == [OWN_DAILY_TAB] and c.closed == [HOME_HOME]
    assert all(u != OWN_DAILY_TAB for u, _ in c.acted) and all(n[0] != 0 for n in c.nav)
    assert S.H.MARK is None                                   # and the pin is released with the run


def test_the_mark_goes_on_the_token_tab_even_when_someone_closes_a_tab_during_creation(monkeypatch):
    def you_close_your_tab(c):
        del c.tabs[0]                                        # every index shifts under an index diff
    c = Chrome(monkeypatch, tabs=[OWN_DAILY_TAB, OWN_TAB], on_new=you_close_your_tab)
    _studio_tab_in(monkeypatch, c)
    marked = [x["url"] for x in c.tabs if x["name"] == S._MARK]
    assert marked == [DAILY_HOME] and [x["name"] for x in c.tabs if x["url"] == OWN_TAB] == [""]


def test_a_tab_opened_by_someone_else_during_creation_cannot_receive_the_mark(monkeypatch):
    def you_open_a_tab_in_front(c):                        # after the listing, before our batch: our index now points at yours
        c.tabs.insert(len(c.tabs) - 1, {"url": OWN_DAILY_TAB, "name": "", "ses": ""})
    c = Chrome(monkeypatch, tabs=[OWN_TAB], on_batch=you_open_a_tab_in_front)
    monkeypatch.setattr(S.H, "ev", lambda js: CHAN)
    with pytest.raises(SystemExit) as e:
        S.studio_tab()
    assert e.value.code == 1 and S._MARK is None
    assert all(x["name"] == "" and x["ses"] == "" for x in c.tabs) and c.nav == []


def test_a_tab_opened_by_someone_else_before_the_listing_is_not_taken_for_ours(monkeypatch):
    c = Chrome(monkeypatch, tabs=[OWN_TAB], on_new=lambda c: c.tabs.append(
        {"url": OWN_DAILY_TAB, "name": "", "ses": ""}))
    _studio_tab_in(monkeypatch, c)
    assert [x["url"] for x in c.tabs if x["name"] == S._MARK] == [DAILY_HOME]


class PinnedChrome:
    """Routes pinned.py's own subprocess batches (its pinned operations) into a Chrome fake."""

    def __init__(self, monkeypatch, chrome):
        self.chrome, self.cmds = chrome, []
        monkeypatch.setattr(S.H.subprocess, "run", self.run)

    def run(self, cmd, input=None, **kw):
        self.cmds.append(cmd)
        out = json.dumps(self.chrome.batch(json.loads(input))) if "batch" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")




def test_every_pinned_operation_acts_on_our_marked_tab_even_with_a_second_matching_tab(monkeypatch):
    c = Chrome(monkeypatch, tabs=[OWN_DAILY_TAB])
    _studio_tab_in(monkeypatch, c)
    mine_idx = next(i for i, x in enumerate(c.tabs) if x["name"] == S._MARK)
    monkeypatch.setattr(S.H, "ev", REAL_EV)   # the real H.ev and H.my_tab from here; only the browser is faked
    monkeypatch.setattr(S.H, "_raw", c.raw)
    monkeypatch.setattr(S.H, "wait", lambda ms: None)
    PinnedChrome(monkeypatch, c)
    assert S.H.my_tab() == str(mine_idx)
    c.acted.clear()
    S.H.ev("1 + 1")
    S.H.ab("open", DAILY_HOME + "/videos")
    assert [u for u, _ in c.acted if u == OWN_DAILY_TAB] == []


def test_pinned_operations_refuse_a_tab_that_does_not_carry_our_mark(monkeypatch):
    c = Chrome(monkeypatch, tabs=[OWN_DAILY_TAB])
    _studio_tab_in(monkeypatch, c)
    for x in c.tabs:
        x["name"] = x["ses"] = ""
    monkeypatch.setattr(S.H, "ev", REAL_EV)
    monkeypatch.setattr(S.H, "_raw", c.raw)
    monkeypatch.setattr(S.H, "wait", lambda ms: None)
    PinnedChrome(monkeypatch, c)
    c.acted.clear()
    c.nav.clear()
    with pytest.raises(SystemExit):
        S.H.ev("1 + 1")
    with pytest.raises(SystemExit):
        S.H.ab("open", DAILY_HOME + "/videos")
    assert c.acted == [] and c.nav == []


def test_the_pin_survives_a_cleared_window_name_through_the_session_mark(monkeypatch):
    c = Chrome(monkeypatch, tabs=[OWN_DAILY_TAB])
    _studio_tab_in(monkeypatch, c)
    ours = next(x for x in c.tabs if x["name"] == S._MARK)
    ours["name"] = ""                       # a navigation cleared window.name; sessionStorage still has the mark
    monkeypatch.setattr(S.H, "ev", REAL_EV)
    monkeypatch.setattr(S.H, "_raw", c.raw)
    monkeypatch.setattr(S.H, "wait", lambda ms: None)
    PinnedChrome(monkeypatch, c)
    assert S.H.ev("1 + 1") is not None


# ---------- the close is one batch that re-checks the mark; the ledger append writes every byte ----------

def test_the_close_is_one_batch_that_selects_rechecks_the_mark_then_closes(monkeypatch):
    c = Chrome(monkeypatch, tabs=[OWN_DAILY_TAB])
    _studio_tab_in(monkeypatch, c)
    seen = []
    real = c.raw
    monkeypatch.setattr(S.H, "_raw", lambda *a, **k: (seen.append((a[:1], json.loads(k["stdin"]) if k.get("stdin") else None)),
                                                       real(*a, **k))[1])
    assert S.close_my_tab() is True
    closes = [steps for a, steps in seen if steps and any(st[:2] == ["tab", "close"] for st in steps)]
    assert len(closes) == 1
    kinds = [st[0] if st[0] != "eval" else "guard" for st in closes[0]]
    assert kinds == ["tab", "guard", "tab"] and closes[0][2] == ["tab", "close"]
    assert not [1 for a, _ in seen if a[:2] == ("tab", "close")]          # and never as a separate command


@pytest.mark.parametrize("shift", ["yours_in_front", "yours_closed_in_front", "ours_moved_behind"])
def test_a_tab_shift_between_finding_and_closing_never_closes_another_tab(monkeypatch, shift):
    c = Chrome(monkeypatch, tabs=[OWN_DAILY_TAB, OWN_TAB])
    _studio_tab_in(monkeypatch, c)
    ours = next(x for x in c.tabs if x["name"] == S._MARK)
    real_find = S._find_mark

    def find_then_shift():
        idx = real_find()
        if shift == "yours_in_front":
            c.tabs.insert(0, {"url": OWN_TAB + "?z=1", "name": "", "ses": ""})
        elif shift == "yours_closed_in_front":
            del c.tabs[0]
        else:
            c.tabs.remove(ours)
            c.tabs.insert(0, ours)
            c.tabs.append({"url": OWN_TAB + "?z=2", "name": "", "ses": ""})
        return idx
    monkeypatch.setattr(S, "_find_mark", find_then_shift)
    S.close_my_tab()
    assert all(x["name"] == "" for x in c.tabs if x is not ours)
    assert not [u for u in c.closed if u in (OWN_DAILY_TAB, OWN_TAB, OWN_TAB + "?z=1", OWN_TAB + "?z=2")]


def test_a_failed_guard_in_the_close_batch_closes_nothing(monkeypatch):
    c = Chrome(monkeypatch, tabs=[OWN_DAILY_TAB])
    _studio_tab_in(monkeypatch, c)
    real_find = S._find_mark
    monkeypatch.setattr(S, "_find_mark", lambda: (lambda i: (c.tabs.insert(0, {"url": OWN_TAB, "name": "", "ses": ""}), i)[1])(real_find()))
    n = len(c.tabs)
    assert S.close_my_tab() is False
    assert len(c.tabs) == n + 1 and c.closed == []


class PieceWriter:
    """os.write that accepts at most `step` bytes per call (as a pipe or a signal-interrupted write can)."""

    def __init__(self, step, real, fail_after=None):
        self.step, self.real, self.calls, self.fail_after = step, real, 0, fail_after

    def __call__(self, fd, data):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise OSError(28, "No space left on device")
        return self.real(fd, bytes(data)[:self.step])


def test_a_ledger_row_written_in_pieces_is_complete(tmp_path, monkeypatch):
    p = tmp_path / "ledger.jsonl"
    w = PieceWriter(7, os.write)
    monkeypatch.setattr(S.os, "write", w)
    S._ledger_append(p, event="upload_started", day=DAY, subject_sha256="a" * 64, id="daily-" + DAY)
    restore_os(monkeypatch, "write")
    rows = [json.loads(x) for x in p.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["event"] == "upload_started" and rows[0]["day"] == DAY
    assert w.calls > 5


def test_a_ledger_write_that_makes_no_progress_raises_and_leaves_no_partial_row(tmp_path, monkeypatch):
    p = tmp_path / "ledger.jsonl"
    S._ledger_append(p, event="old", day="2026-10-15")
    good = p.read_bytes()
    monkeypatch.setattr(S.os, "write", lambda fd, data: 0)
    with pytest.raises(OSError):
        S._ledger_append(p, event="upload_started", day=DAY)
    restore_os(monkeypatch, "write")
    assert p.read_bytes() == good


def test_a_ledger_write_that_fails_midway_is_rolled_back_and_not_made_durable(tmp_path, monkeypatch):
    p = tmp_path / "ledger.jsonl"
    S._ledger_append(p, event="old", day="2026-10-15")
    good = p.read_bytes()
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(S.os, "write", PieceWriter(5, os.write, fail_after=2))
    monkeypatch.setattr(S.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    with pytest.raises(OSError):
        S._ledger_append(p, event="upload_started", day=DAY, subject_sha256="a" * 64)
    restore_os(monkeypatch, "write", "fsync")
    assert p.read_bytes() == good and synced == []


def test_the_reader_still_fails_closed_on_a_torn_row(tmp_path, monkeypatch, ledger_env):
    ledger_env.write_text('{"event": "old", "day": "2026-10-15"}\n{"event": "upload_st')
    with pytest.raises(SystemExit) as e:
        S.ledger_guard(DAY)
    assert e.value.code == 4


# ---------- an empty channel read gets one re-open; a stop at identity keeps a private screenshot ----------



def _reads(monkeypatch, *seq):
    """H.ev answers for the channel read, one per call (the last repeats). Returns the list of scripts it saw."""
    calls = []

    def ev(js):
        calls.append(js)
        return seq[min(len(calls), len(seq)) - 1]
    monkeypatch.setattr(S.H, "ev", ev)
    return calls


def _daily_open_batches(c):
    return [b for b in c.batches if ["open", DAILY_HOME] in b]


@pytest.mark.parametrize("empty", [None, ""])
def test_an_empty_channel_read_gets_one_reopen_of_the_daily_home_then_continues(monkeypatch, capsys, empty):
    c = Chrome(monkeypatch, tabs=[OWN_TAB])
    reads = _reads(monkeypatch, empty, CHAN)
    S.studio_tab()
    assert len(reads) == 2 and S._SWITCHED_IN is True
    opens = _daily_open_batches(c)
    assert len(opens) == 2                               # the open at creation and exactly one re-open
    assert [b[3] for b in opens] == [["wait", "7000"], ["wait", "7000"]]     # the retry reuses the 7000 ms wait
    out = capsys.readouterr().out
    assert c.shots == [] and "studio-stop screenshot" not in out and "ok   identity" in out


import stat  # noqa: E402


def _stop_shots(state):
    d = state / "_studio-stops"
    return sorted(d.glob("*.png")) if d.exists() else []


def _upload_through_main(tmp_path, monkeypatch, capsys):
    w = day_dir(tmp_path)
    item_in(w)
    monkeypatch.setattr(S, "with_browser", lambda fn, *a: Flow.with_browser(fn, *a))
    rc = S.main(["x", "upload", str(w)])
    return rc, capsys.readouterr().out.splitlines()


def test_an_empty_read_twice_retries_once_keeps_a_private_screenshot_then_stops(tmp_path, monkeypatch, capsys, stop_state):
    c = Chrome(monkeypatch, tabs=[OWN_TAB])
    reads = _reads(monkeypatch, None)
    rc, out = _upload_through_main(tmp_path, monkeypatch, capsys)
    assert rc == 4 and out[-1] == "NEEDS_YOU wrong_channel"
    assert len(reads) == 2 and len(_daily_open_batches(c)) == 2         # exactly one retry, never a loop
    shots = _stop_shots(stop_state)
    assert len(shots) == 1 and re.fullmatch(r"\d{8}T\d{6}Z-wrong_channel\.png", shots[0].name)
    assert stat.S_IMODE(shots[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(shots[0].parent.stat().st_mode) == 0o700
    lines = [ln for ln in out if ln.startswith("studio-stop screenshot:")]
    assert lines == [f"studio-stop screenshot: {shots[0]}"]
    assert out.index(lines[0]) < next(i for i, ln in enumerate(out) if ln.startswith("NEEDS YOU at identity"))
    assert "SWITCHED_BACK true" in out and "CLOSED_TAB true" in out      # the switch back still runs
    assert [x["url"] for x in c.tabs] == [OWN_TAB]


@pytest.mark.parametrize("other", ["UCother", HOME_CHAN])
def test_a_different_channel_is_not_retried_but_keeps_a_screenshot_and_stops(monkeypatch, capsys, stop_state, other):
    c = Chrome(monkeypatch, tabs=[OWN_TAB])
    reads = _reads(monkeypatch, other)
    S._NEEDS_CODE[0] = "needs_you"
    with pytest.raises(SystemExit) as e:
        S.studio_tab()
    assert e.value.code == 4 and S._NEEDS_CODE[0] == "wrong_channel"
    assert len(reads) == 1 and len(_daily_open_batches(c)) == 1         # no retry
    shots = _stop_shots(stop_state)
    assert len(shots) == 1 and f"studio-stop screenshot: {shots[0]}" in capsys.readouterr().out.splitlines()


def test_signed_out_after_the_retry_stays_signed_out_and_reads_nothing_more(monkeypatch, capsys, stop_state):
    c = Chrome(monkeypatch, tabs=[OWN_TAB])
    reads = []

    def ev(js):                          # the first read is empty; by the re-open the browser has signed out
        reads.append(js)
        c.redirect[DAILY_HOME] = "https://accounts.google.com/v3/signin/identifier"
        return None
    monkeypatch.setattr(S.H, "ev", ev)
    S._NEEDS_CODE[0] = "needs_you"
    with pytest.raises(SystemExit) as e:
        S.studio_tab()
    assert e.value.code == 4 and S._NEEDS_CODE[0] == "signed_out"
    assert len(reads) == 1 and len(_daily_open_batches(c)) == 2         # no identity read after the retry
    assert c.shots == [] and _stop_shots(stop_state) == []


def test_a_retry_that_lands_off_the_channel_stops_for_you_with_a_screenshot_not_an_abort(monkeypatch, capsys, stop_state):
    c = Chrome(monkeypatch, tabs=[OWN_TAB])
    reads = []

    def ev(js):
        S.H.my_tab()                         # every real H.ev runs through pinned.py's URL pin first
        reads.append(js)
        c.redirect[DAILY_HOME] = CHOOSER     # by the re-open, Studio sends the tab to the channel chooser
        return None
    monkeypatch.setattr(S.H, "ev", ev)
    S._NEEDS_CODE[0] = "needs_you"
    with pytest.raises(SystemExit) as e:
        S.studio_tab()
    assert e.value.code == 4 and S._NEEDS_CODE[0] == "wrong_channel"
    assert len(reads) == 1 and len(_daily_open_batches(c)) == 2
    assert len(_stop_shots(stop_state)) == 1 and [s[0] for s in c.shots] == [CHOOSER]


def test_a_first_open_that_lands_off_the_channel_keeps_a_screenshot_then_stops(monkeypatch, capsys, stop_state):
    c = Chrome(monkeypatch, tabs=[OWN_TAB], redirect={DAILY_HOME: CHOOSER})
    reads = _reads(monkeypatch, CHAN)
    S._NEEDS_CODE[0] = "needs_you"
    with pytest.raises(SystemExit) as e:
        S.studio_tab()
    assert e.value.code == 4 and S._NEEDS_CODE[0] == "wrong_channel"
    assert reads == [] and len(_daily_open_batches(c)) == 1          # no channel read and no retry on this path
    assert len(_stop_shots(stop_state)) == 1 and [s[0] for s in c.shots] == [CHOOSER]
    assert "studio-stop screenshot: " in capsys.readouterr().out


@pytest.mark.parametrize("when", ["first_open", "retry"])
def test_my_tab_off_the_channel_with_your_daily_tab_open_stops_for_you_not_an_abort(monkeypatch, capsys, stop_state,
                                                                                    when):
    """pinned.py's URL pin checks the mark only when two tabs match. With MY tab sent off the channel (a chooser) and
    your own tab on the upload channel, the pin finds YOUR tab and the mark guard refuses it, which is an abort (exit 1)
    in production. That must end as the needs-you stop with a screenshot of MY tab, and your tab is never touched."""
    c = Chrome(monkeypatch, tabs=[OWN_DAILY_TAB], redirect={DAILY_HOME: CHOOSER} if when == "first_open" else None)
    PinnedChrome(monkeypatch, c)        # the real pinned eval: URL pin, then the mark guard in the same batch
    reads = []

    def ev(js):
        S.H._pinned(["eval", "-b", base64.b64encode(js.encode()).decode()])
        reads.append(js)
        c.redirect[DAILY_HOME] = CHOOSER    # from here on, opening the daily home lands on the chooser
        return None
    monkeypatch.setattr(S.H, "ev", ev)
    S._NEEDS_CODE[0] = "needs_you"
    with pytest.raises(SystemExit) as e:
        S.studio_tab()
    assert e.value.code == 4 and S._NEEDS_CODE[0] == "wrong_channel"
    assert len(reads) == (1 if when == "retry" else 0) and len(_daily_open_batches(c)) == 2   # exactly one re-open
    assert [s[0] for s in c.shots] == [CHOOSER] and len(_stop_shots(stop_state)) == 1
    assert [u for u, _ in c.acted if u == OWN_DAILY_TAB] == []


def test_a_driver_failure_during_the_channel_read_still_aborts_and_is_not_a_needs_you_stop(monkeypatch, capsys, stop_state):
    """Only the pin landing on a tab that is not mine counts as Studio showing no channel. A broken batch or a dead
    browser during the read on MY tab is a driver failure: exit 1 as before, no re-open, no screenshot, no needs-you stop."""
    c = Chrome(monkeypatch, tabs=[OWN_TAB])

    def ev(js):
        S.H.die("batch", "no JSON from agent-browser")      # what pinned._pinned does on a broken batch
    monkeypatch.setattr(S.H, "ev", ev)
    S._NEEDS_CODE[0] = "needs_you"
    with pytest.raises(SystemExit) as e:
        S.studio_tab()
    assert e.value.code == 1 and S._NEEDS_CODE[0] == "needs_you"
    assert len(_daily_open_batches(c)) == 1 and c.shots == []
    assert "NEEDS YOU" not in capsys.readouterr().out


def test_a_termination_during_the_channel_read_propagates_and_is_not_an_empty_read(monkeypatch, stop_state):
    c = Chrome(monkeypatch, tabs=[OWN_TAB])

    def ev(js):
        raise S._Terminated(143)
    monkeypatch.setattr(S.H, "ev", ev)
    with pytest.raises(S._Terminated):
        S.studio_tab()
    assert len(_daily_open_batches(c)) == 1 and c.shots == []      # no re-open, no screenshot after a termination


@pytest.mark.parametrize("how", ["fail", "nofile", "raise"])
def test_a_screenshot_failure_prints_none_and_changes_neither_exit_nor_code(tmp_path, monkeypatch, capsys, stop_state, how):
    c = Chrome(monkeypatch, tabs=[OWN_TAB], shot="fail" if how == "fail" else "nofile")
    if how == "raise":
        real = c.raw

        def raw(*a, **k):
            if a[:1] == ("batch",) and any(st[0] == "screenshot" for st in json.loads(k["stdin"])):
                raise OSError("agent-browser went away")
            return real(*a, **k)
        monkeypatch.setattr(S.H, "_raw", raw)
    _reads(monkeypatch, None)
    rc, out = _upload_through_main(tmp_path, monkeypatch, capsys)
    assert rc == 4 and out[-1] == "NEEDS_YOU wrong_channel"
    lines = [ln for ln in out if ln.startswith("studio-stop screenshot:")]
    assert len(lines) == 1 and re.fullmatch(r"studio-stop screenshot: none \([A-Za-z ]+\)", lines[0]), lines
    assert "went away" not in "\n".join(out)                           # a short fixed reason, never driver or page text
    assert _stop_shots(stop_state) == []
    assert "SWITCHED_BACK true" in out and "CLOSED_TAB true" in out


def test_a_retry_that_cannot_find_our_tab_reads_nothing_more_and_stops_as_today(monkeypatch, capsys, stop_state):
    c = Chrome(monkeypatch, tabs=[OWN_DAILY_TAB])
    reads = []

    def ev(js):                          # the first read is empty, and our tab's mark is gone by the retry
        reads.append(js)
        for t in c.tabs:
            t["name"] = t["ses"] = ""
        return None
    monkeypatch.setattr(S.H, "ev", ev)
    S._NEEDS_CODE[0] = "needs_you"
    with pytest.raises(SystemExit) as e:
        S.studio_tab()
    assert e.value.code == 4 and S._NEEDS_CODE[0] == "wrong_channel"
    assert len(reads) == 1 and len(_daily_open_batches(c)) == 1         # no re-open on an uncertain tab
    assert c.shots == [] and "studio-stop screenshot: none (my tab is not certain)" in capsys.readouterr().out
    assert all(u != OWN_DAILY_TAB for u, _ in c.acted)


def test_a_reopen_whose_batch_fails_is_a_driver_abort_like_the_first_open(monkeypatch, capsys, stop_state):
    """My tab is found, but the re-open batch itself fails (no JSON, a dead browser): a driver failure, so exit 1 as
    when the first open fails, never a needs-you account stop."""
    c = Chrome(monkeypatch, tabs=[OWN_TAB])
    reads = _reads(monkeypatch, None)
    real_batch, opens = S._batch, []

    def batch(steps):
        if ["open", DAILY_HOME] in steps:
            opens.append(steps)
            if len(opens) == 2:
                return None                  # agent-browser gave no JSON for the re-open
        return real_batch(steps)
    monkeypatch.setattr(S, "_batch", batch)
    S._NEEDS_CODE[0] = "needs_you"
    with pytest.raises(SystemExit) as e:
        S.studio_tab()
    assert e.value.code == 1 and S._NEEDS_CODE[0] == "needs_you"
    assert len(reads) == 1 and len(opens) == 2 and c.shots == []
    assert "NEEDS YOU" not in capsys.readouterr().out


def test_the_stop_screenshots_keep_the_newest_20_pngs_and_touch_nothing_else(monkeypatch, capsys, stop_state):
    d = stop_state / "_studio-stops"
    (d / "sub").mkdir(parents=True)
    old = []
    for i in range(25):
        p = d / f"20261001T0000{i:02d}Z-wrong_channel.png"
        p.write_bytes(b"old")
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
        old.append(p)
    others = [d / "notes.txt", d / "sub" / "old.png", stop_state / "outside.png", d / "old.PNG.bak"]
    for p in others:
        p.write_bytes(b"keep")
        os.utime(p, (1_600_000_000, 1_600_000_000))
    Chrome(monkeypatch, tabs=[OWN_TAB])
    _reads(monkeypatch, None)
    with pytest.raises(SystemExit):
        S.studio_tab()
    shots = _stop_shots(stop_state)
    new = [p for p in shots if p not in old]
    assert len(shots) == 20 and len(new) == 1
    assert set(shots) == set(old[-19:]) | set(new)                     # the 6 oldest went, the new one stays
    assert all(p.exists() for p in others)


def test_a_symlinked_stop_dir_is_refused_and_nothing_behind_it_is_pruned(monkeypatch, capsys, stop_state, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for i in range(25):
        (elsewhere / f"{i:02d}.png").write_bytes(b"not ours")
    (stop_state / "_studio-stops").symlink_to(elsewhere)
    c = Chrome(monkeypatch, tabs=[OWN_TAB])
    _reads(monkeypatch, None)
    with pytest.raises(SystemExit) as e:
        S.studio_tab()
    assert e.value.code == 4 and S._NEEDS_CODE[0] == "wrong_channel"
    assert len(list(elsewhere.glob("*.png"))) == 25 and c.shots == []
    assert "studio-stop screenshot: none (the stop directory is a symlink)" in capsys.readouterr().out


def _home_open_batches(c):
    return [b for b in c.batches if ["open", HOME_HOME] in b]


@pytest.mark.parametrize("empty", [None, ""])
def test_switch_back_reopens_once_on_an_empty_read_and_is_true_when_home_reads_back(monkeypatch, empty):
    c = Chrome(monkeypatch, tabs=[OWN_TAB])
    _studio_tab_in(monkeypatch, c)
    c.ch_reads = [empty]
    assert S.switch_back() is True
    opens = _home_open_batches(c)
    assert len(opens) == 2 and [b[3] for b in opens] == [["wait", "6000"], ["wait", "6000"]]


def test_switch_back_that_reads_empty_twice_is_false_after_one_retry(monkeypatch):
    c = Chrome(monkeypatch, tabs=[OWN_TAB])
    _studio_tab_in(monkeypatch, c)
    c.ch_reads = [None, ""]
    assert S.switch_back() is False and len(_home_open_batches(c)) == 2


@pytest.mark.parametrize("other", [CHAN, "UCother"])
def test_switch_back_on_a_different_channel_is_false_with_no_retry(monkeypatch, other):
    c = Chrome(monkeypatch, tabs=[OWN_TAB])
    _studio_tab_in(monkeypatch, c)
    c.ch_reads = [other]
    assert S.switch_back() is False and len(_home_open_batches(c)) == 1


def test_every_reopen_and_screenshot_acts_on_our_marked_tab_even_with_a_second_matching_tab(tmp_path, monkeypatch, capsys,
                                                                                           stop_state):
    c = Chrome(monkeypatch, tabs=[OWN_DAILY_TAB, OWN_TAB])
    _reads(monkeypatch, None)
    c.ch_reads = [CHAN, CHAN, None]          # creation and retry read-backs, then an empty first switch-back read
    rc, out = _upload_through_main(tmp_path, monkeypatch, capsys)
    assert rc == 4 and out[-1] == "NEEDS_YOU wrong_channel"
    assert len(_daily_open_batches(c)) == 2 and len(_home_open_batches(c)) == 2    # both retries ran
    assert c.shots and all(m.startswith("oclock-studio-") for _, m, _ in c.shots)   # shot on the tab carrying our mark
    assert [u for u, _ in c.acted if u in (OWN_DAILY_TAB, OWN_TAB)] == []
    assert all(i == 2 for i, _ in c.nav)                                            # every navigation on our tab
    assert [x["url"] for x in c.tabs] == [OWN_DAILY_TAB, OWN_TAB] and all(x["name"] == "" for x in c.tabs)
    assert "SWITCHED_BACK true" in out


@pytest.mark.parametrize("shift", ["yours_in_front", "ours_moved_to_front"])
def test_a_tab_shift_just_before_the_screenshot_never_shoots_another_tab(monkeypatch, capsys, stop_state, shift):
    c = Chrome(monkeypatch, tabs=[OWN_DAILY_TAB, OWN_TAB])
    _reads(monkeypatch, None)
    real_batch = c.batch

    def batch(steps):                        # after our index was found, before the batch selects it
        if any(st[0] == "screenshot" for st in steps):
            if shift == "yours_in_front":
                c.tabs.insert(0, {"url": OWN_TAB + "?z=1", "name": "", "ses": ""})
            else:
                ours = next(x for x in c.tabs if x["name"])
                c.tabs.remove(ours)
                c.tabs.insert(0, ours)
        return real_batch(steps)
    c.batch = batch
    with pytest.raises(SystemExit) as e:
        S.studio_tab()
    assert e.value.code == 4 and S._NEEDS_CODE[0] == "wrong_channel"
    assert c.shots == [] and _stop_shots(stop_state) == []
    assert "studio-stop screenshot: none (the screenshot step failed)" in capsys.readouterr().out


def test_a_termination_during_the_screenshot_propagates(monkeypatch, stop_state):
    c = Chrome(monkeypatch, tabs=[OWN_TAB])
    _studio_tab_in(monkeypatch, c)
    monkeypatch.setattr(S.H, "_raw", lambda *a, **k: (_ for _ in ()).throw(S._Terminated(143)))
    with pytest.raises(S._Terminated):
        S.stop_screenshot("wrong_channel")


# ---------- the visibility comes only from the explicit argument ----------

def test_upload_without_the_argument_or_with_private_is_private(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(S, "cmd_upload", lambda w, vis: seen.append((w, vis)) or 0)
    assert S._dispatch("upload", [str(tmp_path)]) == 0
    assert S._dispatch("upload", [str(tmp_path), "--visibility", "private"]) == 0
    assert seen == [(tmp_path.resolve(), "PRIVATE")] * 2


@pytest.mark.parametrize("extra", [["--visibility"], ["--visibility", "Public"], ["--visibility", "PRIVATE"],
                                   ["--visibility", "unlisted"], ["--visibility", "schedule"], ["--visibility", ""],
                                   ["--visibility", "private", "x"], ["--vis", "private"], ["private"],
                                   ["--visibility", "private", "--visibility", "private"]])
def test_any_other_visibility_argument_is_a_usage_error_before_anything(tmp_path, monkeypatch, extra):
    monkeypatch.setattr(S, "cmd_upload", lambda *a: pytest.fail("ran"))
    assert S.main(["x", "upload", str(tmp_path), *extra]) == 2


def test_the_driver_never_reads_the_mode_from_config_or_environment():
    src = (RELEASE / "studio.py").read_text()
    assert "publish.json" not in src and "ORION_CLOCK_CONFIG" not in src and ".config" not in src
    assert set(re.findall(r"environ\.get\(([^)]*)\)", src)) == {'"ORION_CLOCK_KEEP_ON_ABORT"', '"XDG_RUNTIME_DIR"'}


def test_cmd_upload_refuses_an_unknown_visibility_before_any_browser(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item_in(w)
    no_browser(monkeypatch)
    for vis in ("UNLISTED", "SCHEDULED", "private", "", None):
        with pytest.raises(SystemExit) as e:
            S.cmd_upload(w, vis)
        assert e.value.code == 1, vis
    assert S.receipt(w) == {}


def test_public_is_reached_only_through_the_explicit_argument(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(S, "cmd_upload", lambda w, vis: seen.append(vis) or 0)
    assert S._dispatch("upload", [str(tmp_path), "--visibility", "public"]) == 0
    assert S._dispatch("upload", [str(tmp_path)]) == 0
    assert seen == ["PUBLIC", "PRIVATE"]


@pytest.mark.parametrize("vis,want", [(None, "vis PRIVATE"), ("PRIVATE", "vis PRIVATE"), ("PUBLIC", "vis PUBLIC")])
def test_the_setter_gets_exactly_the_mode_the_upload_was_given(tmp_path, monkeypatch, vis, want):
    w = day_dir(tmp_path)
    item_in(w)
    f = Flow(w, monkeypatch)
    S.cmd_upload(w) if vis is None else S.cmd_upload(w, vis)
    assert [e for e in f.events if e.startswith("vis ")] == [want]


@pytest.mark.parametrize("vis", ["PRIVATE", "PUBLIC"])
def test_the_save_is_proved_for_exactly_the_mode_the_upload_was_given(tmp_path, monkeypatch, vis):
    w = day_dir(tmp_path)
    item_in(w)
    f = Flow(w, monkeypatch)
    S.cmd_upload(w, vis)
    assert f.saved_as == vis


# ---- the receipt, the ledger and a resume keep the visibility the upload began with ----

def test_fresh_upload_in_public_mode_runs_every_step_in_order_and_ends_public_verified(tmp_path, monkeypatch,
                                                                                    ledger_env):
    w = day_dir(tmp_path)
    item = item_in(w)
    f = Flow(w, monkeypatch)
    assert S.cmd_upload(w, "PUBLIC") == 0
    assert f.events == ["tab", "upload_file", "details", "wait", "to_vis", "vis PUBLIC", "commit", "close", "verify"]
    assert f.stage_at["before_file"] == "upload_started" and f.stage_at["claimed_visibility"] == "PUBLIC"
    assert f.stage_at["commit"] == "details" and f.saved_as == "PUBLIC"
    r = S.receipt(w)
    assert (r["stage"], r["visibility"], r["video_id"], r["day"]) == ("public_verified", "PUBLIC", f.vid, DAY)
    rows = ledger_rows(ledger_env)
    assert [x["event"] for x in rows] == ["upload_started", "public_verified"]
    assert rows[1]["video_id"] == f.vid and all(x["subject_sha256"] == item["sha256"] for x in rows)


def test_private_mode_keeps_the_old_receipt_and_ledger(tmp_path, monkeypatch, ledger_env):
    w = day_dir(tmp_path)
    item_in(w)
    Flow(w, monkeypatch)
    assert S.cmd_upload(w, "PRIVATE") == 0
    assert (S.receipt(w)["stage"], S.receipt(w)["visibility"]) == ("private_verified", "PRIVATE")
    assert [x["event"] for x in ledger_rows(ledger_env)] == ["upload_started", "private_verified"]


@pytest.mark.parametrize("stage,recorded", [("private_verified", None), ("private_verified", "PRIVATE"),
                                             ("public_verified", "PUBLIC")])
@pytest.mark.parametrize("vis", ["PRIVATE", "PUBLIC"])
def test_a_finished_day_is_never_flipped(tmp_path, monkeypatch, stage, recorded, vis):
    w = day_dir(tmp_path)
    item = item_in(w)
    rec = dict(day=DAY, subject_sha256=item["sha256"], video_id="vid12345678", stage=stage)
    if recorded:
        rec["visibility"] = recorded
    S.save(w, **rec)
    before = (w / "upload.json").read_bytes()
    no_browser(monkeypatch)
    assert S.cmd_upload(w, vis) == 0
    assert (w / "upload.json").read_bytes() == before


@pytest.mark.parametrize("stage", ["uploaded", "details", "saved"])
@pytest.mark.parametrize("vis", ["PRIVATE", "PUBLIC"])
def test_a_resumed_draft_keeps_the_visibility_it_began_with(tmp_path, monkeypatch, stage, vis):
    w = day_dir(tmp_path)
    item = item_in(w)
    S.save(w, day=DAY, subject_sha256=item["sha256"], video_id="vid12345678", stage=stage, visibility=vis)
    f = Flow(w, monkeypatch)
    assert S.cmd_upload(w, vis) == 0
    assert "upload_file" not in f.events
    if stage != "saved":
        assert f"vis {vis}" in f.events and f.saved_as == vis
    assert S.receipt(w)["stage"] == f"{vis.lower()}_verified"


@pytest.mark.parametrize("stage", ["uploaded", "details", "saved"])
@pytest.mark.parametrize("recorded,vis", [("PRIVATE", "PUBLIC"), ("PUBLIC", "PRIVATE"), (None, "PUBLIC"),
                                          ("UNLISTED", "PRIVATE"), ("UNLISTED", "PUBLIC"), ("public", "PUBLIC")])
def test_a_draft_begun_with_another_visibility_needs_you_before_any_browser(tmp_path, monkeypatch, capsys, stage,
                                                                          recorded, vis):
    w = day_dir(tmp_path)
    item = item_in(w)
    rec = dict(day=DAY, subject_sha256=item["sha256"], video_id="vid12345678", stage=stage)
    if recorded is not None:
        rec["visibility"] = recorded
    S.save(w, **rec)
    no_browser(monkeypatch)
    capsys.readouterr()
    argv = ["x", "upload", str(w)] + (["--visibility", vis.lower()] if vis == "PUBLIC" else [])
    assert S.main(argv) == 4
    assert _last_line(capsys) == "NEEDS_YOU visibility_mismatch"
    assert S.receipt(w)["stage"] == stage


def test_a_legacy_draft_with_no_recorded_visibility_resumes_private(tmp_path, monkeypatch):
    w = day_dir(tmp_path)
    item = item_in(w)
    S.save(w, day=DAY, subject_sha256=item["sha256"], video_id="vid12345678", stage="details")
    f = Flow(w, monkeypatch)
    assert S.cmd_upload(w) == 0
    assert "vis PRIVATE" in f.events and S.receipt(w)["stage"] == "private_verified"


def test_the_recorded_visibility_can_be_set_once_and_never_changed(tmp_path):
    S.save(tmp_path, visibility="PUBLIC")
    S.save(tmp_path, visibility="PUBLIC")
    with pytest.raises(S.ReceiptError) as e:
        S.save(tmp_path, visibility="PRIVATE")
    assert e.value.code == "receipt_identity_changed"


def test_the_terminal_stages_and_codes():
    assert S.VERIFIED == {"PRIVATE": "private_verified", "PUBLIC": "public_verified"}
    assert set(S.RESUMABLE) == {"uploaded", "details", "save_clicked", "saved", "private_verified", "public_verified"}
    assert S.SAVE_CLICKED == "save_clicked"
    assert "visibility_mismatch" in S.NEEDS_YOU_CODES


# ---- verify: a fresh Studio read shows the chosen visibility; public needs oEmbed 200 within a bound ----

PUBLIC_READ = dict(PRIVATE_READ, visibility="Public")


def test_the_oembed_bound_is_pinned():
    assert (S.OEMBED_TRIES, S.OEMBED_WAIT_S) == (10, 30)


def test_verify_checks_need_public_and_an_answering_oembed():
    vc = lambda g, st: S.verify_checks(ITEM, g, st, VID, "PUBLIC", oembed_valid=True)      # noqa: E731
    pub = good_read(visibility="Public")
    assert all(vc(pub, 200).values())
    assert not all(S.verify_checks(ITEM, pub, 200, VID, "PUBLIC").values())        # a 200 alone is not enough
    assert not all(S.verify_checks(ITEM, pub, 200, VID, "PUBLIC", oembed_valid=False).values())
    for st in (401, 403, 404, None, 500, "200"):
        assert not all(vc(pub, st).values()), st
    assert not all(vc(dict(pub, title="other"), 200).values())
    assert not all(vc(dict(pub, description="other"), 200).values())
    assert not all(vc(None, 200).values())
    assert not all(S.verify_checks(ITEM, pub, 200, None, "PUBLIC").values())


@pytest.mark.parametrize("vis", ["Private", "Unlisted", "Scheduled", "Public Private", "Public, scheduled for Oct 19",
                                 "Unlisted Public", "public", ""])
def test_verify_public_visibility_rejects_any_other_visibility_text(vis):
    assert not S.verify_checks(ITEM, good_read(visibility=vis), 200, VID, "PUBLIC")["visibility shows Public"]


@pytest.mark.parametrize("flags", [{"kids": "false", "altered": "true"}, {"kids": "true", "altered": "false"},
                                   {"kids": "unreadable", "altered": "true"}, {}, None, {"kids": True, "altered": True}])
def test_verify_public_needs_both_flags_read_back_true(flags):
    c = S.verify_checks(ITEM, good_read(visibility="Public", flags=flags), 200, VID, "PUBLIC")
    assert not all(c.values()) and not (c["not made for kids"] and c["altered content is yes"])


@pytest.mark.parametrize("loc", [None, "", "https://studio.youtube.com/video/OTHERVIDEO1/edit"])
def test_verify_public_needs_the_page_to_be_this_videos(loc):
    assert not S.verify_checks(ITEM, good_read(visibility="Public", location=loc), 200, VID, "PUBLIC")[
        "page is this video's"]


def test_private_verify_checks_are_unchanged_by_default():
    assert list(S.verify_checks(ITEM, good_read(), 404, VID)) == [
        "title", "description", "page is this video's", "visibility shows Private", "not made for kids",
        "altered content is yes", "oEmbed refuses (not public)"]
    assert S.verify_checks(ITEM, good_read(), 404, VID) == S.verify_checks(ITEM, good_read(), 404, VID, "PRIVATE")


@pytest.mark.parametrize("want", ["UNLISTED", "SCHEDULED", "public", "", None, ["PUBLIC"]])
def test_verify_checks_never_pass_anything_outside_the_two(want):
    for got, st in ((good_read(), 404), (good_read(visibility="Public"), 200), (good_read(visibility="Unlisted"), 200)):
        assert not all(S.verify_checks(ITEM, got, st, VID, want).values())


def poll_env(monkeypatch, got, answers):
    """answers: a status (200 means a valid oEmbed body) or (status, body is valid oEmbed for this id)."""
    calls = verify_env(monkeypatch, got, None)
    seq = [a if isinstance(a, tuple) else (a, a == 200) for a in answers]
    pauses = []

    def nxt(v):
        calls.append(("oembed", v))
        return seq.pop(0) if len(seq) > 1 else seq[0]
    monkeypatch.setattr(S, "oembed", lambda v: nxt(v)[0])
    monkeypatch.setattr(S, "oembed_video", nxt)
    monkeypatch.setattr(S, "_pause", lambda sec: (calls.append(("pause", sec)), pauses.append(sec)))
    return calls, pauses


def test_verify_one_public_passes_on_studio_public_and_oembed_200(monkeypatch):
    calls, pauses = poll_env(monkeypatch, PUBLIC_READ, [200])
    r = S.verify_one(ITEM, "vid12345678", "PUBLIC")
    assert r["pass"] is True and r["oembed"] == 200 and pauses == [] and not r.get("unconfirmed")
    assert [c for c in calls if c[0] == "oembed"] == [("oembed", "vid12345678")]


@pytest.mark.parametrize("answers", [[404, 200], [404, None, 403, 200], [None] * 9 + [200]])
def test_verify_one_public_waits_for_oembed_within_the_bound(monkeypatch, answers):
    calls, pauses = poll_env(monkeypatch, PUBLIC_READ, answers)
    r = S.verify_one(ITEM, "vid12345678", "PUBLIC")
    assert r["pass"] is True and r["oembed"] == 200
    assert pauses == [30] * (len(answers) - 1)
    kinds = [c[0] for c in calls]
    assert kinds.index("ev") < kinds.index("oembed")               # the Studio read comes first


@pytest.mark.parametrize("answer", [404, None, 401, 500])
def test_verify_one_public_that_never_answers_200_is_unconfirmed_after_ten_tries(monkeypatch, answer):
    calls, pauses = poll_env(monkeypatch, PUBLIC_READ, [answer])
    r = S.verify_one(ITEM, "vid12345678", "PUBLIC")
    assert r["pass"] is False and r["unconfirmed"] is True and r["oembed_tries"] == 10
    assert len([c for c in calls if c[0] == "oembed"]) == 10 and pauses == [30] * 9


@pytest.mark.parametrize("got", [PRIVATE_READ, dict(PUBLIC_READ, visibility="Unlisted"), dict(PUBLIC_READ, title="x"),
                                 dict(PUBLIC_READ, location="https://studio.youtube.com/video/OTHERVIDEO1/edit"), None])
def test_verify_one_public_with_a_failing_studio_read_fails_without_waiting(monkeypatch, got):
    calls, pauses = poll_env(monkeypatch, got, [404])
    r = S.verify_one(ITEM, "vid12345678", "PUBLIC")
    assert r["pass"] is False and not r.get("unconfirmed") and pauses == []


def test_verify_one_private_asks_oembed_once_and_never_waits(monkeypatch):
    for answer in (404, 200, None):
        calls, pauses = poll_env(monkeypatch, PRIVATE_READ, [answer])
        r = S.verify_one(ITEM, "vid12345678", "PRIVATE")
        assert r["pass"] is (answer == 404) and pauses == [] and not r.get("unconfirmed")
        assert len([c for c in calls if c[0] == "oembed"]) == 1


def test_verify_one_never_clicks_saves_or_touches_the_visibility(monkeypatch):
    calls, _ = poll_env(monkeypatch, PUBLIC_READ, [404])
    S.verify_one(ITEM, "vid12345678", "PUBLIC")
    scripts = [c[1] for c in calls if c[0] == "ev"]
    assert scripts and not any(re.search(r"click|privacy-radios|done-button", js) for js in scripts)
    assert [c[1][0] for c in calls if c[0] == "ab"] == ["open"]


def test_public_unconfirmed_is_exit_4_and_never_saves_again(tmp_path, monkeypatch, capsys, ledger_env):
    w = day_dir(tmp_path)
    item_in(w)
    f = Flow(w, monkeypatch, unconfirmed=True)
    capsys.readouterr()
    assert S.main(["x", "upload", str(w), "--visibility", "public"]) == 4
    assert _last_line(capsys) == "NEEDS_YOU public_unconfirmed"
    assert f.events.count("commit") == 1 and f.verified_as == "PUBLIC"
    r = S.receipt(w)
    assert r["stage"] == "saved" and r["verify"]["unconfirmed"] is True
    assert [x["event"] for x in ledger_rows(ledger_env)] == ["upload_started"]
    f2 = Flow(w, monkeypatch)                   # you rerun once oEmbed answers: verify only, no second save
    assert S.cmd_upload(w, "PUBLIC") == 0
    assert f2.events == ["tab", "verify"] and S.receipt(w)["stage"] == "public_verified"


def test_a_failed_public_verify_is_a_needs_you_stop_and_keeps_the_saved_stage(tmp_path, monkeypatch, capsys):
    w = day_dir(tmp_path)
    item_in(w)
    Flow(w, monkeypatch, verify_pass=False)
    capsys.readouterr()
    assert S.main(["x", "upload", str(w), "--visibility", "public"]) == 4
    assert _last_line(capsys) == "NEEDS_YOU public_unconfirmed"
    assert S.receipt(w)["stage"] == "saved"


def test_the_flow_verifies_the_visibility_it_saved(tmp_path, monkeypatch):
    for vis, day in (("PRIVATE", "2026-10-11"), ("PUBLIC", "2026-10-12")):
        w = day_dir(tmp_path, day)
        item_in(w)
        f = Flow(w, monkeypatch)
        assert S.cmd_upload(w, vis) == 0 and f.verified_as == vis


def test_verify_command_takes_the_visibility_only_from_the_argument(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(S, "cmd_verify", lambda w, vis: seen.append(vis) or 0)
    assert S._dispatch("verify", [str(tmp_path)]) == 0
    assert S._dispatch("verify", [str(tmp_path), "--visibility", "public"]) == 0
    assert seen == ["PRIVATE", "PUBLIC"]
    for extra in (["--visibility", "Public"], ["--visibility", "unlisted"], ["--visibility"]):
        assert S.main(["x", "verify", str(tmp_path), *extra]) == 2


@pytest.mark.parametrize("stage", ["saved", "public_verified"])
def test_verify_command_judges_public_when_told_and_the_receipt_agrees(tmp_path, monkeypatch, capsys, stage):
    w = day_dir(tmp_path)
    item = item_in(w)
    S.save(w, day=DAY, subject_sha256=item["sha256"], video_id="vid12345678", stage=stage, visibility="PUBLIC")
    f = Flow(w, monkeypatch)
    assert S.cmd_verify(w, "PUBLIC") == 0 and f.verified_as == "PUBLIC"
    assert S.receipt(w)["stage"] == stage          # verify is a read: it never advances the stage


@pytest.mark.parametrize("recorded,vis", [("PUBLIC", "PRIVATE"), ("PRIVATE", "PUBLIC"), (None, "PUBLIC")])
def test_verify_command_with_another_visibility_than_the_receipt_needs_you(tmp_path, monkeypatch, capsys, recorded,
                                                                           vis):
    w = day_dir(tmp_path)
    item = item_in(w)
    rec = dict(day=DAY, subject_sha256=item["sha256"], video_id="vid12345678", stage="saved")
    if recorded:
        rec["visibility"] = recorded
    S.save(w, **rec)
    no_browser(monkeypatch)
    capsys.readouterr()
    argv = ["x", "verify", str(w)] + (["--visibility", "public"] if vis == "PUBLIC" else [])
    assert S.main(argv) == 4 and _last_line(capsys) == "NEEDS_YOU visibility_mismatch"


def test_verify_command_unconfirmed_is_exit_4_public_unconfirmed(tmp_path, monkeypatch, capsys):
    w = day_dir(tmp_path)
    item = item_in(w)
    S.save(w, day=DAY, subject_sha256=item["sha256"], video_id="vid12345678", stage="saved", visibility="PUBLIC")
    Flow(w, monkeypatch, unconfirmed=True)
    capsys.readouterr()
    assert S.main(["x", "verify", str(w), "--visibility", "public"]) == 4
    assert _last_line(capsys) == "NEEDS_YOU public_unconfirmed"


def test_public_unconfirmed_is_a_known_code():
    assert "public_unconfirmed" in S.NEEDS_YOU_CODES



# ---- the done-button label is exact per value, and the "Video published" dialog ----

def test_only_the_label_map_and_the_published_dialog_may_say_publish():
    assert {n for n, toks in ALLOW.items() if "publish" in toks} == {"DONE_LABELS", "PUBLISHED_JS",
                                                                      "close_published_dialog", "NOT_PUBLIC"}
    assert "publish" not in ALLOW["visibility_ok"] | ALLOW["commit_visibility"] | ALLOW["READ_VIS"]


def dialog_run(js, dialogs):
    need_node()
    r = subprocess.run([NODE, "-e", ACK_PRELUDE % (json.dumps(dialogs), json.dumps(js))], capture_output=True, text=True,
                       timeout=30)
    assert r.returncode == 0, r.stderr[-300:]
    return json.loads(r.stdout)


PUB_DIALOG = {"text": "Video published\nShare this video", "w": 1,
              "buttons": [{"label": "Copy link"}, {"label": "Publish"}, {"label": "Close"}]}


def close_js(browser):
    fb = browser({'"Close"': True})
    assert S.close_published_dialog() is True
    return fb.calls[0]


def test_the_published_dialog_is_closed_only_by_its_own_close_button(browser):
    js = close_js(browser)
    assert dialog_run(js, [PUB_DIALOG])["clicked"] == ["Video published\nShare this video|Close"]
    other = {"text": "Video saved", "w": 1, "buttons": [{"label": "Close"}]}
    assert dialog_run(js, [other, PUB_DIALOG])["clicked"] == ["Video published\nShare this video|Close"]
    assert dialog_run(js, [other])["clicked"] == []
    assert dialog_run(js, [dict(PUB_DIALOG, w=0)])["clicked"] == []
    hidden_close = dict(PUB_DIALOG, buttons=[{"label": "Close", "w": 0}, {"label": "Publish"}])
    r = dialog_run(js, [hidden_close])
    assert r["clicked"] == [] and r["result"] is False
    for label in ("Closed", "close", "Done"):
        assert dialog_run(js, [dict(PUB_DIALOG, buttons=[{"label": label}])])["clicked"] == [], label


def test_the_published_text_comes_from_that_dialog_only_and_reads_nothing_else():
    assert not re.search(r"click|dispatchEvent|setAttribute|focus|submit", S.PUBLISHED_JS)
    other = {"text": "Something else entirely", "w": 1, "buttons": []}
    r = dialog_run(S.PUBLISHED_JS, [other, PUB_DIALOG])
    assert r["clicked"] == [] and r["result"] == "Video published Share this video"
    assert dialog_run(S.PUBLISHED_JS, [other])["result"] is None
    assert dialog_run(S.PUBLISHED_JS, [dict(PUB_DIALOG, w=0)])["result"] is None


def public_commit(browser, published, gone="open", notice=False):
    return browser({'"Close"': True, "done_label": saved(GOOD_PUBLIC), "Video published": published, "Got it": notice,
                    "'gone'": gone, "role=dialog": []})


def test_public_commit_records_the_dialog_text_and_closes_it(browser):
    long = PUBLISHED + " x" * 200
    fb = public_commit(browser, Seq([None, long]))
    assert S.commit_visibility("PUBLIC") == long[:160]
    order = [("close" if '"Close"' in js else "read" if "Video published" in js else "click" if "done_label" in js
              else "ack" if "Got it" in js else "other") for js in fb.calls]
    assert order == ["read", "click", "ack", "read", "close"]       # before, the proof and click, then after
    assert not any("Video (saved" in js for js in fb.calls)       # never the private confirmation


def test_public_commit_acknowledges_the_checking_notice_before_reading_the_dialog(browser):
    fb = public_commit(browser, Seq([None, PUBLISHED]), notice=True)
    assert S.commit_visibility("PUBLIC") == PUBLISHED
    ack = next(i for i, js in enumerate(fb.calls) if "Got it" in js)
    reads = [i for i, js in enumerate(fb.calls) if "Video published" in js and '"Close"' not in js]
    assert reads[0] < ack < reads[1]


def test_public_commit_without_the_dialog_falls_back_to_the_wizard_closed_path(browser):
    fb = public_commit(browser, None, gone="gone")
    assert S.commit_visibility("PUBLIC") == "wizard closed"
    assert not any('"Close"' in js for js in fb.calls)


@pytest.mark.parametrize("gone", ["open", None])
def test_public_commit_without_the_dialog_and_no_proof_the_wizard_closed_exits_1(browser, gone):
    fb = public_commit(browser, None, gone=gone)
    with pytest.raises(SystemExit) as e:
        S.commit_visibility("PUBLIC")
    assert e.value.code == 1 and not any('"Close"' in js for js in fb.calls)


def test_a_stale_published_dialog_is_not_a_new_confirmation(browser):
    fb = public_commit(browser, PUBLISHED, gone="open")
    with pytest.raises(SystemExit):
        S.commit_visibility("PUBLIC")
    assert not any('"Close"' in js for js in fb.calls)


def test_private_commit_never_reads_or_closes_the_published_dialog(browser):
    fb = browser({'"Close"': True, "done_label": saved(GOOD), "Video (saved": Seq([None, "Video saved Close"]),
                  "Video published": PUBLISHED})
    assert S.commit_visibility("PRIVATE") == "Video saved Close"
    assert not any("Video published" in js for js in fb.calls)


def test_public_commit_refuses_a_save_label_and_never_clicks(browser):
    for label in ("Save", "Schedule", ""):
        fb = public_commit(browser, None)
        fb.answers["done_label"] = saved(dict(GOOD_PUBLIC, done_label=label), False)
        with pytest.raises(SystemExit):
            S.commit_visibility("PUBLIC")
        assert not any('"Close"' in js for js in fb.calls), label



# ---- a landed click is on disk before any wait; a terminal stage must name its own visibility ----

def clicked_env(browser, monkeypatch, want, state=None, clicked=True):
    order = []
    fb = browser({'"Close"': True, "done_label": saved(state or GOODS[want], clicked),
                  "Video (saved": Seq([None, "Video saved Close"]), "Video published": Seq([None, PUBLISHED]),
                  "Got it": False, "'gone'": "open", "role=dialog": []})
    monkeypatch.setattr(S.H, "wait", lambda ms: order.append("wait"))
    return fb, order


@pytest.mark.parametrize("want", WANTS)
def test_the_attempt_is_recorded_just_before_the_click_is_dispatched(browser, monkeypatch, want):
    fb, order = clicked_env(browser, monkeypatch, want)
    S.commit_visibility(want, on_attempt=lambda: order.append(("attempt", len(fb.calls))))
    first = order[0]
    assert first[0] == "attempt" and not any("done_label" in js for js in fb.calls[:first[1]])   # before the click
    assert "done_label" in fb.calls[first[1]]                                                   # the next call is it
    assert order.count("wait") >= 1 and order.index(first) < order.index("wait")


@pytest.mark.parametrize("want", WANTS)
def test_the_attempt_is_recorded_whatever_the_click_script_reports(browser, monkeypatch, want):
    for state, clicked in ((GOODS[want], False), (dict(GOODS[want], unlisted="true"), False), (None, False),
                           (GOODS[OTHER[want]], True)):
        fb, order = clicked_env(browser, monkeypatch, want, state=state, clicked=clicked)
        with pytest.raises(SystemExit) as e:
            S.commit_visibility(want, on_attempt=lambda: order.append("attempt"))
        assert e.value.code == 1 and order == ["attempt"], state


def test_an_eval_that_returns_nothing_after_the_attempt_is_still_recorded(browser, monkeypatch):
    browser({"done_label": None, "Video (saved": None, "Video published": None})
    seen = []
    with pytest.raises(SystemExit):
        S.commit_visibility("PUBLIC", on_attempt=lambda: seen.append(1))
    assert seen == [1]


def test_a_refused_visibility_records_no_attempt(browser):
    fb = browser({})
    seen = []
    with pytest.raises(SystemExit):
        S.commit_visibility("UNLISTED", on_attempt=lambda: seen.append(1))
    assert seen == [] and fb.calls == []


@pytest.mark.parametrize("vis", ["PRIVATE", "PUBLIC"])
def test_a_delivered_click_whose_eval_returned_nothing_is_never_clicked_again(tmp_path, monkeypatch, browser, vis):
    real = S.commit_visibility
    w = day_dir(tmp_path)
    item_in(w)
    f = Flow(w, monkeypatch)
    monkeypatch.setattr(S, "commit_visibility", real)
    browser({"done_label": None, "Video (saved": None, "Video published": None})   # the eval timed out after the click
    code = S.cmd_upload(w, vis)
    assert code == (4 if vis == "PUBLIC" else 1)
    assert S.receipt(w)["stage"] == "save_clicked" and "verify" not in f.events
    again = Flow(w, monkeypatch)                   # the rerun goes to verify and never clicks
    assert S.cmd_upload(w, vis) == 0
    assert again.events == ["tab", "verify"] and "commit" not in again.events


@pytest.mark.parametrize("vis", ["PRIVATE", "PUBLIC"])
def test_the_save_clicked_stage_is_written_when_the_save_is_attempted(tmp_path, monkeypatch, vis):
    w = day_dir(tmp_path)
    item_in(w)
    f = Flow(w, monkeypatch)
    assert S.cmd_upload(w, vis) == 0
    assert (f.stage_at["after_click"]["stage"], f.stage_at["after_click"]["visibility"]) == ("save_clicked", vis)


@pytest.mark.parametrize("vis", ["PRIVATE", "PUBLIC"])
def test_a_kill_between_the_click_and_saved_resumes_at_verify_only(tmp_path, monkeypatch, ledger_env, vis):
    w = day_dir(tmp_path)
    item_in(w)
    Flow(w, monkeypatch, kill_after_click=True)
    assert S.cmd_upload(w, vis) == 143
    assert (S.receipt(w)["stage"], S.receipt(w)["visibility"]) == ("save_clicked", vis)
    again = Flow(w, monkeypatch)                     # the rerun never reopens the wizard (no draft_gone)
    assert S.cmd_upload(w, vis) == 0
    assert again.events == ["tab", "verify"] and again.verified_as == vis
    assert S.receipt(w)["stage"] == f"{vis.lower()}_verified"
    assert [x["event"] for x in ledger_rows(ledger_env)] == ["upload_started", f"{vis.lower()}_verified"]


def test_save_clicked_is_resumable_verifiable_and_keeps_its_visibility(tmp_path, monkeypatch, capsys):
    assert "save_clicked" in S.RESUMABLE
    w = day_dir(tmp_path)
    item = item_in(w)
    S.save(w, day=DAY, subject_sha256=item["sha256"], video_id="vid12345678", stage="save_clicked", visibility="PUBLIC")
    no_browser(monkeypatch)
    capsys.readouterr()
    assert S.main(["x", "upload", str(w)]) == 4 and _last_line(capsys) == "NEEDS_YOU visibility_mismatch"
    f = Flow(w, monkeypatch)
    assert S.cmd_verify(w, "PUBLIC") == 0 and f.verified_as == "PUBLIC"


@pytest.mark.parametrize("stage,recorded", [("public_verified", None), ("public_verified", "PRIVATE"),
                                             ("public_verified", "public"), ("private_verified", "PUBLIC"),
                                             ("private_verified", "UNLISTED")])
@pytest.mark.parametrize("vis", ["PRIVATE", "PUBLIC"])
def test_a_terminal_stage_without_its_own_visibility_needs_you_and_is_no_noop(tmp_path, monkeypatch, capsys, stage,
                                                                           recorded, vis):
    w = day_dir(tmp_path)
    item = item_in(w)
    rec = dict(day=DAY, subject_sha256=item["sha256"], video_id="vid12345678", stage=stage)
    if recorded:
        rec["visibility"] = recorded
    S.save(w, **rec)
    no_browser(monkeypatch)
    capsys.readouterr()
    argv = ["x", "upload", str(w)] + (["--visibility", "public"] if vis == "PUBLIC" else [])
    assert S.main(argv) == 4 and _last_line(capsys) == "NEEDS_YOU receipt_not_resumable"



# ---- an exact public proof, a real oEmbed body, one click attempt, public stops are asks ----

@pytest.mark.parametrize("vis", ["Publish", "Published", "Publicly", "Not Public", "Public published", "Public, Publish",
                                 "Public Private", "Public unlisted", "Public (scheduled)", "Public not public",
                                 "public", " Publicity", "Draft Public", "PUBLIC"])
def test_the_public_visibility_label_must_be_exactly_public(vis):
    c = S.verify_checks(ITEM, good_read(visibility=vis), 200, VID, "PUBLIC", oembed_valid=True)
    assert not c["visibility shows Public"], vis


@pytest.mark.parametrize("vis", ["Public", "  Public  ", "Public\nEveryone can watch your video", "Public\t"])
def test_a_plain_public_label_passes(vis):
    c = S.verify_checks(ITEM, good_read(visibility=vis), 200, VID, "PUBLIC", oembed_valid=True)
    assert c["visibility shows Public"] and all(c.values()), vis


def test_the_private_proof_is_as_strict_as_before():
    for vis in ("Private", "Private\nOnly you can watch"):
        assert S.verify_checks(ITEM, good_read(visibility=vis), 404, VID)["visibility shows Private"]
    for vis in ("Private Public", "Unlisted Private", "Private, scheduled", "Public"):
        assert not S.verify_checks(ITEM, good_read(visibility=vis), 404, VID)["visibility shows Private"]


GOOD_OEMBED = json.dumps({"type": "video", "title": "T", "html": f'<iframe src="https://www.youtube.com/embed/{VID}?feature=oembed"></iframe>'})


@pytest.mark.parametrize("body,ok", [
    (GOOD_OEMBED, True),
    (json.dumps({"type": "video", "url": f"https://www.youtube.com/watch?v={VID}"}), True),
    (json.dumps({"type": "rich", "html": f"embed/{VID}"}), False),
    (json.dumps({"type": "video", "html": "embed/OTHERVIDEO1"}), False),
    (json.dumps({"type": "video", "html": f"embed/x{VID}y"}), False),
    (json.dumps({"type": "video"}), False),
    (json.dumps({"type": "video", "html": 5}), False),
    (json.dumps([{"type": "video", "html": VID}]), False),
    ("<html><body>Sign in to YouTube " + VID + "</body></html>", False),
    ("", False), (b"\xff\xfe", False), ("[" * 100000, False)])
def test_only_a_real_oembed_body_for_this_video_confirms(body, ok):
    assert S.oembed_body_ok(body.encode() if isinstance(body, str) else body, VID) is ok


class FakeResp:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_oembed_video_reads_status_and_body(monkeypatch):
    seen = []
    monkeypatch.setattr(S.urllib.request, "urlopen",
                        lambda url, timeout=0: (seen.append(url), FakeResp(200, GOOD_OEMBED.encode()))[1])
    assert S.oembed_video(VID) == (200, True)
    assert seen[0].startswith("https://www.youtube.com/oembed") and seen[0].endswith(f"watch?v={VID}")
    monkeypatch.setattr(S.urllib.request, "urlopen", lambda url, timeout=0: FakeResp(200, b"<html>login</html>"))
    assert S.oembed_video(VID) == (200, False)                    # a 200 that is not oEmbed (a redirect page)

    def refuse(url, timeout=0):
        raise S.urllib.error.HTTPError(url, 404, "nf", None, None)
    monkeypatch.setattr(S.urllib.request, "urlopen", refuse)
    assert S.oembed_video(VID) == (404, False)

    def dead(url, timeout=0):
        raise OSError("down")
    monkeypatch.setattr(S.urllib.request, "urlopen", dead)
    assert S.oembed_video(VID) == (None, False)


def test_a_200_that_is_not_oembed_never_confirms_and_ends_unconfirmed(monkeypatch):
    calls, pauses = poll_env(monkeypatch, PUBLIC_READ, [(200, False)])
    r = S.verify_one(ITEM, "vid12345678", "PUBLIC")
    assert r["pass"] is False and r["unconfirmed"] is True and r["oembed_tries"] == 10 and pauses == [30] * 9


def test_a_non_oembed_200_then_a_real_answer_passes(monkeypatch):
    calls, pauses = poll_env(monkeypatch, PUBLIC_READ, [(200, False), (200, True)])
    r = S.verify_one(ITEM, "vid12345678", "PUBLIC")
    assert r["pass"] is True and pauses == [30]


class FlowStop(Flow):
    """The flow with one step raising, to see what a public run turns it into."""

    def __init__(self, work, monkeypatch, step, exc, **kw):
        super().__init__(work, monkeypatch, **kw)
        self.step, self.exc = step, exc

    def details(self, item):
        super().details(item)
        if self.step == "details":
            raise self.exc

    def commit(self, want, on_attempt=None):
        out = super().commit(want, on_attempt)
        if self.step == "commit":
            raise self.exc
        return out

    def verify(self, item, vid, vis):
        out = super().verify(item, vid, vis)
        if self.step == "verify":
            raise self.exc
        return out


def _stop(step, exc):
    return {"die": SystemExit(1), "error": RuntimeError("x"), "term": S._Terminated(143),
            "you": SystemExit(4)}[exc]


@pytest.mark.parametrize("step", ["commit", "verify"])
@pytest.mark.parametrize("exc", ["die", "error"])
def test_any_public_failure_after_the_attempt_is_a_needs_you_stop(tmp_path, monkeypatch, capsys, step, exc):
    w = day_dir(tmp_path)
    item_in(w)
    FlowStop(w, monkeypatch, step, _stop(step, exc))
    capsys.readouterr()
    assert S.main(["x", "upload", str(w), "--visibility", "public"]) == 4
    assert _last_line(capsys) == "NEEDS_YOU public_unconfirmed"


@pytest.mark.parametrize("exc", ["die", "error"])
def test_a_private_failure_after_the_attempt_keeps_todays_exit(tmp_path, monkeypatch, capsys, exc):
    w = day_dir(tmp_path)
    item_in(w)
    FlowStop(w, monkeypatch, "verify", _stop("verify", exc))
    assert S.main(["x", "upload", str(w)]) == 1
    assert "NEEDS_YOU" not in capsys.readouterr().out


def test_a_public_failure_before_the_attempt_is_an_ordinary_abort(tmp_path, monkeypatch, capsys):
    w = day_dir(tmp_path)
    item_in(w)
    FlowStop(w, monkeypatch, "details", SystemExit(1))
    assert S.main(["x", "upload", str(w), "--visibility", "public"]) == 1
    assert "NEEDS_YOU" not in capsys.readouterr().out


@pytest.mark.parametrize("step", ["commit", "verify"])
def test_a_termination_after_the_attempt_still_propagates(tmp_path, monkeypatch, step):
    w = day_dir(tmp_path)
    item_in(w)
    FlowStop(w, monkeypatch, step, S._Terminated(143))
    assert S.cmd_upload(w, "PUBLIC") == 143


def test_another_needs_you_code_after_the_attempt_keeps_its_code(tmp_path, monkeypatch, capsys):
    w = day_dir(tmp_path)
    item_in(w)

    def wrong(*a):
        S.needs_you("identity", "no", "wrong_channel")
    FlowStop(w, monkeypatch, None, None)
    monkeypatch.setattr(S, "verify_one", lambda item, vid, vis: wrong())
    capsys.readouterr()
    assert S.main(["x", "upload", str(w), "--visibility", "public"]) == 4
    assert _last_line(capsys) == "NEEDS_YOU wrong_channel"


def test_the_attended_public_verify_failure_is_a_needs_you_stop(tmp_path, monkeypatch, capsys):
    w = day_dir(tmp_path)
    item = item_in(w)
    S.save(w, day=DAY, subject_sha256=item["sha256"], video_id="vid12345678", stage="saved", visibility="PUBLIC")
    Flow(w, monkeypatch, verify_pass=False)
    capsys.readouterr()
    assert S.main(["x", "verify", str(w), "--visibility", "public"]) == 4
    assert _last_line(capsys) == "NEEDS_YOU public_unconfirmed"


# ---- no home channel set (channel.home_id left out): nothing to switch back to ----

def test_without_a_home_channel_the_switch_back_is_skipped_and_the_tab_still_closes(tmp_path, monkeypatch, capsys):
    S.configure(dataclasses.replace(SETTINGS, home_channel_id=None))
    w = day_dir(tmp_path)
    item_in(w)
    h = HeldClose(w, monkeypatch)
    monkeypatch.setattr(S, "switch_back", lambda: pytest.fail("switched back with no home channel"))
    assert S.cmd_upload(w) == 0
    out = capsys.readouterr().out.splitlines()
    assert h.events == ["tab", "close"] and "CLOSED_TAB true" in out
    assert not [ln for ln in out if ln.startswith("SWITCHED_BACK")]
    assert "switched_back" not in S.receipt(w) and S.receipt(w)["stage"] == "private_verified"


def test_without_a_home_channel_no_tab_is_ever_sent_to_another_channel(monkeypatch, capsys):
    S.configure(dataclasses.replace(SETTINGS, home_channel_id=None))
    c = Chrome(monkeypatch, tabs=[OWN_TAB])
    _studio_tab_in(monkeypatch, c)
    S.switch_back_after(None, False)
    assert "CLOSED_TAB true" in capsys.readouterr().out
    assert [u for _, u in c.nav] == [DAILY_HOME] and S.switch_back() is False
