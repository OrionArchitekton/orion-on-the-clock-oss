"""Compose the episode: the timeline from the voiced lines, the cue times the picture and the sound share, the karaoke
caption events, and the filled HyperFrames composition (daily/engine/template-daily.html).

The timeline rule: each scene is a lead, its lines and a hold. Scenes are fixed: cold_open, scoreboard, story,
steal_this, then the optional confessional, then out. A day shorter than MIN_TOTAL gets its holds stretched (at most
PAD_MAX more per present scene); a long day is never trimmed, so the render check fails it honestly. This module never
imports daily.voice (voice imports compose); a test keeps scene_of in step with voice's scenes.

On-screen text comes only from the gate-scanned script fields, the gate-verified quote (by id), the voices.json labels,
the operator's settings (the series name and the channel handle), the day and fixed strings; every string is checked
for long dashes before the page is written. Counts come from the ledger. A count the ledger holds as null (unknown:
GitHub switched off, no tracker) is drawn as nothing, never as a zero."""
from __future__ import annotations

import json
import re
import shutil
from datetime import date, datetime
from pathlib import Path

from daily import day as days
from daily import files, paths, voiceconf

SCENES = ("cold_open", "scoreboard", "story", "steal_this", "confessional", "out")
OPTIONAL_SCENES = frozenset({"confessional"})
LEAD = 0.15
GAP = 0.25
HOLD = {"cold_open": 0.5, "scoreboard": 1.4, "story": 0.6, "steal_this": 1.0, "confessional": 0.8, "out": 2.2}
STEAL_MIN = 3.0  # the Steal-this card stays on screen at least this long, so it can be read
MIN_SCENE = {"steal_this": STEAL_MIN}
MIN_TOTAL = 60.0
PAD_MAX = 2.0
CAPTION_WORDS = 5
CAPTION_TAIL = 0.15
NO_CAPTIONS = frozenset({"confessional"})  # the quote shows as its own card; captions come from host lines
STORY_CARD_STEP = 1.2


def r3(x: float) -> float:
    return round(x + 0.0, 3)


def quote_by_id(ledger: dict, quote_id: str) -> dict:
    """The one quote with this id. Fails closed with LookupError when there is none or more than one: the gate
    verified a single quote under the id (it takes a quote only when exactly one matches), so with two entries it is
    not known which one it checked, and neither may be voiced or shown."""
    quotes = ledger.get("quotes")
    matches = [q for q in quotes if isinstance(q, dict) and q.get("id") == quote_id] if isinstance(quotes, list) else []
    if not matches:
        raise LookupError(f"quote {quote_id!r} is not in the ledger")
    if len(matches) > 1:
        raise LookupError(f"quote {quote_id!r} is in the ledger more than once")
    return matches[0]


def scene_of(line_id: str) -> str:
    if line_id.startswith("story_"):
        return "story"
    if line_id in ("confessional_intro", "confessional"):
        return "confessional"
    if line_id in ("cold_open", "scoreboard", "steal_this", "out"):
        return line_id
    raise ValueError(f"unknown line id {line_id!r}")


def build_timeline(lines: list[dict], min_total: float = MIN_TOTAL) -> dict:
    """lines: [{"id", "dur", "words": [{"word", "start", "end"}]}] in speaking order, word times relative to the
    line. Returns {"total", "scenes": {id: {start, dur}} (present scenes only), "lines": [{id, scene, start, dur,
    file}], "words": [{line, word, start, end}] (absolute), "padded_per_scene"}. A scene with no lines is an error
    unless it is optional, in which case it is absent (no lead, no hold)."""
    by_scene = {s: [ln for ln in lines if scene_of(ln["id"]) == s] for s in SCENES}
    for s in SCENES:
        if not by_scene[s] and s not in OPTIONAL_SCENES:
            raise ValueError(f"scene {s} has no lines")
    present = [s for s in SCENES if by_scene[s]]

    def lay(extra: float) -> dict:
        t, scenes, placed, words = 0.0, {}, [], []
        for s in present:
            start = t
            t += LEAD
            for k, ln in enumerate(by_scene[s]):
                if k:
                    t += GAP
                placed.append({"id": ln["id"], "scene": s, "start": r3(t), "dur": r3(ln["dur"]),
                               "file": f"{ln['id']}.wav"})
                words += [{"line": ln["id"], "word": w["word"], "start": r3(t + w["start"]), "end": r3(t + w["end"])}
                          for w in ln["words"]]
                t += ln["dur"]
            t += max(HOLD[s] + extra, MIN_SCENE.get(s, 0.0) - (t - start))
            scenes[s] = {"start": r3(start), "dur": r3(t - start)}
        return {"total": r3(t), "scenes": scenes, "lines": placed, "words": words}

    tl = lay(0.0)
    pad = 0.0
    # The Steal-this stretch absorbs part of any pad (its scene only grows once hold + pad passes the stretch), so one
    # division can leave the day short: raise the pad by the remaining shortfall until the day reaches min_total or
    # the cap. The total grows with the pad, so this converges.
    for _ in range(100):
        if tl["total"] >= min_total or pad >= PAD_MAX:
            break
        pad = min(PAD_MAX, pad + (min_total - tl["total"]) / len(present))
        tl = lay(pad)
    tl["padded_per_scene"] = r3(pad)
    return tl


