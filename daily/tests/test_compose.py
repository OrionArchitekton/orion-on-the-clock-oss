"""The timeline, the cue times, the karaoke captions and the small helpers of daily.compose. What compose.build fills
into the page, and where on-screen text comes from, is test_template.py's."""
import json
from pathlib import Path

import pytest

from daily import compose, day, voice
from media_helpers import CASES, ledger, script, voices


def L(i, dur, words=None):
    return {"id": i, "dur": dur, "words": words if words is not None else [{"word": "w", "start": 0.0, "end": dur}]}


def lines(**dur):
    d = {"cold_open": 2.0, "scoreboard": 3.0, "story_0": 6.0, "story_1": 6.0, "story_2": 6.0, "steal_this": 4.0,
         "confessional_intro": 2.0, "confessional": 4.0, "out": 3.0}
    d.update(dur)
    return [L(k, v) for k, v in d.items() if v is not None]


BASE = lines()
NO_CONF = lines(confessional_intro=None, confessional=None)


def hand_total(extra=0.0, conf=True, steal=4.0):
    """The rule by hand: per scene LEAD + lines + GAPs + HOLD + extra; the Steal-this scene is at least 3.0 s."""
    scenes = [0.15 + 2.0 + 0.5, 0.15 + 3.0 + 1.4, 0.15 + 18.0 + 0.5 + 0.6]
    steal_scene = max(0.15 + steal + 1.0 + extra, 3.0)
    scenes.append(steal_scene - extra)
    if conf:
        scenes.append(0.15 + 2.0 + 0.25 + 4.0 + 0.8)
    scenes.append(0.15 + 3.0 + 2.2)
    return round(sum(scenes) + extra * len(scenes), 3)


def test_scene_table():
    assert compose.SCENES == ("cold_open", "scoreboard", "story", "steal_this", "confessional", "out")
    assert compose.OPTIONAL_SCENES == frozenset({"confessional"})
    assert compose.scene_of("steal_this") == "steal_this"
    with pytest.raises(ValueError):
        compose.scene_of("bogus")


def test_short_timeline_is_padded_to_the_minimum():
    tl = compose.build_timeline(BASE)
    base = hand_total()
    assert base < 60.0
    pad = min(2.0, (60.0 - base) / 6)
    assert tl["padded_per_scene"] == round(pad, 3)
    assert tl["total"] == hand_total(extra=round(pad, 3))
    assert list(tl["scenes"]) == list(compose.SCENES)
    assert tl["lines"][0] == {"id": "cold_open", "scene": "cold_open", "start": 0.15, "dur": 2.0,
                              "file": "cold_open.wav"}


def test_padding_divides_over_present_scenes():
    with_conf = compose.build_timeline(BASE)
    without = compose.build_timeline(NO_CONF)
    assert list(without["scenes"]) == ["cold_open", "scoreboard", "story", "steal_this", "out"]
    base = hand_total(conf=False)
    assert without["padded_per_scene"] == round(min(2.0, (60.0 - base) / 5), 3)
    assert without["padded_per_scene"] > with_conf["padded_per_scene"] or without["padded_per_scene"] == 2.0
    assert without["total"] == hand_total(extra=without["padded_per_scene"], conf=False)


def test_long_timeline_is_never_trimmed():
    tl = compose.build_timeline([dict(x, dur=x["dur"] * 3) for x in BASE])
    assert tl["padded_per_scene"] == 0.0 and tl["total"] > 75.0


def test_story_lines_are_gapped_and_words_are_absolute():
    tl = compose.build_timeline(BASE)
    s = {x["id"]: x["start"] for x in tl["lines"]}
    assert round(s["story_1"] - (s["story_0"] + 6.0), 3) == compose.GAP
    w = [x for x in tl["words"] if x["line"] == "story_1"][0]
    assert w["start"] == s["story_1"]


@pytest.mark.parametrize("missing", ["cold_open", "scoreboard", "story_0", "steal_this", "out"])
def test_missing_required_scene_is_an_error(missing):
    scene = compose.scene_of(missing)
    with pytest.raises(ValueError, match=scene):
        compose.build_timeline([x for x in BASE if compose.scene_of(x["id"]) != scene])


