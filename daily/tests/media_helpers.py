"""Synthetic fixtures shared by the media tests (voice, compose, mix, render): a day's ledger, three scripts, the voices,
the settings, and helpers that lay them out as a work directory the real gate has accepted, a stub media kit, a sound
kit of tones, and a work dir voiced in dry mode. Every name, sentence and number here is invented.

The ledger is built by the real ledger builder (daily.ledger.build_ledger) from facts stated as the collect stage's
readers would hand them over, so it always has the text half's real shape. It holds a Codex transcript quote and a
PR-description quote, and the three scripts differ only in their confessional: `transcript` quotes the first, `pr` the
second, `none` has no confessional beat (quote_id and host_intro both ""). A quote id hashes its source path, so the
ledger keeps its sources under NOMINAL_WORK and `work_dir` moves them under the real work directory and re-hashes the
ids."""
from __future__ import annotations

import copy
import datetime as dt
import functools
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from daily import files, gate
from daily.config import load_config
from daily.ledger import RawSources, build_ledger, exclude_fragments, quote_id
from daily.settings import load_settings
from daily.sources.claude import ClaudeDay
from daily.sources.codex import CodexDay
from daily.sources.github import PR
from daily.sources.tracker import ActiveArc, ArcClose
from daily.sources.transcripts import HarnessDay, QuoteCandidate, SessionNote, digest_fragments
from daily.stage import Ctx
from daily_helpers import write_jsonl

DAY = dt.date(2026, 10, 16)
CASES = ("transcript", "pr", "none")
NOMINAL_WORK = "/work/2026-10-16"
OWNER = "demo-owner"
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg and ffprobe are not installed")

LANES = {
    "lanes": {
        "recipe-box": {"disclosure": "public", "label": "the recipe app", "why": "so dinner scales itself"},
        "side-quest": {"disclosure": "headline", "label": "a private side project", "why": "so weekends stay fun"},
        "old-diary": {"disclosure": "public", "label": "a diary", "why": "listed by mistake"},
    },
    "hard_exclude": ["old-diary", "scratch-dir"],
    "blocked_names": ["Quokka"],
}
EMBARGO = {"active": True, "terms": ["moon launch"], "_note": "test"}

HOST_REF_BYTES = b"a synthetic reference recording"
P = {"exaggeration": 0.5, "cfg_weight": 0.5, "temperature": 0.8}
VOICES = {
    "schema": "daily-build/voices/v1",
    "voices": {
        "host": {"display": "Sam", "ref": "~/no-such-folder/host-reference.wav",
                 "sha256_prefix": hashlib.sha256(HOST_REF_BYTES).hexdigest()[:16],
                 "params": {"exaggeration": 0.55, "cfg_weight": 0.45, "temperature": 0.8},
                 "params_cold_open": {"exaggeration": 0.7, "cfg_weight": 0.3, "temperature": 0.8}, "post": ""},
        "claude_code": {"display": "Claude Code", "glyph": "CC", "color": "#F2A65A", "active": True, "params": dict(P),
                        "post": "asetrate=24000*1.06,aresample=24000,atempo=0.8774,equalizer=f=3200:t=q:w=1:g=3"},
        "codex": {"display": "Codex", "glyph": "CX", "color": "#5CC8FF", "active": True,
                  "params": {"exaggeration": 0.4, "cfg_weight": 0.5, "temperature": 0.7},
                  "post": "asetrate=24000*0.9,aresample=24000,atempo=1.0333,highpass=f=100,lowpass=f=7500"},
        "my_agent": {"display": "My Agent", "glyph": "MA", "color": "#C9A7FF", "active": True,
                     "params": {"exaggeration": 0.5, "cfg_weight": 0.5, "temperature": 0.8},
                     "post": "asetrate=24000*0.95,aresample=24000,atempo=0.9789,bass=g=5,aecho=0.8:0.9:40:0.15"},
        "night_bot": {"display": "Night Bot", "glyph": "NB", "color": "#9BE38B", "active": True,
                      "params": {"exaggeration": 0.6, "cfg_weight": 0.4, "temperature": 0.8},
                      "post": "asetrate=24000*0.85,aresample=24000,atempo=1.0941,highpass=f=100"},
    },
}
AGENTS = ("claude_code", "codex", "my_agent", "night_bot")  # the agent voices above, in file order

DISCLOSURE = "Narrated with an AI voice; agent lines are verbatim from logs."
SERIES = "Demo Agents on the Clock"
HANDLE = "@DemoChannel"
SETTINGS = f'''timezone = "America/Los_Angeles"

[show]
series = "{SERIES}"
host_name = "Sam"
disclosure = "{DISCLOSURE}"
handle = "{HANDLE}"

[sources]
tracker = "/no-such-folder/tracker.py"

[[sources.adapters]]
harness = "my_agent"
display = "My Agent"
module = "/no-such-folder/my_agent.py"

[[sources.adapters]]
harness = "night_bot"
display = "Night Bot"
module = "/no-such-folder/night_bot.py"

[github]
owner = "{OWNER}"
'''

