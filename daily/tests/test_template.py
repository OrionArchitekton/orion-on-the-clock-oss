"""The HyperFrames composition: the template's slots and sinks, what compose.build fills into it, where every piece of
on-screen text comes from, and (with node) what the page builds when its script runs against a small DOM stand-in.
The browser tests need playwright and a real media kit (ORION_CLOCK_TEST_KIT names one) and are skipped otherwise."""
import json
import os
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from daily import compose, gate
from media_helpers import CASES, HANDLE, HAS_FFMPEG, SERIES, dry_work, make_kit, voices

DAY = date(2026, 10, 16)
HAS_NODE = shutil.which("node") is not None
KIT = Path(os.environ["ORION_CLOCK_TEST_KIT"]) if os.environ.get("ORION_CLOCK_TEST_KIT") else None
LABELS = {"transcript": "VERBATIM FROM CODEX LOG, 2:14 PM PDT", "pr": "FROM CODEX'S PR DESCRIPTION, 2:03 PM PDT",
          "none": None}
HOSTILE = ["<img src=x onerror=window.PWN=1>", "</script><script>window.PWN=1</script>", "<!--", "back\\slash",
           "ls" + chr(0x2028) + "x", "&amp;", "<b>x</b>", "<!-- <script "]
HOSTILE_TEXT = " ".join(HOSTILE)
LONG_DASHES = "".join(chr(c) for c in (0x2013, 0x2014, 0x2015))  # built from code points: no dash in this file


@pytest.fixture(scope="module")
def gold(tmp_path_factory):
    """Each case gated and dry-voiced once; tests copy the directory."""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg and ffprobe are not installed (the dry voice stage writes silence with ffmpeg)")
    return {c: dry_work(tmp_path_factory.mktemp(c), c) for c in CASES}


@pytest.fixture
def clone(gold, tmp_path):
    def make(case="transcript"):
        w = tmp_path / case / DAY.isoformat()
        shutil.copytree(gold[case], w)
        return w, json.loads((w / "script.json").read_text()), json.loads((w / "ledger.json").read_text())
    return make


def comp_html(w):
    return (w / "build" / "comps" / "daily" / "index.html").read_text(encoding="utf-8")


def js_of(html):
    return html.rsplit("<script>", 1)[1].split("</script>", 1)[0]


def build(tmp_path, w, s, led, dry=True, kit=None, handle=HANDLE, v=None):
    return compose.build(w, DAY, s, led, dry=dry, kit=kit or make_kit(tmp_path), voices=v or voices(), series=SERIES,
                         handle=handle)


def spec_of(s, led, tl, handle=HANDLE, v=None):
    return compose.build_spec(s, led, v or voices(), tl, DAY, series=SERIES, handle=handle)


def revoice(w, line_id, text):
    """Change a voiced line's text (and its dry word times) in place, as a re-voice would."""
    d = w / "voice"
    lines = json.loads((d / "lines.json").read_text())
    for ln in lines:
        if ln["id"] == line_id:
            ln["text"] = text
    (d / "lines.json").write_text(json.dumps(lines))
    toks = text.split()
    p = d / f"{line_id}.words.json"
    rec = json.loads(p.read_text())
    rec.update(text=text, dur=round(max(0.6, 0.4 * len(toks)), 3),
               words=[{"word": t, "start": round(i * 0.4, 3), "end": round((i + 1) * 0.4, 3)}
                      for i, t in enumerate(toks)])
    p.write_text(json.dumps(rec))


def set_text(w, s, led, texts):
    """Put `texts` (a dict of field -> text) on every allowed on-screen field, voiced lines included."""
    s["cold_open"]["on_screen"] = texts["cold_open"]
    s["story"]["on_screen"] = [texts["story"]] * len(s["story"]["on_screen"])
    s["steal_this"]["on_screen"] = texts["steal"]
    s["out"]["teaser"] = texts["teaser"]
    revoice(w, "cold_open", texts["cold_open"])
    revoice(w, "steal_this", texts["steal"])
    if s["confessional"]["quote_id"]:
        q = compose.quote_by_id(led, s["confessional"]["quote_id"])
        q["text"] = texts["quote"]
        revoice(w, "confessional", texts["quote"].strip())


def test_template_has_every_placeholder_and_no_long_dash():
    t = compose.TEMPLATE.read_text(encoding="utf-8")
    for k in ("__CLIPS__", "__SPEC__", "__TIMING__", "__CAPTIONS__", "{{total}}"):
        assert k in t
    for sc in compose.SCENES:
        assert f"C.{sc} = " in t
    assert "NO DATA TODAY" in t and 'r.unit !== "sessions"' in t
    assert "STEAL THIS" in t and "esc(SPEC.brand)" in t and "ON THE CLOCK" not in t  # the brand comes from settings
    assert "overflow-wrap: anywhere" in t
    assert not any(d in t for d in compose.LONG_DASHES)