def test_steal_this_scene_lasts_at_least_three_seconds():
    tl = compose.build_timeline(lines(steal_this=1.0), min_total=0.0)
    assert tl["padded_per_scene"] == 0.0
    assert tl["scenes"]["steal_this"]["dur"] == 3.0
    ok = compose.build_timeline(lines(steal_this=4.0), min_total=0.0)
    assert ok["scenes"]["steal_this"]["dur"] == round(0.15 + 4.0 + compose.HOLD["steal_this"], 3)
    padded = compose.build_timeline(lines(steal_this=1.0))
    assert padded["scenes"]["steal_this"]["dur"] >= 3.0


def test_cues_land_story_cards_on_story_lines():
    tl = compose.build_timeline(BASE)
    c = compose.cues(tl, 4)
    s = {x["id"]: x["start"] for x in tl["lines"]}
    assert c["story_items"][:3] == [s["story_0"], s["story_1"], s["story_2"]]
    assert c["story_items"][3] == round(s["story_2"] + 1.2, 3)
    assert c["subscribe"] == round(s["out"] + 0.6, 3) and c["like"] == round(s["out"] + 1.2, 3)
    assert c["quote_in"] == s["confessional"]
    assert c["steal_in"] == s["steal_this"]
    assert c["harness_in"] == round(tl["scenes"]["confessional"]["start"] + 0.05, 3)


def test_cues_without_a_confessional():
    tl = compose.build_timeline(NO_CONF)
    c = compose.cues(tl, 3)
    assert c["harness_in"] is None and c["quote_in"] is None
    assert c["steal_in"] == {x["id"]: x["start"] for x in tl["lines"]}["steal_this"]


def test_story_cards_never_spill_into_steal_this():
    tl = compose.build_timeline(BASE)
    steal = tl["scenes"]["steal_this"]["start"]
    assert all(t < steal for t in compose.cues(tl, 5)["story_items"])
    with pytest.raises(ValueError, match="story"):
        compose.cues(tl, 40)


def test_karaoke_events_grow_word_by_word():
    words = [{"word": f"w{i}", "start": 0.5 * i, "end": 0.5 * i + 0.4} for i in range(7)]
    tl = compose.build_timeline([L("cold_open", 3.6, words)] + BASE[1:])
    events, groups = compose.caption_events(tl)
    first = [e for e in events if e["words"][0] == "w0"]
    assert [e["words"] for e in first][:2] == [["w0"], ["w0", "w1"]]
    assert first[4]["active"] == 4 and first[4]["words"] == ["w0", "w1", "w2", "w3", "w4"]
    assert first[4]["end"] <= [e for e in events if e["words"] == ["w5"]][0]["start"]
    assert groups[0] == {"line": "cold_open", "words": ["w0", "w1", "w2", "w3", "w4"]}


def test_confessional_gets_no_captions_but_steal_this_and_intro_do():
    _, groups = compose.caption_events(compose.build_timeline(BASE))
    got = {g["line"] for g in groups}
    assert "confessional" not in got
    assert {"steal_this", "confessional_intro", "cold_open", "out"} <= got


def test_local_label_uses_the_settings_zone_and_its_daylight_saving():
    """conftest puts every test in America/Los_Angeles; run.py does the same once per run from settings.toml."""
    assert compose.local_label("2026-10-16T21:14:05Z") == ("2:14 pm", "PDT")
    assert compose.local_label("2026-11-20T21:14:05Z") == ("1:14 pm", "PST")
    assert compose.local_label("2026-10-16T21:14:05+00:00") == ("2:14 pm", "PDT")
    assert compose.local_label("2026-10-16T14:14:05-07:00") == ("2:14 pm", "PDT")


@pytest.mark.parametrize("zone,want", [("Asia/Tokyo", ("6:14 am", "JST")), ("UTC", ("9:14 pm", "UTC")),
                                       ("Europe/Berlin", ("11:14 pm", "CEST")), ("Asia/Dubai", ("1:14 am", "+04"))])