# ---- the scripts ----

_BASE = {
    "schema": "daily-build/script/v1", "day": "2026-10-16", "quiet": False, "quiet_reason": "",
    "story_item_id": "pr:recipe-box#21",
    "title": "The agent that refused to ship its own feature",
    "description": "Yesterday's build, in about a minute. " + DISCLOSURE,
    "cold_open": {"host": "Yesterday an agent refused to ship its own feature, and it was right to.",
                  "on_screen": "IT REFUSED TO SHIP"},
    "scoreboard": {"host": "Now the scoreboard. Sixteen pull requests merged, six tasks closed, and two kinds of "
                           "agent did most of it."},
    "story": {
        "host": [
            "The big one is the recipe app, which can now scale any recipe up or down for the number of people at "
            "the table. It is the kind of chore I used to do on a napkin, and now it happens while I cook.",
            "You type how many people are coming, and it rewrites every amount, converts the awkward units, and "
            "rounds the results so they still make sense in a real kitchen. Then it checks its own math against the "
            "original before it shows you anything.",
            "Yesterday it learned to stop and refuse when a scaled amount would come out as a fraction you cannot "
            "measure.",
        ],
        "on_screen": ["ANY RECIPE, ANY CROWD", "IT CHECKS ITS OWN MATH", "IT KNOWS WHEN TO STOP"],
    },
    "steal_this": {"host": "Here is one to steal. Have your agent check its finished work against the plan, so a gap "
                           "fails loudly. Write the plan first, then ask the agent to compare what it made with what "
                           "you asked for.",
                   "on_screen": "MAKE THE AGENT CHECK ITS OWN OUTPUT"},
    "confessional": {"host_intro": "", "quote_id": ""},
    "out": {"host": "Tomorrow the recipe app learns to plan a whole week. Subscribe to hear how it goes.",
            "teaser": "A WHOLE WEEK, TOMORROW"},
}
HEARD = "The scaling check caught an amount that rounded to zero, so I refused to ship on purpose."
PR_LINE = "The check compares every scaled amount with the original and fails the build when one rounds to zero."
BODY = "Adds recipe scaling.\n" + PR_LINE
INTROS = {"transcript": "And Codex had thoughts about that.", "pr": "And its own pull request agreed."}


# ---- the ledger, from the facts ----

def _pr(repo, n, title, body, ts, harness):
    return PR(f"{OWNER}/{repo}", n, title, body, f"https://github.com/{OWNER}/{repo}/pull/{n}", ts, harness)


def raw_facts(cfg) -> RawSources:
    """The day as the collect stage's readers hand it over: one public PR by Codex with a quotable body, two PRs in a
    headline lane, thirteen in an unlisted repo, six closed tasks, one still-moving task, a Codex rollout sentence, a
    counts-only adapter that read and one that failed."""
    prs = [_pr("recipe-box", 21, "Add recipe scaling and a rounding check", BODY, "2026-10-16T21:03:00Z", "codex"),
           _pr("side-quest", 4, "Check the first restore", "", "2026-10-16T19:10:00Z", "claude_code"),
           _pr("side-quest", 5, "Check the second restore", "", "2026-10-16T20:20:00Z", "claude_code")]
    prs += [_pr("draft-pile", n, "Tidy up", "", "2026-10-16T18:00:00Z", "unknown") for n in range(1, 14)]
    arcs = [ArcClose(f"tidy-{n}", "Tidy up", f"{OWNER}/draft-pile", "codex", "2026-10-16T20:00:00+00:00")
            for n in range(6)]
    heard = QuoteCandidate(harness="codex", repo=None, cwd="/w/recipe-box", text=HEARD, ts="2026-10-16T21:14:05Z",
                           path=f"{NOMINAL_WORK}/sources/rollout-2026-10-16.jsonl", line=3, record="msg_1",
                           session_repos=("recipe-box",), session_cwds=("/w/recipe-box",), own_repo="recipe-box")
    note = SessionNote("codex", "0f3a9c21", "Recipe scaling and rounding check", ("recipe-box",), ("/w/recipe-box",),
                       False, "2026-10-16T21:14:05.000Z")
    digest = digest_fragments(exclude_fragments(cfg))
    return RawSources(
        prs=tuple(prs), arcs=tuple(arcs), claude=ClaudeDay(11, 92, ()),
        codex=CodexDay(2, 3, (heard,), notes=(note,), fragments_checked=2, fragments_digest=digest),
        active=(ActiveArc("week-plan", "Plan a whole week of dinners", f"{OWNER}/recipe-box", "codex",
                          "2026-10-16T22:00:00+00:00"),),
        others={"my_agent": HarnessDay(1, 0, ())},
        coverage={"claude_code": "ok", "codex": "ok", "my_agent": "ok", "night_bot": "unavailable:adapter_unavailable"})


