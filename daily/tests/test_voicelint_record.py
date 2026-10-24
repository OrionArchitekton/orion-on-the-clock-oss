"""The host-line record (voicelint.write_record, hostlint.json): an advisory record of the tells left in the lines the
host's voice will say, written by the voice stage before any audio is made. It never blocks."""
import json
from pathlib import Path

import pytest

from daily import voicelint, writer
from media_helpers import CASES, script

ZWSP = chr(0x200B)
LONG_DASHES = "".join(chr(c) for c in (0x2013, 0x2014, 0x2015))  # built from code points: no dash in this file


def flags_of(rec, line):
    return {f["rule"] for f in rec["result"]["flags"] if f["line"] == line}


# ---- which lines the lint reads ----

def test_host_lines_are_the_host_lines_in_episode_order():
    s = script()
    lines = voicelint.host_lines(s)
    assert [i for i, _ in lines] == ["cold_open", "scoreboard", "story_0", "story_1", "story_2", "steal_this",
                                     "confessional_intro", "out"]
    assert [t for _, t in lines] == [s["cold_open"]["host"], s["scoreboard"]["host"], *s["story"]["host"],
                                     s["steal_this"]["host"], s["confessional"]["host_intro"], s["out"]["host"]]


def test_a_script_with_no_confessional_has_no_confessional_line():
    ids = {case: [i for i, _ in voicelint.host_lines(script(case))] for case in CASES}
    assert "confessional_intro" in ids["transcript"] and "confessional_intro" in ids["pr"]  # positive control
    assert "confessional_intro" not in ids["none"] and "steal_this" in ids["none"]


def test_an_empty_host_line_is_not_a_line():
    s = script()
    s["story"]["host"] = ["Only one story line, and I wrote it myself.", "   ", ""]
    assert [i for i, _ in voicelint.host_lines(s)] == ["cold_open", "scoreboard", "story_0", "steal_this",
                                                       "confessional_intro", "out"]


# ---- the record ----

def test_lint_flags_tells_and_never_blocks(tmp_path):
    s = script()
    s["cold_open"]["host"] = "Here's the thing nobody tells you."
    rec = voicelint.write_record(s, tmp_path / "hostlint.json")
    assert rec["advisory"] is True and rec["lint"] == "ok" and rec["reason"] is None
    assert {"heres", "nobody"} <= flags_of(rec, "cold_open")
    saved = json.loads((tmp_path / "hostlint.json").read_text())
    assert saved == rec and saved["schema"] == voicelint.RECORD_SCHEMA == "daily-build/hostlint/v1"
    assert saved["result"]["open"] == len(saved["result"]["flags"]) and saved["result"]["kept"] == 0
    assert "Advisory" in saved["report"]


def test_a_tell_in_the_steal_this_line_is_flagged_under_its_own_id(tmp_path):
    s = script()
    clean = voicelint.write_record(s, tmp_path / "a.json")
    assert flags_of(clean, "steal_this") == set()  # positive control: clean as is
    s["steal_this"]["host"] = "Granted, a clever check is the one to steal."
    rec = voicelint.write_record(s, tmp_path / "b.json")
    assert flags_of(rec, "steal_this") >= {"writerly"}
    assert flags_of(rec, "confessional_intro") == flags_of(clean, "confessional_intro")


def test_a_script_with_no_confessional_is_linted_without_that_line(tmp_path):
    with_quote = voicelint.write_record(script(), tmp_path / "a.json")
    without = voicelint.write_record(script("none"), tmp_path / "b.json")
    assert with_quote["result"]["lines"] == 8 and without["result"]["lines"] == 7
    assert all(f["line"] != "confessional_intro" for f in without["result"]["flags"])


def test_the_counts_are_the_writers_own_tell_logic(tmp_path):
    """Same lint, same rules: the record's tells by rule equal what the writer's review counts over the same lines,
    and every person-less flag is one of the writer's sentences of six words or more."""
    lint = writer.load_lint()
    lines = [t for _, t in voicelint.host_lines(script())]
    rec = voicelint.write_record(script(), tmp_path / "hostlint.json")
    by_rule = {}
    for line in lines:
        for rule, _ in lint.tells(line):
            by_rule[rule] = by_rule.get(rule, 0) + 1
    assert {r: n for r, n in rec["result"]["counts"].items() if n} == by_rule
    assert 0 < rec["result"]["counts"]["impersonal"] <= sum(map(lint.long_sentences, lines))  # the fixture has some


def test_the_operators_avoid_words_replace_the_built_in_list(tmp_path):
    s = script()
    s["steal_this"]["host"] = "Granted, a clever check is the one to steal."
    built_in = voicelint.write_record(s, tmp_path / "a.json")
    mine = voicelint.write_record(s, tmp_path / "b.json", ("recipe",))
    assert {f["match"] for f in built_in["result"]["flags"] if f["rule"] == "writerly"} >= {"granted", "clever"}
    writerly = {(f["line"], f["match"]) for f in mine["result"]["flags"] if f["rule"] == "writerly"}
    assert writerly and {m for _, m in writerly} == {"recipe"}  # the story says "the recipe app"
    assert all(line.startswith("story_") or line == "out" for line, _ in writerly)