def cues(tl: dict, n_items: int) -> dict:
    """Times the picture and the sound share: story card k lands as story line k starts (cards past the last line
    follow it STORY_CARD_STEP apart and must land before the Steal-this scene, else ValueError); the scoreboard counts
    up 0.4 s into its scene; the Steal-this card lands as its line starts; the harness card lands as the confessional
    scene starts and the quote as its line starts (both None without a confessional); the subscribe button and the like
    sticker land 0.6 s and 1.2 s after the out line starts."""
    start = {ln["id"]: ln["start"] for ln in tl["lines"]}
    story = sorted((ln for ln in tl["lines"] if ln["scene"] == "story"), key=lambda ln: ln["start"])
    items = [story[k]["start"] if k < len(story) else r3(story[-1]["start"] + STORY_CARD_STEP * (k - len(story) + 1))
             for k in range(n_items)]
    sc = tl["scenes"]
    items = [r3(x) for x in items]
    if items and max(items) >= sc["steal_this"]["start"]:
        raise ValueError("story items do not fit the story time")
    conf = "confessional" in sc
    return {"count": r3(sc["scoreboard"]["start"] + 0.4), "story_items": items,
            "steal_in": start["steal_this"],
            "harness_in": r3(sc["confessional"]["start"] + 0.05) if conf else None,
            "quote_in": start["confessional"] if conf and "confessional" in start else None,
            "subscribe": r3(start["out"] + 0.6), "like": r3(start["out"] + 1.2)}


def caption_events(tl: dict, max_words: int = CAPTION_WORDS, tail: float = CAPTION_TAIL) -> tuple[list, list]:
    """Karaoke captions: per captioned line, groups of max_words; each event shows the group so far with the word
    being spoken active. Returns (events [{start, end, words, active}], groups [{line, words}])."""
    order = sorted(tl["lines"], key=lambda ln: ln["start"])
    next_start = {ln["id"]: (order[i + 1]["start"] if i + 1 < len(order) else tl["total"])
                  for i, ln in enumerate(order)}
    events, groups = [], []
    for ln in order:
        if ln["id"] in NO_CAPTIONS:
            continue
        ws = [w for w in tl["words"] if w["line"] == ln["id"]]
        for g0 in range(0, len(ws), max_words):
            grp = ws[g0:g0 + max_words]
            nxt = ws[g0 + max_words]["start"] if g0 + max_words < len(ws) else next_start[ln["id"]]
            line_tail = min(grp[-1]["end"] + tail, nxt)
            groups.append({"line": ln["id"], "words": [w["word"] for w in grp]})
            for i, w in enumerate(grp):
                end = grp[i + 1]["start"] if i + 1 < len(grp) else line_tail
                if end > w["start"]:
                    events.append({"start": w["start"], "end": r3(end), "words": [x["word"] for x in grp[:i + 1]],
                                   "active": i})
    return events, groups


def local_label(ts: str) -> tuple:
    """(wall-clock time as "2:14 pm", the zone's abbreviation such as "PDT") of an aware ISO timestamp, in the
    settings' time zone (daily.day.zone(), which run.py sets once per run). A timestamp with no zone is a ValueError:
    the zone is never guessed."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("timestamp has no zone")
    local = dt.astimezone(days.zone())
    return local.strftime("%I:%M %p").lstrip("0").lower(), local.strftime("%Z") or "UTC"


ENGINE = paths.ENGINE
TEMPLATE = ENGINE / "template-daily.html"
COMP = "daily"
GSAP = "gsap.min.js"
FONTS = ("Anton.ttf", "InterVariable.ttf")
MAX_CARDS = 4
LONG_DASHES = (chr(0x2013), chr(0x2014), chr(0x2015))
_SLOTS = re.compile(r"__(CLIPS|SPEC|TIMING|CAPTIONS)__")


def check_text(obj, where: str = "spec") -> None:
    """House style: no em, en or horizontal-bar dash in anything on screen."""
    if isinstance(obj, str):
        if any(d in obj for d in LONG_DASHES):
            raise ValueError(f"long dash in {where}: {obj[:60]!r}")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            check_text(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            check_text(v, f"{where}[{i}]")


def _count(value, what: str) -> int:
    """A scoreboard count is a real int: a bool, float, string or None is a malformed ledger, never drawn."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"count for {what} is not an int")
    return value