def config_in(d: Path):
    d.mkdir(parents=True, exist_ok=True)
    (d / "lanes.json").write_text(json.dumps(LANES))
    (d / "embargo.json").write_text(json.dumps(EMBARGO))
    return load_config(d)


@functools.lru_cache(maxsize=1)
def _ledger_and_records() -> tuple:
    from daily import day
    saved = day.zone_name()
    day.use_timezone("America/Los_Angeles")  # the ledger records its zone: pin it, whatever zone a test left active
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config_in(Path(tmp) / "cfg")
            led, records = build_ledger(DAY, raw_facts(cfg), cfg, "2026-10-17T12:30:00Z",
                                        f"{NOMINAL_WORK}/sources/github.jsonl")
    finally:
        day.use_timezone(saved)
    return json.loads(json.dumps(led)), json.loads(json.dumps(records))


def ledger() -> dict:
    """A fresh copy of the day's ledger (quote sources under NOMINAL_WORK)."""
    return copy.deepcopy(_ledger_and_records()[0])


def pr_records() -> list:
    return copy.deepcopy(_ledger_and_records()[1])


def quote_of_kind(led: dict, kind: str) -> dict:
    return next(q for q in led["quotes"] if q["kind"] == kind)


def script(case: str = "transcript") -> dict:
    """The case's script, its quote id taken from the nominal ledger."""
    s = copy.deepcopy(_BASE)
    if case != "none":
        s["confessional"] = {"host_intro": INTROS[case], "quote_id": quote_of_kind(ledger(), case)["id"]}
    return s


def voices() -> dict:
    """The parsed voices (voiceconf.parse_voices_bytes of the VOICES file)."""
    from daily import voiceconf
    return voiceconf.parse_voices_bytes(json.dumps(VOICES).encode("utf-8"))


# ---- a config folder and a work directory ----

def make_config(root: Path, *, kit: Path | None = None, extra: str = "", voices_json=None) -> Path:
    """A config folder under root: settings.toml (plus `[render] kit` when kit is given, plus `extra`), voices.json,
    lanes.json and embargo.json. Returns the folder."""
    d = Path(root) / "media-cfg"
    config_in(d)
    text = SETTINGS + (f'\n[render]\nkit = "{kit}"\n' if kit is not None else "") + extra
    (d / "settings.toml").write_text(text)
    (d / "voices.json").write_text(json.dumps(VOICES if voices_json is None else voices_json))
    return d


def use_config(monkeypatch, root: Path, **kw):
    """make_config, pointed at by ORION_CLOCK_CONFIG; returns (folder, Settings, Config)."""
    d = make_config(root, **kw)
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(d))
    return d, load_settings(d), load_config(d)


def settings_for(root: Path, **kw):
    d = make_config(root, **kw)
    return load_settings(d)


def _rebase(led: dict, work: Path) -> tuple:
    """(the ledger with every quote source under `work` and each quote re-hashed, old quote id -> new quote id)."""
    led = copy.deepcopy(led)
    ids = {}
    for q in led["quotes"]:
        src = q["source"]
        assert src["path"].startswith(NOMINAL_WORK + "/"), f"a fixture quote source is not under {NOMINAL_WORK}"
        src["path"] = str(work) + src["path"][len(NOMINAL_WORK):]
        new = quote_id(q["harness"], q["text"], src["path"], src["line"])
        ids[q["id"]], q["id"] = new, new
    return led, ids


def _write_sources(led: dict, work: Path) -> None:
    """The files the quotes point at: github.jsonl as the collect stage writes it, and a Codex rollout whose assistant
    message on the quote's line holds the quote inside a longer reply."""
    files.write_text(work / "sources" / "github.jsonl",
                     "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in pr_records()))
    for q in led["quotes"]:
        src = q["source"]
        if q["kind"] != "transcript":
            continue
        assert src["line"] == 3, "the rollout below puts the quote on line 3"
        write_jsonl(Path(src["path"]), [
            {"timestamp": "2026-10-16T21:10:00Z", "type": "session_meta",
             "payload": {"id": "th-0f3a9c21", "cwd": "/w/recipe-box"}},
            {"timestamp": "2026-10-16T21:11:00Z", "type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Does the scaling check catch an amount of zero?"}]}},
            {"timestamp": q["ts"], "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "id": src["record"],
                "content": [{"type": "output_text",
                             "text": "I ran it on a tiny batch. " + q["text"] + " Now the unit tests."}]}},
        ])