def test_template_reads_confessional_cues_only_under_a_guard():
    t = compose.TEMPLATE.read_text(encoding="utf-8")
    body = t.split("C.confessional = ", 1)[1].split("C.out = ", 1)[0]
    assert "SPEC.cues.harness_in" in body and "SPEC.cues.quote_in" in body
    loop = t.split("for (const id of ORDER)", 1)[1]
    assert "!SPEC.confessional" in loop


def test_template_leaves_out_an_unknown_total_and_an_absent_handle():
    t = compose.TEMPLATE.read_text(encoding="utf-8")
    tiles = t.split('[[S.prs, "PRS MERGED"]', 1)[1].split(".forEach(", 1)[0]
    assert ".filter(([n]) => n !== null && n !== undefined)" in tiles  # before the tiles are drawn
    out = t.split("C.out = ", 1)[1]
    assert 'if (SPEC.channel) up(mk("div", "ou-h", esc(SPEC.channel)' in out


def _args(src: str, start: int) -> list[str]:
    """The top-level arguments of the call whose "(" is at src[start]."""
    args, depth, quote, cur, i = [], 0, None, "", start
    while i < len(src):
        c = src[i]
        if quote:
            cur += c
            if c == "\\":
                i += 1
                cur += src[i]
            elif c == quote:
                quote = None
        elif c in "\"'`":
            quote, cur = c, cur + c
        elif c in "([{":
            depth += 1
            if depth > 1:
                cur += c
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                args.append(cur.strip())
                return args
            cur += c
        elif c == "," and depth == 1:
            args.append(cur.strip())
            cur = ""
        else:
            cur += c
        i += 1
    raise AssertionError("unbalanced call")


def _strip_calls(expr: str, name: str) -> str:
    while True:
        m = re.search(rf"\b{name}\(", expr)
        if not m:
            return expr
        end = m.end() - 1
        depth, quote, i = 0, None, end
        while True:
            c = expr[i]
            if quote:
                if c == "\\":
                    i += 1
                elif c == quote:
                    quote = None
            elif c in "\"'":
                quote = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        expr = expr[:m.start()] + "0" + expr[i + 1:]


def test_every_dom_sink_fed_from_spec_goes_through_esc_or_textcontent():
    t = js_of(compose.TEMPLATE.read_text(encoding="utf-8").replace("__SPEC__", "{}").replace("__TIMING__", "{}")
              .replace("__CAPTIONS__", "[]").replace("{{total}}", "0").replace("__CLIPS__", ""))
    assert not re.search(r"insertAdjacentHTML|document\.write|outerHTML|\.innerHTML\s*[+]?=",
                         t.split("const mk", 1)[1].split("\n", 1)[1])
    calls = [m.end() - 1 for m in re.finditer(r"\bmk\(", t)]
    assert len(calls) >= 25
    for pos in calls:
        args = _args(t, pos)
        if len(args) < 3:
            continue
        html = _strip_calls(args[2], "esc")
        html = re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', '""', html)  # string literals
        html = html.replace("(w, i) =>", "")  # the caption map's parameters; a bare use of w below is a raw sink
        left = set(re.findall(r"[A-Za-z_][A-Za-z_0-9.]*", html))
        assert left <= {"null", "k", "i", "e.active", "e.words.map", "join", "String", "Math", "round", "o", "v"}, (
            args[2], left)
    # the helper really rejects a bare sink: a mutant with the esc() removed fails the same check
    bad = _strip_calls('mk("div", "co-w", SPEC.cold_open.text, root)', "esc")
    assert "SPEC.cold_open.text" in bad


# ---- the scoreboard rows ----

def extended_voices(*extra):
    """The fixture voices plus more agents (id, display), each with its own glyph and color."""
    v = voices()
    for n, (h, display) in enumerate(extra):
        v[h] = dict(v["codex"], display=display, glyph=f"X{n}", color=f"#1020{30 + n:02d}")
    return v


def six_agent_ledger(led):
    led["scoreboard"]["sessions"] = {"claude_code": 11, "codex": 2, "my_agent": 1, "agent_four": 3, "agent_five": 4}
    led["coverage"] = {"claude_code": "ok", "codex": "ok", "my_agent": "ok", "agent_four": "ok", "agent_five": "ok",
                       "night_bot": "unavailable:adapter_unavailable"}
    return led


SIX = (("agent_four", "Agent Four"), ("agent_five", "Agent Five"))


def fixture_ledger():
    from media_helpers import ledger
    return ledger()


def test_the_fixture_ledger_shows_every_agent_in_coverage_order_and_the_failed_one_as_no_data():
    rows = compose.scoreboard_rows(fixture_ledger(), voices())
    assert [r["harness"] for r in rows] == ["claude_code", "codex", "my_agent", "night_bot"]
    assert [r["n"] for r in rows] == [11, 2, 1, None]
    assert [r["unit"] for r in rows] == ["sessions", "sessions", "sessions", None]
    assert rows[3] == {"harness": "night_bot", "display": "Night Bot", "glyph": "NB", "color": "#9BE38B", "n": None,
                       "unit": None, "available": False}