def _count_or_unknown(value, what: str):
    """A top-line count the ledger may hold as null when its source was not read (GitHub switched off, no tracker):
    None stays None (the page leaves that tile out), anything else must be a real int."""
    return None if value is None else _count(value, what)


MAX_ROWS = 6  # the scoreboard rows the template has room for


def scoreboard_rows(ledger: dict, voices: dict) -> list[dict]:
    """One row per agent in ledger["coverage"], in the ledger's order (built-in agents first, then your adapters in
    their configured order), at most MAX_ROWS. Unavailable ("unavailable:<reason>"): n None, drawn as NO DATA TODAY,
    never a zero. Otherwise its count from scoreboard.sessions, else no row. The labels come from voices.json,
    outside script.json; an agent in the coverage with no voices.json entry is a ValueError (the page could not
    label it)."""
    sb = ledger["scoreboard"]
    sessions = sb.get("sessions", {})
    coverage = ledger.get("coverage", {})
    rows = []
    for h in coverage:
        v = voices.get(h) if h != voiceconf.HOST else None
        if v is None:
            raise ValueError("an agent in the coverage has no voice in voices.json")
        row = {"harness": h, "display": v["display"], "glyph": v["glyph"], "color": v["color"]}
        if str(coverage[h]).startswith("unavailable"):
            rows.append({**row, "n": None, "unit": None, "available": False})
        elif h in sessions:
            rows.append({**row, "n": _count(sessions[h], h), "unit": "sessions", "available": True})
    if not rows:
        raise ValueError("scoreboard has no harness rows")
    if len(rows) > MAX_ROWS:
        raise ValueError(f"scoreboard has {len(rows)} rows; at most {MAX_ROWS}")
    return rows


def quote_label(q: dict, display: str) -> str:
    """The provenance line under a confessional quote, by the quote's kind, with the local time it was written. Any
    other kind has no label."""
    clock, zone = local_label(q["ts"])
    when = f"{clock} {zone}"
    if q.get("kind") == "transcript":
        text = f"VERBATIM FROM {display} LOG, {when}"
    elif q.get("kind") == "pr":
        text = f"FROM {display}'S PR DESCRIPTION, {when}"
    else:
        raise ValueError(f"quote kind {q.get('kind')!r} has no label")
    return text.upper()


def build_spec(script: dict, ledger: dict, voices: dict, tl: dict, day: date, *, series: str,
               handle: str | None = None) -> dict:
    """What the template draws. On-screen text comes only from the gate-scanned script fields, the gate-verified quote
    (by id), voices.json labels, the series name and channel handle from settings, the day and fixed strings.
    Scoreboard numbers come from the ledger; an unknown (null) PR or task count is None and its tile is left out."""
    items = script["story"]["on_screen"]
    if len(items) > MAX_CARDS:
        raise ValueError(f"story has {len(items)} on-screen cards; at most {MAX_CARDS}")
    sb = ledger["scoreboard"]
    rows = scoreboard_rows(ledger, voices)
    qid = script["confessional"]["quote_id"]
    conf = None
    if qid:
        q = quote_by_id(ledger, qid)
        hv = voices[q["harness"]]
        conf = {"display": hv["display"], "glyph": hv["glyph"], "color": hv["color"], "text": q["text"].strip(),
                "label": quote_label(q, hv["display"])}
        conf["harness"] = q["harness"]
    if (conf is not None) != ("confessional" in tl["scenes"]):
        raise ValueError("confessional scene and quote disagree")
    c = cues(tl, len(items))
    subs = [_count(v, k) for k, v in sb["subagents"].items()]
    spec = {
        "brand": series.upper(),
        "channel": handle,
        "day": day.strftime("%b %d").upper(),
        "cold_open": {"text": script["cold_open"]["on_screen"]},
        "scoreboard": {"prs": _count_or_unknown(sb.get("prs_merged"), "prs_merged"),
                       "arcs": _count_or_unknown(sb.get("arcs_closed"), "arcs_closed"),
                       "sessions": sum(r["n"] for r in rows if r["unit"] == "sessions"), "subagents": sum(subs),
                       # the harness id is code, not content: the page shows display, glyph and color
                       "rows": [{k: v for k, v in r.items() if k != "harness"} for r in rows]},
        "story": {"items": [{"text": t, "at": c["story_items"][k]} for k, t in enumerate(items)]},
        "steal_this": {"text": script["steal_this"]["on_screen"], "at": c["steal_in"]},
        "confessional": conf and {k: v for k, v in conf.items() if k != "harness"},
        "out": {"teaser": script["out"]["teaser"]},
        "cues": c,
    }
    return spec