# The scanners are stubbed clean (test_gate covers them, and a test runs the real ones); every other check reads the
# files: shape, ledger ids, the quote's source line, names, embargo, budget, code shapes and numbers.
CLEAN = gate.Deps(lambda t: ("PUBLISH", []), lambda t: [], lambda t: [])


def gate_work(work: Path, dry: bool = False, settings=None):
    """The real text gate stage on a work directory (script.json, ledger.json, sources/): writes verdict.json, bound to
    the ledger, and returns the StageResult."""
    root = Path(work).parent / "gate-cfg"
    s = settings if settings is not None else settings_for(root)
    return gate.run_stage(Ctx(DAY, work, config_in(root / "media-cfg"), dry, s), deps=CLEAN)


def work_dir(tmp_path: Path, case: str = "transcript", *, gated: bool = True, dry: bool = False) -> Path:
    """A work directory for 2026-10-16 holding the case's script, the ledger, the quote sources and (gated, the default)
    a verdict the real gate wrote, so `gate.accepts(verdict, script, gate.REQUIRED, ledger_path=ledger)` is (True, "ok").
    gated=False leaves out verdict.json; dry marks the verdict dry."""
    w = Path(tmp_path) / DAY.isoformat()
    w.mkdir(parents=True, exist_ok=True)
    led, ids = _rebase(ledger(), w)
    s = script(case)
    qid = s["confessional"]["quote_id"]
    s["confessional"]["quote_id"] = ids.get(qid, qid)  # "" stays ""
    files.write_json(w / "ledger.json", led)
    files.write_json(w / "script.json", s)
    _write_sources(led, w)
    if gated:
        res = gate_work(w, dry)
        if not res.ok:
            raise AssertionError(f"the gate rejects the {case} fixture: {res.reason}")
    return w


def ctx(w: Path, dry: bool = False, settings=None) -> Ctx:
    """A stage context for the work dir with media settings (a stub kit beside it unless settings are given)."""
    if settings is None:
        settings = settings_for(Path(w).parent / "ctx-cfg", kit=make_kit(Path(w).parent))
    return Ctx(DAY, w, None, dry, settings)


# ---- media ----

def tone(path: Path, seconds: float, freq: int = 440, sr: int = 48000, ch: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=f={freq}:d={seconds}:r={sr}", "-ac", str(ch), str(path)], check=True, timeout=60)
    return path


def make_kit(tmp_path: Path) -> Path:
    """Fonts and GSAP stubs: enough for compose.build, not for a real render."""
    kit = Path(tmp_path) / "kit"
    (kit / "fonts").mkdir(parents=True, exist_ok=True)
    (kit / "gsap.min.js").write_text("/* gsap stub */")
    for f in ("Anton.ttf", "InterVariable.ttf"):
        (kit / "fonts" / f).write_bytes(b"font")
    return kit


STINGS = ("claude_code", "codex", "my_agent")  # agents the sound kit has a sting for (night_bot has none)


def kit_sounds() -> list:
    """Every kit sound the sound kit holds: the beds, the cues and the stings."""
    from daily import mix
    return sorted({r for r, _g in mix.BEDS.values()} | {r for r, _g in mix.CUES.values()}
                  | {mix.entrant_rel(h) for h in STINGS})


def kit_freq(rel: str) -> int:
    """The test tone of one kit sound, distinct per file (the beds, every cue and every sting), so a test can tell which
    sound landed where. Voice tones in the mix tests use VOICE_HZ, below all of these."""
    return 400 + 100 * kit_sounds().index(rel)


VOICE_HZ = 100


def make_sound_kit(tmp_path: Path) -> Path:
    """The stub kit plus every kit sound, each its own tone (kit_freq)."""
    kit = make_kit(tmp_path)
    for rel in kit_sounds():
        tone(kit / rel, 3.0 if rel in ("sound/theme.wav",) else 2.0 if rel.startswith("music/") else 0.3, kit_freq(rel))
    return kit


def dry_work(tmp_path: Path, case: str = "transcript", monkeypatch=None) -> Path:
    """A gated work dir whose lines are voiced by the real voice stage in dry mode: lines.json, hostlint.json, silence of
    the right length and word times for every line. The config folder holding voices.json is put in place for the
    stage's one read and taken away again."""
    from daily import voice
    import os
    w = work_dir(tmp_path, case)
    d = make_config(Path(tmp_path) / "dry-cfg")
    saved = os.environ.get("ORION_CLOCK_CONFIG")
    os.environ["ORION_CLOCK_CONFIG"] = str(d)
    try:
        res = voice.run_stage(Ctx(DAY, w, None, True, load_settings(d)))
    finally:
        if saved is None:
            os.environ.pop("ORION_CLOCK_CONFIG", None)
        else:
            os.environ["ORION_CLOCK_CONFIG"] = saved
    assert res.ok, res.reason
    return w