def test_six_rows_fit_and_a_seventh_is_refused():
    led = six_agent_ledger(fixture_ledger())
    rows = compose.scoreboard_rows(led, extended_voices(*SIX))
    assert [r["harness"] for r in rows] == list(led["coverage"]) and len(rows) == compose.MAX_ROWS == 6
    assert [r["n"] for r in rows[:5]] == [11, 2, 1, 3, 4] and not rows[5]["available"]
    led["coverage"]["agent_six"] = "ok"
    led["scoreboard"]["sessions"]["agent_six"] = 0
    with pytest.raises(ValueError, match="at most 6"):
        compose.scoreboard_rows(led, extended_voices(*SIX, ("agent_six", "Agent Six")))


def test_an_agent_in_the_coverage_without_a_voice_is_refused():
    led = fixture_ledger()
    v = voices()
    del v["my_agent"]
    with pytest.raises(ValueError, match="no voice"):
        compose.scoreboard_rows(led, v)
    led["coverage"]["host"] = "ok"  # the narrator is never an agent row
    with pytest.raises(ValueError, match="no voice"):
        compose.scoreboard_rows(led, voices())


def test_unavailable_never_shows_a_count_and_a_day_needs_a_row():
    led = fixture_ledger()
    led["scoreboard"]["sessions"]["night_bot"] = 7  # a count beside an unavailable coverage is never drawn
    assert compose.scoreboard_rows(led, voices())[3]["n"] is None
    led["scoreboard"]["sessions"], led["coverage"] = {}, {}
    with pytest.raises(ValueError, match="no harness rows"):
        compose.scoreboard_rows(led, voices())


@pytest.mark.parametrize("bad", [True, 2.5, "3", None])
def test_counts_must_be_ints(bad):
    led = fixture_ledger()
    led["scoreboard"]["sessions"]["codex"] = bad
    with pytest.raises(ValueError, match="count"):
        compose.scoreboard_rows(led, voices())


# ---- compose.build ----

@pytest.mark.parametrize("case", CASES)
def test_build_fills_the_composition(tmp_path, clone, case):
    w, s, led = clone(case)
    tl = build(tmp_path, w, s, led)
    html = comp_html(w)
    assert "__SPEC__" not in html and "{{" not in html and "__CLIPS__" not in html
    # dry voicing is 0.3 s a word (a cloned voice's pace), at least 0.6 s a line; at the 165 to 210 word budget no
    # case needs padding to reach 60 s
    assert (tl["total"], tl["padded_per_scene"]) == {"transcript": (73.25, 0.0), "pr": (73.55, 0.0),
                                                      "none": (65.15, 0.0)}[case]
    assert tl["captions"] and tl["caption_groups"]
    assert (w / "build" / "comps" / "daily" / "assets" / "fonts" / "Anton.ttf").exists()
    assert (w / "build" / "comps" / "daily" / "assets" / "gsap.min.js").read_text() == "/* gsap stub */"
    saved = json.loads((w / "build" / "timeline.json").read_text())
    assert saved["total"] == tl["total"]
    for sc in compose.SCENES:
        assert (f'id="sc-{sc}"' in html) == (sc in tl["scenes"])
    if case == "none":
        assert "confessional" not in tl["scenes"] and saved["confessional_harness"] is None
        assert '"confessional": null' in html
    else:
        assert saved["confessional_harness"] == "codex" and '"display": "Codex"' in html
        assert LABELS[case] in html


def test_the_quote_label_uses_the_settings_zone(tmp_path, clone):
    from daily import day
    w, s, led = clone()
    tl = build(tmp_path, w, s, led)
    day.use_timezone("Europe/Berlin")  # the conftest puts the zone back after the test
    assert spec_of(s, led, tl)["confessional"]["label"] == "VERBATIM FROM CODEX LOG, 11:14 PM CEST"


def test_the_brand_and_the_handle_come_from_settings(tmp_path, clone):
    w, s, led = clone()
    build(tmp_path, w, s, led)
    spec = extract(comp_html(w), "SPEC")
    assert spec["brand"] == SERIES.upper() and spec["channel"] == HANDLE
    build(tmp_path, w, s, led, handle=None)
    spec = extract(comp_html(w), "SPEC")
    assert spec["channel"] is None and HANDLE not in comp_html(w)


@pytest.mark.parametrize("field", ["prs_merged", "arcs_closed"])
def test_an_unknown_total_is_null_never_a_zero(tmp_path, clone, field):
    w, s, led = clone()
    led["scoreboard"][field] = None
    build(tmp_path, w, s, led)
    spec = extract(comp_html(w), "SPEC")
    key = {"prs_merged": "prs", "arcs_closed": "arcs"}[field]
    assert spec["scoreboard"][key] is None
    other = {"prs": "arcs", "arcs": "prs"}[key]
    assert isinstance(spec["scoreboard"][other], int)  # positive control: the other total is still drawn
    del led["scoreboard"][field]  # a ledger without the key at all reads the same
    build(tmp_path, w, s, led)
    assert extract(comp_html(w), "SPEC")["scoreboard"][key] is None