def clips_html(tl: dict) -> str:
    return "\n".join(f'      <div id="sc-{s}" class="clip" data-start="{tl["scenes"][s]["start"]}" '
                     f'data-duration="{tl["scenes"][s]["dur"]}" data-track-index="1"></div>'
                     for s in SCENES if s in tl["scenes"])


def js(x) -> str:
    """JSON for an inline script: < > & are escaped, so no text can close the script tag or open markup."""
    return (json.dumps(x, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026"))


def fill(template: str, tl: dict, spec: dict, events: list[dict]) -> str:
    """One pass over the template's slots, so text inside the inserted data is never itself replaced."""
    html = template.replace("{{total}}", str(tl["total"]))
    fills = {"__CLIPS__": clips_html(tl), "__SPEC__": js(spec),
             "__TIMING__": js({"total": tl["total"], "scenes": tl["scenes"], "lines": tl["lines"], "order": list(SCENES)}),
             "__CAPTIONS__": js(events)}
    missing = [k for k in fills if k not in html]
    if missing or "{{" in html:
        raise ValueError(f"template placeholders wrong (missing {missing})")
    return _SLOTS.sub(lambda m: fills[m.group(0)], html)


def voiced_lines(work: Path, dry: bool) -> list[dict]:
    """The voiced lines in lines.json order. Each line's words.json must carry the same id and text; a dry file is
    refused on a real build and only a dry file is accepted on a dry one."""
    voice_dir = Path(work) / "voice"
    out = []
    for ln in files.read_json(voice_dir / "lines.json"):
        scene_of(ln["id"])  # an id that is no scene's line never reaches a path
        rec = files.read_json(voice_dir / f"{ln['id']}.words.json")
        if rec.get("id") != ln["id"]:
            raise ValueError(f"words file id mismatch for {ln['id']}")
        if rec.get("text") != ln["text"]:
            raise ValueError(f"words file text mismatch for {ln['id']}")
        if [w.get("word") for w in rec["words"]] != ln["text"].split():
            raise ValueError(f"caption words of {ln['id']} are not the line's words")
        want = "dry" if dry else "whisper"  # a positive check: only real Whisper words go into a real build
        if rec.get("source") != want:
            raise ValueError(f"line {ln['id']} source {rec.get('source')!r} is not {want!r} (dry={bool(dry)})")
        out.append({"id": ln["id"], "dur": rec["dur"], "words": rec["words"]})
    return out


def build(work: Path, day: date, script: dict, ledger: dict, *, dry: bool, kit: Path, voices: dict, series: str,
          handle: str | None = None) -> dict:
    """Lay out the timeline from the voiced lines, fill the template into build/comps/daily/index.html (with fonts and
    GSAP from the media kit, settings.toml `render.kit`) and write build/timeline.json. script and ledger are the
    objects the caller parsed from the bytes it checked against the verdict, and voices the parsed voices it read once
    and hashed: this never opens script.json, ledger.json or voices.json. Returns the timeline."""
    work = Path(work)
    kit = Path(kit)
    for stale in (work / "build" / "comps" / COMP / "index.html", work / "build" / "timeline.json"):
        stale.unlink(missing_ok=True)  # a failed build never leaves an old composition behind
    tl = build_timeline(voiced_lines(work, dry))
    events, groups = caption_events(tl)
    spec = build_spec(script, ledger, voices, tl, day, series=series, handle=handle)
    check_text(spec)
    check_text(events, "captions")
    qid = script["confessional"]["quote_id"]
    tl.update({"captions": events, "caption_groups": groups, "cues": spec["cues"],
               "confessional_harness": quote_by_id(ledger, qid)["harness"] if qid else None})
    comp = work / "build" / "comps" / COMP
    (comp / "assets" / "fonts").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(kit / GSAP, comp / "assets" / GSAP)
    for f in FONTS:
        shutil.copyfile(kit / "fonts" / f, comp / "assets" / "fonts" / f)
    files.write_text(comp / "index.html", fill(TEMPLATE.read_text(encoding="utf-8"), tl, spec, events))
    files.write_text(work / "build" / "timeline.json", json.dumps(tl, indent=1, ensure_ascii=False))
    return tl