def test_an_invisible_character_cannot_hide_a_tell(tmp_path):
    s = script()
    s["cold_open"]["host"] = f"No{ZWSP}body saw that coming, and I did not either."
    raw = voicelint.lint_line(s["cold_open"]["host"])
    assert not any(rule == "nobody" for rule, _ in raw)  # positive control: the raw line slips past the lint
    assert "nobody" in flags_of(voicelint.write_record(s, tmp_path / "hostlint.json"), "cold_open")


def test_the_record_replaces_a_stale_one(tmp_path):
    out = tmp_path / "hostlint.json"
    out.write_text(json.dumps({"schema": "stale", "advisory": False}))
    voicelint.write_record(script(), out)
    assert json.loads(out.read_text())["schema"] == "daily-build/hostlint/v1"
    assert [p.name for p in tmp_path.iterdir()] == ["hostlint.json"]  # no temp file left behind


def test_a_lint_that_crashes_writes_an_unavailable_record(tmp_path, monkeypatch):
    def boom(self, lines):
        raise RuntimeError("lint broke /home/someone/secret")
    monkeypatch.setattr(voicelint.Linter, "lint", boom)
    out = tmp_path / "hostlint.json"
    out.write_text("stale")
    rec = voicelint.write_record(script(), out)
    assert rec["lint"] == "unavailable" and rec["result"] is None and rec["advisory"] is True
    assert rec["reason"] == "run"  # a reason is a code
    assert "lint broke" not in json.dumps(rec) and "secret" not in json.dumps(rec)
    assert json.loads(out.read_text()) == rec and "Advisory" in rec["report"]


def test_a_lint_that_ran_has_no_reason(tmp_path):
    assert voicelint.write_record(script(), tmp_path / "hostlint.json")["reason"] is None


def test_a_termination_signal_is_not_swallowed(tmp_path, monkeypatch):
    def stop(self, lines):
        raise KeyboardInterrupt
    monkeypatch.setattr(voicelint.Linter, "lint", stop)
    with pytest.raises(KeyboardInterrupt):
        voicelint.write_record(script(), tmp_path / "hostlint.json")


# ---- the report ----

def test_the_report_counts_each_rule_and_lists_each_tell(tmp_path):
    s = script()
    s["cold_open"]["host"] = "Here's the thing nobody tells you."
    rec = voicelint.write_record(s, tmp_path / "hostlint.json")
    rep = rec["report"].splitlines()
    res = rec["result"]
    assert rep[0].startswith(f"Host-line lint: {res['open']} open tell(s) in {res['lines']} host line(s); ")
    for rule, n in res["counts"].items():
        assert f"{rule} {n}" in rep[0]
    assert "  cold_open: heres: here's" in rep and "  cold_open: nobody: nobody" in rep
    assert len(rep) == len(res["flags"]) + 2 and rep[-1] == "Advisory: nothing here blocks the build."


def test_report_of_a_clean_set_of_lines():
    res = voicelint.Linter().lint([("out", "See you tomorrow.")])
    assert res["open"] == 0
    assert voicelint.report([("out", "See you tomorrow.")], res).splitlines() == [
        "Host-line lint: 0 open tell(s) in 1 host line(s); heres 0, nobody 0, writerly 0, impersonal 0.",
        "Advisory: nothing here blocks the build."]


# ---- open_count: what the voice stage reads ----

def test_open_count_is_the_lints_open_flags(tmp_path):
    clean = voicelint.write_record(script(), tmp_path / "a.json")
    s = script()
    s["cold_open"]["host"] = "Here's the thing nobody tells you."
    tells = voicelint.write_record(s, tmp_path / "b.json")
    assert voicelint.open_count(clean) == clean["result"]["open"] == len(clean["result"]["flags"])
    assert voicelint.open_count(tells) == tells["result"]["open"]
    # the new line adds here's and nobody and loses the old line's one person-less flag ("you" is a person)
    assert "impersonal" in flags_of(clean, "cold_open") and flags_of(tells, "cold_open") == {"heres", "nobody"}
    assert voicelint.open_count(tells) == voicelint.open_count(clean) + 1
    saved = json.loads((tmp_path / "b.json").read_text())
    assert voicelint.open_count(saved) == voicelint.open_count(tells)  # as read back from the file


def test_open_count_is_none_when_the_lint_was_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(voicelint.Linter, "lint", lambda self, lines: (_ for _ in ()).throw(ValueError("x")))
    rec = voicelint.write_record(script(), tmp_path / "hostlint.json")
    assert rec["reason"] == "run" and voicelint.open_count(rec) is None  # None, not 0 and not a KeyError
    assert voicelint.open_count(json.loads((tmp_path / "hostlint.json").read_text())) is None


@pytest.mark.parametrize("rec", [
    {"result": {"open": True}}, {"result": {"open": False}}, {"result": {"open": "3"}}, {"result": {"open": 2.5}},
    {"result": {"open": None}}, {"result": {}}, {"result": None}, {"result": "x"}, {}, None, [], "rec",
])
def test_open_count_is_none_for_anything_but_a_whole_number(rec):
    assert voicelint.open_count(rec) is None


def test_open_count_of_zero_is_zero():
    assert voicelint.open_count({"result": {"open": 0}}) == 0


def test_this_file_has_no_long_dash():
    assert not [c for c in Path(__file__).read_text(encoding="utf-8") if c in LONG_DASHES]