def test_unknown_quote_kind_is_an_error(tmp_path, clone):
    w, s, led = clone()
    compose.quote_by_id(led, s["confessional"]["quote_id"])["kind"] = "tweet"
    with pytest.raises(ValueError, match="kind"):
        build(tmp_path, w, s, led)


@pytest.mark.skipif(not HAS_NODE, reason="node not installed")
@pytest.mark.parametrize("case", CASES)
def test_filled_script_is_valid_javascript(tmp_path, clone, case):
    w, s, led = clone(case)
    build(tmp_path, w, s, led)
    f = tmp_path / "main.js"
    f.write_text(js_of(comp_html(w)))
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


# A small DOM and GSAP stand-in: enough for the page's script to run in node and build its elements, so a test can
# read what it drew without a browser. Layout is not modelled (every offset is 0, so the fit loops stop at once).
DOM_HARNESS = r'''
const fs = require("fs"), vm = require("vm");
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ""; this.html = null; this.attrs = {};
    this.textContent = ""; this.offsetTop = 0; this.offsetHeight = 0; this.style = { setProperty() {} }; }
  set innerHTML(v) { this.html = v; } get innerHTML() { return this.html; }
  appendChild(c) { this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = v; }
  querySelectorAll(sel) { const cls = sel.replace(/^\./, ""), out = [];
    const walk = (e) => { for (const c of e.children) { if (c.className.split(" ").includes(cls)) out.push(c); walk(c); } };
    walk(this); return out; }
}
const all = [], byId = {};
const document = { createElement(t) { const e = new El(t); all.push(e); return e; },
  getElementById(id) { if (!byId[id]) { byId[id] = new El("div"); byId[id].id = id; } return byId[id]; } };
const chain = { fromTo() { return chain; }, to() { return chain; }, set() { return chain; } };
const window = {};
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), { document, window, gsap: { timeline: () => chain } });
const of = (cls) => all.filter((e) => e.className.split(" ").includes(cls)).map((e) => e.html);
process.stdout.write(JSON.stringify({ tiles: of("sb-l"), rows: of("sb-r").length, nodata: of("na"), brand: of("brand"),
  handle: of("ou-h"), day: of("day"), quote: of("cf-q"), timeline: Boolean(window.__timelines && window.__timelines.main) }));
'''