def test_local_label_follows_whatever_zone_the_run_uses(zone, want):
    day.use_timezone(zone)  # the conftest puts the zone back after the test
    assert compose.local_label("2026-10-16T21:14:05Z") == want


@pytest.mark.parametrize("ts", ["2026-10-16T21:14:05", "2026-10-16 21:14:05"])
def test_local_label_rejects_naive_timestamps(ts):
    with pytest.raises(ValueError):
        compose.local_label(ts)


@pytest.mark.parametrize("case", CASES)
def test_scene_agrees_with_voice_build_lines(case):
    built = voice.build_lines(script(case), ledger(), voices())
    assert built and ("confessional" in [ln["id"] for ln in built]) == (case != "none")
    for ln in built:
        assert compose.scene_of(ln["id"]) == ln["scene"]


# ---- review round: pad divisor, pad vs Steal-this stretch, boundary mutants ----

def test_pad_divides_by_present_scenes_with_exact_values():
    with_conf = compose.build_timeline(lines(story_0=10.0, story_1=10.0, story_2=10.0))
    # scenes: 2.65 + 4.55 + 31.25 + 5.15 + 7.2 + 5.35 = 56.15; 3.85 / 6 over six scenes
    assert with_conf["padded_per_scene"] == 0.642 and with_conf["total"] == 60.0
    no_conf = compose.build_timeline(lines(story_0=12.0, story_1=12.0, story_2=12.0,
                                           confessional_intro=None, confessional=None))
    # 2.65 + 4.55 + 37.25 + 5.15 + 5.35 = 54.95; 5.05 / 5 over five scenes
    assert no_conf["padded_per_scene"] == 1.01 and no_conf["total"] == 60.0


def test_a_short_steal_this_line_does_not_leave_the_day_short():
    # steal_this 0.5 s is stretched to 3.0 s, which used to absorb its share of the pad (day landed ~58.65 s)
    cases = [(9.0, {}), (13.0, {"confessional_intro": None, "confessional": None})]
    for story, extra in cases:
        tl = compose.build_timeline(lines(steal_this=0.5, story_0=story, story_1=story, story_2=story, **extra))
        assert tl["total"] == 60.0, (story, tl["total"])
        assert 0.0 < tl["padded_per_scene"] < compose.PAD_MAX
        assert tl["scenes"]["steal_this"]["dur"] >= compose.STEAL_MIN


def test_pad_stays_capped_when_the_day_cannot_reach_the_minimum():
    tl = compose.build_timeline(lines(steal_this=0.5))
    assert tl["padded_per_scene"] == compose.PAD_MAX and tl["total"] < 60.0


def _hand_tl(story_start, steal_start):
    return {"lines": [{"id": "story_0", "scene": "story", "start": story_start},
                      {"id": "steal_this", "scene": "steal_this", "start": steal_start + 0.15},
                      {"id": "out", "scene": "out", "start": 50.0}],
            "scenes": {"scoreboard": {"start": 2.0}, "steal_this": {"start": steal_start}}}


def test_story_card_clamp_boundary():
    with pytest.raises(ValueError):
        compose.cues(_hand_tl(10.0, 11.2), 2)  # second card lands exactly on the steal_this start
    assert compose.cues(_hand_tl(10.0, 11.21), 2)["story_items"] == [10.0, 11.2]


def test_scoreboard_count_offset():
    tl = compose.build_timeline(BASE)
    assert compose.cues(tl, 3)["count"] == round(tl["scenes"]["scoreboard"]["start"] + 0.4, 3)


def test_caption_tail_value_and_clamp_to_the_next_line():
    assert compose.CAPTION_TAIL == 0.15
    def tl(second_start):
        return {"total": 20.0, "lines": [{"id": "cold_open", "start": 0.0}, {"id": "scoreboard", "start": second_start}],
                "words": [{"line": "cold_open", "word": "a", "start": 0.0, "end": 1.0},
                          {"line": "scoreboard", "word": "b", "start": second_start, "end": second_start + 0.5}]}
    ev, _ = compose.caption_events(tl(5.0))
    assert [e for e in ev if e["words"] == ["a"]][0]["end"] == 1.15  # the full tail
    ev, _ = compose.caption_events(tl(1.05))
    assert [e for e in ev if e["words"] == ["a"]][0]["end"] == 1.05  # clamped to the next line's start