def run_page(tmp_path, w):
    js = tmp_path / "page.js"
    js.write_text(js_of(comp_html(w)), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(DOM_HARNESS)
    r = subprocess.run(["node", str(harness), str(js)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout)


@pytest.mark.skipif(not HAS_NODE, reason="node not installed")
def test_the_page_draws_four_totals_the_rows_the_brand_and_the_handle(tmp_path, clone):
    w, s, led = clone()
    build(tmp_path, w, s, led)
    got = run_page(tmp_path, w)
    assert got["timeline"] is True
    assert got["tiles"] == ["PRS MERGED", "TASKS CLOSED", "AGENT SESSIONS", "HELPER AGENTS"]
    assert got["rows"] == 4 and got["nodata"] == ["NO DATA TODAY"]
    assert got["brand"] == [SERIES.upper()] and got["handle"] == [HANDLE] and got["day"] == ["OCT 16"]


@pytest.mark.skipif(not HAS_NODE, reason="node not installed")
def test_the_page_leaves_out_unknown_totals_and_an_absent_handle(tmp_path, clone):
    w, s, led = clone()
    led["scoreboard"]["prs_merged"] = None
    led["scoreboard"]["arcs_closed"] = None
    build(tmp_path, w, s, led, handle=None)
    got = run_page(tmp_path, w)
    assert got["tiles"] == ["AGENT SESSIONS", "HELPER AGENTS"]  # never a zero for a count nobody took
    assert got["handle"] == [] and got["timeline"] is True


@pytest.mark.skipif(not HAS_NODE, reason="node not installed")
def test_the_page_escapes_hostile_text_into_its_elements(tmp_path, clone):
    w, s, led = clone()
    set_text(w, s, led, {k: "<img src=x onerror=1>" for k in ("cold_open", "story", "steal", "teaser", "quote")})
    build(tmp_path, w, s, led)
    got = run_page(tmp_path, w)
    assert got["quote"] and "<img" not in got["quote"][0] and "&lt;img src=x onerror=1&gt;" in got["quote"][0]


def test_a_long_dash_in_shipped_text_stops_the_build(tmp_path, clone):
    w, s, led = clone()
    s["out"]["teaser"] = "NEXT " + chr(0x2014) + " RESTORE"
    with pytest.raises(ValueError, match="long dash"):
        build(tmp_path, w, s, led)
    s["out"]["teaser"] = "FINE"
    s["steal_this"]["on_screen"] = "A" + chr(0x2013) + "B"
    with pytest.raises(ValueError, match="long dash"):
        build(tmp_path, w, s, led)


def test_a_long_dash_in_the_settings_text_stops_the_build(tmp_path, clone):
    w, s, led = clone()
    with pytest.raises(ValueError, match="long dash"):
        compose.build(w, DAY, s, led, dry=True, kit=make_kit(tmp_path), voices=voices(),
                      series="Demo " + chr(0x2015) + " Show", handle=HANDLE)
    v = voices()
    v["codex"]["display"] = "Co" + chr(0x2013) + "dex"
    with pytest.raises(ValueError, match="long dash"):
        build(tmp_path, w, s, led, v=v)


def test_a_long_dash_in_a_caption_word_stops_the_build(tmp_path, clone):
    w, s, led = clone()
    revoice(w, "cold_open", "Yesterday " + chr(0x2014) + " an agent refused")
    with pytest.raises(ValueError, match="long dash in captions"):
        build(tmp_path, w, s, led)


def test_a_failed_build_leaves_no_old_composition(tmp_path, clone):
    w, s, led = clone()
    build(tmp_path, w, s, led)
    assert (w / "build" / "timeline.json").exists()
    s["out"]["teaser"] = "X" + chr(0x2014)
    with pytest.raises(ValueError):
        build(tmp_path, w, s, led)
    assert not (w / "build" / "comps" / "daily" / "index.html").exists()
    assert not (w / "build" / "timeline.json").exists()


def test_build_never_opens_the_gated_files_or_voices(tmp_path, clone, monkeypatch):
    from daily import voiceconf
    w, s, led = clone()
    (w / "script.json").write_text("not json")
    (w / "ledger.json").write_text("not json")
    parsed = voices()  # the caller's one parse, before the readers are taken away
    for name in ("load_voices", "load_voices_hashed", "load_voices_raw", "parse_voices_bytes"):
        monkeypatch.setattr(voiceconf, name, lambda *a, **k: pytest.fail("compose opened voices.json"))
    build(tmp_path, w, s, led, v=parsed)


def test_lines_json_ids_drive_the_read_and_are_checked(tmp_path, clone):
    w, s, led = clone()
    p = w / "voice" / "scoreboard.words.json"
    good = p.read_text()
    rec = json.loads(good)
    rec["id"] = "other"
    p.write_text(json.dumps(rec))
    with pytest.raises(ValueError, match="id"):
        build(tmp_path, w, s, led)
    rec = json.loads(good)
    rec["text"] += " extra"
    p.write_text(json.dumps(rec))
    with pytest.raises(ValueError, match="text"):
        build(tmp_path, w, s, led)
    p.write_text(good)
    (w / "voice" / "stray.words.json").write_text("garbage")  # not in lines.json: never read
    build(tmp_path, w, s, led)


def test_a_line_id_outside_the_scenes_never_reaches_a_path(tmp_path, clone):
    w, s, led = clone()
    lines = json.loads((w / "voice" / "lines.json").read_text())
    lines[0]["id"] = "../../evil"
    (w / "voice" / "lines.json").write_text(json.dumps(lines))
    with pytest.raises(ValueError, match="unknown line id"):
        build(tmp_path, w, s, led)


def test_an_injected_caption_word_is_refused(tmp_path, clone):
    w, s, led = clone()
    p = w / "voice" / "out.words.json"
    rec = json.loads(p.read_text())
    rec["words"].insert(1, {"word": "INJECTED", "start": 0.1, "end": 0.2})
    p.write_text(json.dumps(rec))
    with pytest.raises(ValueError, match="caption words"):
        build(tmp_path, w, s, led)
    rec["words"].pop(1)
    rec["words"][0]["word"] = "Changed"
    p.write_text(json.dumps(rec))
    with pytest.raises(ValueError, match="caption words"):
        build(tmp_path, w, s, led)


def test_dry_words_are_refused_on_a_real_build_and_real_on_a_dry_one(tmp_path, clone):
    w, s, led = clone()
    with pytest.raises(ValueError, match="dry"):
        build(tmp_path, w, s, led, dry=False)
    p = w / "voice" / "out.words.json"
    rec = json.loads(p.read_text())
    rec["source"] = "whisper"
    p.write_text(json.dumps(rec))
    with pytest.raises(ValueError, match="dry"):
        build(tmp_path, w, s, led, dry=True)
    for ln in json.loads((w / "voice" / "lines.json").read_text()):
        q = w / "voice" / f"{ln['id']}.words.json"
        r = json.loads(q.read_text())
        r["source"] = "whisper"
        q.write_text(json.dumps(r))
    build(tmp_path, w, s, led, dry=False)


def test_build_carries_six_rows_into_the_composition(tmp_path, clone):
    w, s, led = clone()
    six_agent_ledger(led)
    tl = build(tmp_path, w, s, led, v=extended_voices(*SIX))
    html = comp_html(w)
    assert html.count('"available": false') == 1 and '"display": "Agent Five"' in html
    assert tl["total"] > 0


def test_more_than_four_story_cards_is_an_error(tmp_path, clone):
    w, s, led = clone()
    s["story"]["on_screen"] = ["A", "B", "C", "D", "E"]
    with pytest.raises(ValueError, match="at most 4"):
        build(tmp_path, w, s, led)


@pytest.mark.parametrize("field", ["prs_merged", "arcs_closed"])
@pytest.mark.parametrize("bad", [True, 2.5, "3"])
def test_top_level_counts_must_be_ints_or_null(tmp_path, clone, field, bad):
    w, s, led = clone()
    led["scoreboard"][field] = bad
    with pytest.raises(ValueError, match="count"):
        build(tmp_path, w, s, led)


@pytest.mark.parametrize("bad", [True, 2.5, None])
def test_subagent_counts_must_be_ints(tmp_path, clone, bad):
    w, s, led = clone()
    led["scoreboard"]["subagents"]["codex"] = bad
    with pytest.raises(ValueError, match="count"):
        build(tmp_path, w, s, led)


def test_confessional_scene_and_quote_must_agree(tmp_path, clone):
    w, s, led = clone("transcript")
    tl_with = build(tmp_path, w, s, led)
    w2, s2, led2 = clone("none")
    tl_none = build(tmp_path, w2, s2, led2)
    with pytest.raises(ValueError, match="disagree"):
        spec_of(s2, led2, tl_with)  # no quote, but a confessional scene
    with pytest.raises(ValueError, match="disagree"):
        spec_of(s, led, tl_none)  # a quote, but no confessional scene


def test_the_quote_is_stripped_like_its_voiced_line(tmp_path, clone):
    w, s, led = clone()
    tl = build(tmp_path, w, s, led)
    q = compose.quote_by_id(led, s["confessional"]["quote_id"])
    want = q["text"].strip()
    q["text"] = "  " + want + "\n"
    assert spec_of(s, led, tl)["confessional"]["text"] == want


def test_fill_is_a_single_pass(tmp_path, clone):
    w, s, led = clone()
    tl = build(tmp_path, w, s, led)
    spec = spec_of(s, led, tl)
    marker = "__SPEC__ __CAPTIONS__ __TIMING__ {{total}}"
    spec["out"]["teaser"] = marker
    html = compose.fill(compose.TEMPLATE.read_text(encoding="utf-8"), tl, spec, [])
    assert marker in html and html.count("const SPEC = ") == 1


def extract(html, name):
    m = re.search(rf"const {name} = (.*?);\n", html)
    return json.loads(m.group(1))


def values(x, path=()):
    if isinstance(x, str):
        yield path, x
    elif isinstance(x, dict):
        for k, v in x.items():
            yield from values(v, path + (k,))
    elif isinstance(x, list):
        for i, v in enumerate(x):
            yield from values(v, path + (i,))


@pytest.mark.parametrize("case", CASES)
def test_the_page_draws_each_field_from_its_own_source(tmp_path, clone, case):
    """The on-screen text rule, read from the filled page (not from build_spec): each field equals its one source and
    every other string is a voices.json label, the series or handle from settings, the day or a unit."""
    w, s, led = clone(case)
    build(tmp_path, w, s, led)
    html = comp_html(w)
    spec, cap = extract(html, "SPEC"), extract(html, "CAP")
    v = voices()
    assert spec["cold_open"]["text"] == s["cold_open"]["on_screen"]
    assert [i["text"] for i in spec["story"]["items"]] == s["story"]["on_screen"]
    assert spec["steal_this"]["text"] == s["steal_this"]["on_screen"]
    assert spec["out"]["teaser"] == s["out"]["teaser"]
    assert spec["brand"] == SERIES.upper() and spec["channel"] == HANDLE and spec["day"] == "OCT 16"
    allowed = {SERIES.upper(), HANDLE, "sessions", spec["day"]}
    for h in v:
        if h != "host":
            allowed |= {v[h]["display"], v[h]["glyph"], v[h]["color"]}
    allowed |= set(gate.shipped_fields(s))
    if case == "none":
        assert spec["confessional"] is None
    else:
        q = compose.quote_by_id(led, s["confessional"]["quote_id"])
        assert spec["confessional"]["text"] == q["text"].strip()
        assert spec["confessional"]["label"] == LABELS[case]
        allowed.add(q["text"].strip())
    stray = [x for path, x in values(spec) if path != ("confessional", "label") and x not in allowed]
    assert stray == []
    host_words = {t for line in gate.host_lines(s) for t in line.split()}
    assert {w_ for e in cap for w_ in e["words"]} <= host_words


def test_the_host_voice_label_never_reaches_the_page(tmp_path, clone):
    w, s, led = clone()
    build(tmp_path, w, s, led)
    assert '"Sam"' not in comp_html(w)  # the narrator's display name is not an agent row or card


# ---- in a real browser, with a real media kit ----

@pytest.fixture(scope="module")
def browser():
    if KIT is None or not (KIT / "gsap.min.js").exists() or not (KIT / "fonts" / "Anton.ttf").exists():
        pytest.skip("no media kit (set ORION_CLOCK_TEST_KIT to a kit folder to run the browser tests)")
    pw = pytest.importorskip("playwright.sync_api")
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    ctx = pw.sync_playwright().start()
    try:
        b = ctx.chromium.launch(args=["--no-sandbox"], env=env, timeout=30000)
    except Exception as e:  # no browser installed
        ctx.stop()
        pytest.skip(f"no headless chromium: {type(e).__name__}")
    yield b
    b.close()
    ctx.stop()


def open_page(browser, w):
    pg = browser.new_page(viewport={"width": 1080, "height": 1920})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("file://" + str(w / "build" / "comps" / "daily" / "index.html"), wait_until="load", timeout=20000)
    try:
        pg.wait_for_function("Boolean(window.__timelines && window.__timelines.main)", timeout=10000)
    except Exception:
        raise AssertionError(f"page did not build its timeline: {errs}")
    return pg, errs


def seek_all(pg, tl):
    for v in tl["scenes"].values():
        for t in (v["start"] + v["dur"] / 2, v["start"] + v["dur"] - 0.05):
            pg.evaluate(f"window.__timelines.main.seek({t});0")


@pytest.mark.parametrize("case", CASES)
def test_hostile_text_never_becomes_markup_or_script(tmp_path, clone, browser, case):
    w, s, led = clone(case)
    set_text(w, s, led, {k: HOSTILE_TEXT for k in ("cold_open", "story", "steal", "teaser", "quote")})
    tl = build(tmp_path, w, s, led, kit=KIT)
    pg, errs = open_page(browser, w)
    try:
        html = comp_html(w)
        assert "<!--" not in html and "\\u003c!-- \\u003cscript " in html and "\\u0026amp;" in html
        seek_all(pg, tl)
        assert pg.evaluate("window.PWN") is None
        assert errs == []
        assert pg.evaluate("document.querySelectorAll('img,[onerror]').length") == 0
        assert pg.evaluate("document.querySelectorAll('script').length") == 2
        assert pg.evaluate("document.querySelectorAll('#root b').length") <= pg.evaluate(
            "document.querySelectorAll('#captions .cap').length")  # only the caption highlight makes <b>
        text = pg.evaluate("document.body.textContent")
        assert "<img src=x onerror=window.PWN=1>" in text
        assert pg.evaluate("document.querySelectorAll('#captions b').length") > 0
        if case != "none":
            assert pg.evaluate("document.querySelector('.cf-q').textContent").count("<img src=x") == 1
    finally:
        pg.close()


def _boxes(pg, scene):
    return pg.evaluate("""(sc) => { const out = {}; document.getElementById('sc-' + sc).querySelectorAll('*').forEach(e => {
        const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
        if (r.width && r.height && cs.opacity !== '0') (out[e.className] = out[e.className] || []).push(
          [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom), e.scrollWidth > e.clientWidth + 1]); });
        return out; }""", scene)


@pytest.mark.parametrize("glyph", ["W", "M"])
def test_four_extreme_story_cards_end_above_the_caption_zone(tmp_path, clone, browser, glyph):
    w, s, led = clone()
    s["story"]["on_screen"] = [glyph * 42] * 4
    tl = build(tmp_path, w, s, led, kit=KIT)
    pg, errs = open_page(browser, w)
    try:
        v = tl["scenes"]["story"]
        pg.evaluate(f"window.__timelines.main.seek({v['start'] + v['dur'] - 0.05});0")
        pg.evaluate("document.fonts.ready.then(() => 0)")
        pg.wait_for_timeout(300)
        boxes = _boxes(pg, "story")["st-c"]
        assert len(boxes) == 4 and max(b[3] for b in boxes) <= 1250
        assert all(b[0] >= 40 and b[2] <= 1040 and not b[4] for b in boxes) and errs == []
    finally:
        pg.close()


@pytest.mark.parametrize("case", ["transcript", "none"])
def test_worst_case_text_stays_in_the_safe_box_and_does_not_overlap(tmp_path, clone, browser, case):
    wide = " ".join(["WWWW"] * 8)  # 39 wide capitals
    w, s, led = clone(case)
    quote = " ".join(["Whatever"] * 30)  # the longest quote the ledger admits is 30 words
    set_text(w, s, led, {"cold_open": wide, "story": wide, "steal": "W" * 42, "teaser": "W" * 42, "quote": quote})
    s["story"]["on_screen"] = ["W" * 42, wide, "W" * 42, wide]
    tl = build(tmp_path, w, s, led, kit=KIT)
    pg, errs = open_page(browser, w)
    try:
        for sc, v in tl["scenes"].items():
            pg.evaluate(f"window.__timelines.main.seek({v['start'] + v['dur'] - 0.05});0")
            boxes = _boxes(pg, sc)
            for cls, bs in boxes.items():
                for b in bs:
                    assert 40 <= b[0] and b[2] <= 1040 and b[3] <= 1250 and not b[4], (sc, cls, b)
            if sc == "story":
                cards = sorted(boxes["st-c"], key=lambda b: b[1])
                assert len(cards) == 4 and all(a[3] <= b[1] for a, b in zip(cards, cards[1:]))
            if sc == "confessional":
                assert boxes["cf-q"][0][3] <= boxes["cf-lbl"][0][1]
            if sc == "out":
                assert boxes["ou-t"][0][3] <= boxes["ou-sub"][0][1] and boxes["ou-sub"][0][3] <= boxes["ou-like"][0][1]
        assert errs == []
    finally:
        pg.close()


def test_frame_zero_shows_the_hook(tmp_path, clone, browser):
    w, s, led = clone()
    build(tmp_path, w, s, led, kit=KIT)
    pg, errs = open_page(browser, w)
    try:
        pg.evaluate("window.__timelines.main.seek(0);0")
        assert pg.evaluate("getComputedStyle(document.querySelector('.co-w')).opacity") == "1"
        box = pg.evaluate("(() => { const r = document.querySelector('.co-w').getBoundingClientRect(); return [r.top, r.bottom]; })()")
        assert box[0] >= 200 and box[1] <= 1250 and errs == []
        assert pg.evaluate("document.querySelector('.co-w').textContent") == s["cold_open"]["on_screen"]
    finally:
        pg.close()


@pytest.mark.parametrize("given,shown", [("Steal this: check the output", "check the output"),
                                          ("STEAL THIS - check the output", "check the output"),
                                          ("steal this check", "check"), ("Steal this", "Steal this"),
                                          ("Stealthy tip", "Stealthy tip"), ("Check it. Steal this", "Check it. Steal this")])
def test_steal_card_drops_its_own_header_words(tmp_path, clone, browser, given, shown):
    w, s, led = clone()
    s["steal_this"]["on_screen"] = given
    build(tmp_path, w, s, led, kit=KIT)
    pg, errs = open_page(browser, w)
    try:
        assert pg.evaluate("document.querySelector('.sl-c').textContent") == shown
        assert pg.evaluate("[...document.querySelectorAll('#sc-steal_this .kicker')].map(e => e.textContent)") == ["STEAL THIS"]
    finally:
        pg.close()


def test_scoreboard_kicker_names_the_day_and_chrome_clears_the_top_bar(tmp_path, clone, browser):
    w, s, led = clone()
    build(tmp_path, w, s, led, kit=KIT)
    pg, errs = open_page(browser, w)
    try:
        assert pg.evaluate("document.querySelector('#sc-scoreboard .kicker').textContent") == "OCT 16, BY THE NUMBERS"
        assert "YESTERDAY" not in comp_html(w)
        tops = pg.evaluate("['.brand', '.day'].map(q => document.querySelector(q).getBoundingClientRect().top)")
        assert all(t >= 200 for t in tops)
        assert pg.evaluate("document.querySelector('.brand').textContent") == SERIES.upper()
        t = compose.TEMPLATE.read_text(encoding="utf-8")
        assert "left: 60px; width: 860px" in t.split("#captions .cap {", 1)[1].split("}", 1)[0]
    finally:
        pg.close()


def test_null_totals_leave_two_tiles_in_a_real_browser(tmp_path, clone, browser):
    w, s, led = clone()
    led["scoreboard"]["prs_merged"] = None
    led["scoreboard"]["arcs_closed"] = None
    build(tmp_path, w, s, led, kit=KIT, handle=None)
    pg, errs = open_page(browser, w)
    try:
        assert pg.evaluate("document.querySelectorAll('.sb-t').length") == 2
        assert pg.evaluate("document.querySelectorAll('.ou-h').length") == 0 and errs == []
    finally:
        pg.close()


def test_the_scene_order_comes_from_compose_scenes_through_the_timing_blob():
    t = compose.TEMPLATE.read_text(encoding="utf-8")
    assert "const ORDER = T.order" in t
    assert not any(f'"{a}", "{b}"' in t for a, b in zip(compose.SCENES, compose.SCENES[1:]))  # no second copy
    tl = {"total": 60.0, "scenes": {}, "lines": []}
    html = compose.fill("{{total}}|__CLIPS__|__SPEC__|__TIMING__|__CAPTIONS__", tl, {}, [])
    blob = json.loads(html.split("|")[3])
    assert blob["order"] == list(compose.SCENES)


def test_no_long_dashes_in_this_test_file():
    assert not [c for c in Path(__file__).read_text(encoding="utf-8") if c in LONG_DASHES]