def test_two_words_with_the_same_start_make_no_empty_event():
    words = [{"word": "a", "start": 0.0, "end": 0.4}, {"word": "b", "start": 0.0, "end": 0.4},
             {"word": "c", "start": 0.5, "end": 0.9}]
    tl = compose.build_timeline([L("cold_open", 2.0, words)] + BASE[1:])
    ev, _ = compose.caption_events(tl)
    mine = [e for e in ev if e["words"][0] == "a"]
    assert all(e["end"] > e["start"] for e in ev)
    assert [e["words"] for e in mine] == [["a", "b"], ["a", "b", "c"]]


@pytest.mark.parametrize("source,dry,ok", [("whisper", False, True), ("dry", True, True), ("dry", False, False),
                                           ("whisper", True, False), ("", False, False), (None, False, False),
                                           ("other", False, False), ("other", True, False)])
def test_words_source_must_be_exactly_whisper_for_real_and_dry_for_dry(tmp_path, source, dry, ok):
    vd = tmp_path / "voice"
    vd.mkdir()
    (vd / "lines.json").write_text(json.dumps([{"id": "cold_open", "text": "one two"}]))
    rec = {"id": "cold_open", "text": "one two", "dur": 1.0, "source": source,
           "words": [{"word": "one", "start": 0, "end": 0.5}, {"word": "two", "start": 0.5, "end": 1}]}
    (vd / "cold_open.words.json").write_text(json.dumps(rec))
    if ok:
        assert compose.voiced_lines(tmp_path, dry)[0]["id"] == "cold_open"
    else:
        with pytest.raises(ValueError):
            compose.voiced_lines(tmp_path, dry)


# ---- quote_by_id ----

def test_quote_by_id_finds_one_quote_and_refuses_none_or_two():
    led = ledger()
    q = led["quotes"][0]
    assert compose.quote_by_id(led, q["id"]) is q
    with pytest.raises(LookupError):
        compose.quote_by_id(led, "q:codex:00000000")
    led["quotes"].append(dict(q))
    with pytest.raises(LookupError, match="more than once"):
        compose.quote_by_id(led, q["id"])


@pytest.mark.parametrize("quotes", [None, "q:codex:x", {"id": "q:codex:x"}, 7])
def test_quote_by_id_refuses_a_ledger_without_a_quote_list(quotes):
    with pytest.raises(LookupError):
        compose.quote_by_id({"quotes": quotes}, "q:codex:x")


# ---- check_text and js ----

@pytest.mark.parametrize("code", [0x2013, 0x2014, 0x2015])
def test_check_text_finds_a_long_dash_anywhere_in_nested_data(code):
    dash = chr(code)
    compose.check_text({"a": ["fine", {"b": "also fine"}]})  # positive control
    with pytest.raises(ValueError, match=r"long dash in spec\.a\[1\]\.b"):
        compose.check_text({"a": ["fine", {"b": "x " + dash + " y"}]})


def test_js_escapes_what_could_close_a_script_tag_or_open_markup():
    out = compose.js({"t": "</script><b>&amp;"})
    assert "<" not in out and ">" not in out and "&" not in out
    assert json.loads(out) == {"t": "</script><b>&amp;"}


def test_fill_refuses_a_template_missing_a_slot():
    tl = {"total": 60.0, "scenes": {}, "lines": []}
    with pytest.raises(ValueError, match="placeholders"):
        compose.fill("{{total}}|__CLIPS__|__SPEC__|__TIMING__", tl, {}, [])


def test_no_long_dashes_in_this_test_file():
    dashes = {chr(c) for c in (0x2013, 0x2014, 0x2015)}
    assert not dashes & set(Path(__file__).read_text(encoding="utf-8"))
