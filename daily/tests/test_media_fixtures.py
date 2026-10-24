"""The media fixtures: the scripts, the ledger and the voices the media tests share, and the helpers that lay them out
as a work directory. They must model the text half's real shapes, so they are held to its own code: the ledger is what
the real ledger builder yields from stated facts, and the real gate accepts every case with its ledger bound."""
import json
import subprocess

import pytest

from daily import gate as G
from daily import voiceconf
from daily.ledger import resolve_view_id, writer_view
from daily_helpers import good_script, no_confessional_script
from media_helpers import (AGENTS, CASES, DISCLOSURE, NOMINAL_WORK, PR_LINE, HEARD, VOICES, config_in, gate_work,
                           kit_freq, kit_sounds, ledger, make_config, make_kit, make_sound_kit, needs_ffmpeg,
                           pr_records, quote_of_kind, script, settings_for, tone, voices, work_dir)


def verdict(w):
    return json.loads((w / "verdict.json").read_text())


def evidence(v, name):
    return next(c["evidence"] for c in v["checks"] if c["name"] == name)


def quote_in(w, kind):
    return quote_of_kind(json.loads((w / "ledger.json").read_text()), kind)


# ---- the ledger ----

def test_coverage_lists_the_built_ins_first_then_the_adapters_in_their_order():
    cov = ledger()["coverage"]
    assert list(cov) == ["claude_code", "codex", "my_agent", "night_bot"]  # dict equality ignores order: check it here
    assert cov["night_bot"].startswith("unavailable:") and all(cov[h] == "ok" for h in list(cov)[:3])


def test_the_scoreboard_counts_are_the_facts():
    sb = ledger()["scoreboard"]
    assert (sb["prs_merged"], sb["arcs_closed"]) == (16, 6)  # 3 listed PRs and 13 unlisted ones count; 6 closed tasks
    assert sb["sessions"] == {"claude_code": 11, "codex": 2, "my_agent": 1}
    assert sb["subagents"] == {"claude_code": 92, "codex": 3, "my_agent": 0}
    assert "night_bot" not in sb["sessions"]  # an unavailable agent is in no scoreboard map
    assert "handoffs" not in sb


def test_the_ledger_holds_one_quote_of_each_kind_from_the_public_lane():
    led = ledger()
    assert [q["kind"] for q in led["quotes"]] == ["transcript", "pr"]
    t, p = quote_of_kind(led, "transcript"), quote_of_kind(led, "pr")
    assert (t["text"], p["text"]) == (HEARD, PR_LINE)
    for q in (t, p):
        assert (q["harness"], q["lane"], q["disclosure"]) == ("codex", "recipe-box", "public")
        assert q["source"]["path"].startswith(NOMINAL_WORK + "/sources/")
    assert t["source"]["line"] == 3 and p["source"]["record"] == "pr:recipe-box#21"


def test_private_names_are_the_headline_and_unlisted_repos():
    led = ledger()
    assert led["private_names"] == ["draft-pile", "side-quest"]
    assert led["excluded"]["unlisted_lane"] > 0 and led["tz"] == "America/Los_Angeles"


def test_the_ledger_helper_hands_out_independent_copies():
    a = ledger()
    a["scoreboard"]["prs_merged"] = 99
    assert ledger()["scoreboard"]["prs_merged"] == 16


def test_the_pr_records_are_what_the_collect_stage_writes(tmp_path):
    w = work_dir(tmp_path, "pr", gated=False)
    written = [json.loads(line) for line in (w / "sources" / "github.jsonl").read_text().splitlines()]
    assert written == pr_records() and len(written) == 1
    assert written[0]["repo"] == "demo-owner/recipe-box" and PR_LINE in written[0]["body"]


def test_story_ids_resolve_through_the_ledger():
    led = ledger()
    assert resolve_view_id(led, "pr:recipe-box#21") == "pr:recipe-box#21"
    assert resolve_view_id(led, "lane:private-1") == "lane:side-quest"  # the headline alias
    assert resolve_view_id(led, "session:0f3a9c21") == "session:0f3a9c21"
    assert resolve_view_id(led, "pr:recipe-box#99") is None  # negative control


def test_the_writer_view_hides_the_headline_lane_name():
    view = json.dumps(writer_view(ledger()))
    assert "side-quest" not in view and "a private side project" in view  # positive control: its label shows


# ---- the voices ----

def test_the_fixture_voices_cover_every_agent_in_the_coverage():
    v = voices()
    assert voiceconf.harnesses(v) == AGENTS
    assert set(ledger()["coverage"]) <= set(v)
    for h in AGENTS:
        voiceconf.voice_for(h, v)  # every agent voice is active and valid


def test_the_fixture_voices_load_from_a_config_folder(tmp_path):
    d = make_config(tmp_path)
    assert voiceconf.load_voices(d / "voices.json") == voices()
    assert json.loads((d / "voices.json").read_text()) == VOICES


# ---- the scripts ----

@pytest.mark.parametrize("case", CASES)
def test_each_script_is_valid_text_half_shape_with_steal_this(case):
    s = script(case)
    assert G.validate_script(s, "2026-10-16") == []
    assert set(s) == set(good_script())  # the same keys the text half's helper builds, steal_this included
    assert s["steal_this"]["host"].strip() and 0 < len(s["steal_this"]["on_screen"]) <= 42
    assert s["description"].endswith(DISCLOSURE)


@pytest.mark.parametrize("case", CASES)
def test_each_script_sits_inside_the_word_budget_with_room_to_spare(case):
    n = G.host_words(script(case))  # the gate's own counter
    assert G.WORDS_MIN + 5 <= n <= G.WORDS_MAX - 5, n


@pytest.mark.parametrize("case", CASES)
def test_no_script_holds_a_digit(case):
    """The scoreboard line must have none, and every other number would need grounding: there are none at all."""
    assert not any(ch.isdigit() for ch in G.shipped_text(script(case)))


def test_the_three_scripts_differ_only_in_the_confessional():
    base, pr_s, none = (script(c) for c in CASES)
    rest = lambda s: {k: v for k, v in s.items() if k != "confessional"}  # noqa: E731
    assert rest(base) == rest(pr_s) == rest(none)
    assert len({json.dumps(s["confessional"]) for s in (base, pr_s, none)}) == 3  # positive control: they do differ
    assert none["confessional"] == no_confessional_script()["confessional"]  # the text half's own empty form
    led = ledger()
    by_id = {q["id"]: q for q in led["quotes"]}
    assert by_id[base["confessional"]["quote_id"]]["kind"] == "transcript"
    assert by_id[pr_s["confessional"]["quote_id"]]["kind"] == "pr"
    assert led["quotes"][0]["id"] == base["confessional"]["quote_id"]  # the default case's quote leads the ledger


# ---- the verdicts ----

@pytest.mark.parametrize("case", CASES)
def test_every_case_gets_a_verdict_the_gate_accepts_with_the_ledger_bound(tmp_path, case):
    w = work_dir(tmp_path, case)
    assert G.accepts(w / "verdict.json", w / "script.json", G.REQUIRED, ledger_path=w / "ledger.json") == (True, "ok")
    v = verdict(w)
    assert v["pass"] is True and [c["name"] for c in v["checks"]] == list(G.REQUIRED)
    assert v["dry"] is False and v["at"] and len(v["ledger_sha256"]) == 64


def test_dry_marks_the_verdict_and_ungated_leaves_none(tmp_path):
    w = work_dir(tmp_path / "dry", dry=True)
    assert verdict(w)["dry"] is True
    bare = work_dir(tmp_path / "bare", gated=False)
    assert not (bare / "verdict.json").exists() and (bare / "script.json").is_file()


def test_the_transcript_case_quotes_a_codex_rollout_line(tmp_path):
    w = work_dir(tmp_path, "transcript")
    q = quote_in(w, "transcript")
    assert json.loads((w / "script.json").read_text())["confessional"]["quote_id"] == q["id"]
    assert evidence(verdict(w), "quote_verbatim") == "matched rollout-2026-10-16.jsonl:3"
    assert q["source"]["path"].startswith(str(w) + "/sources/")


def test_the_pr_case_quotes_the_pr_description_at_merge_time(tmp_path):
    w = work_dir(tmp_path, "pr")
    q = quote_in(w, "pr")
    item = next(i for i in json.loads((w / "ledger.json").read_text())["items"] if i["id"] == q["source"]["record"])
    assert q["ts"] == item["ts"] == "2026-10-16T21:03:00Z"  # a PR quote's ts is the merge time
    assert q["source"] == {"path": str(w / "sources" / "github.jsonl"), "line": 1, "record": "pr:recipe-box#21"}
    assert json.loads((w / "script.json").read_text())["confessional"]["quote_id"] == q["id"]
    assert evidence(verdict(w), "quote_verbatim") == "matched github.jsonl:1"


def test_the_none_case_has_no_confessional_beat(tmp_path):
    w = work_dir(tmp_path, "none")
    s = json.loads((w / "script.json").read_text())
    assert s["confessional"] == {"host_intro": "", "quote_id": ""}
    assert evidence(verdict(w), "quote_verbatim") == G.NO_CONFESSIONAL
    assert evidence(verdict(w), "quote_in_ledger") == G.NO_CONFESSIONAL
    assert json.loads((w / "ledger.json").read_text())["quotes"]  # quotes existed; the writer chose none


@pytest.mark.parametrize("case", CASES)
def test_a_ledger_changed_after_the_gate_is_not_accepted(tmp_path, case):  # negative control
    w = work_dir(tmp_path, case)
    led = json.loads((w / "ledger.json").read_text())
    led["scoreboard"]["prs_merged"] += 1
    (w / "ledger.json").write_text(json.dumps(led))
    assert G.accepts(w / "verdict.json", w / "script.json", G.REQUIRED, ledger_path=w / "ledger.json") == (
        False, "ledger_mismatch")


@pytest.mark.parametrize("case,source,word", [("transcript", "rollout-2026-10-16.jsonl", "rounded"),
                                              ("pr", "github.jsonl", "compares")])
def test_the_gate_really_reads_the_source_files(tmp_path, case, source, word):  # negative control
    w = work_dir(tmp_path, case, gated=False)
    assert gate_work(w).ok  # positive control: the untouched sources pass
    path = w / "sources" / source
    text = path.read_text()
    assert word in text
    path.write_text(text.replace(word, word[:-1]))  # the quoted sentence is no longer on its line
    res = gate_work(w)
    assert not res.ok and res.reason == "quote_verbatim"


@pytest.mark.parametrize("case", CASES)
def test_the_real_built_in_scanners_find_nothing_in_any_case(tmp_path, case):
    settings = settings_for(tmp_path / "scan-cfg")
    deps = G.default_deps(settings)
    assert deps.slop_scan("We delve into the build.")  # positive control: the real slop gate is live
    assert deps.redactor_decision("mail me at pat" + "@" + "corp-mail.test")[0] != "PUBLISH"  # and the redactor
    w = work_dir(tmp_path, case, gated=False)
    v = G.gate((w / "script.json").read_bytes(), json.loads((w / "ledger.json").read_text()),
               config_in(tmp_path / "scan-lanes"), deps, work_dir=w, settings=settings)
    assert [c["name"] for c in v["checks"] if not c["pass"]] == [] and v["pass"] is True


# ---- audio and kit helpers ----

def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=channels,sample_rate:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    return float(j["format"]["duration"]), j["streams"][0]["channels"], int(j["streams"][0]["sample_rate"])


@needs_ffmpeg
def test_tone_makes_a_tiny_wav_of_the_asked_length_and_format(tmp_path):
    dur, ch, sr = probe(tone(tmp_path / "new" / "dir" / "t.wav", 0.5))
    assert abs(dur - 0.5) < 0.02 and (ch, sr) == (2, 48000)
    dur, ch, sr = probe(tone(tmp_path / "m.wav", 1.0, 330, sr=24000, ch=1))
    assert abs(dur - 1.0) < 0.02 and (ch, sr) == (1, 24000)


def test_the_stub_kit_has_the_fonts_and_gsap_compose_needs(tmp_path):
    kit = make_kit(tmp_path)
    assert (kit / "gsap.min.js").is_file()
    assert {p.name for p in (kit / "fonts").iterdir()} == {"Anton.ttf", "InterVariable.ttf"}
    assert make_kit(tmp_path) == kit  # a second call is harmless
    assert all(p.stat().st_size < 64 for p in kit.rglob("*") if p.is_file())  # stubs, never real media


def test_every_kit_sound_has_its_own_test_tone():
    rels = kit_sounds()
    assert len({kit_freq(r) for r in rels}) == len(rels)
    assert "sound/entrant_night_bot.wav" not in rels  # an agent without a sting, on purpose


@needs_ffmpeg
def test_the_sound_kit_holds_every_kit_sound(tmp_path):
    kit = make_sound_kit(tmp_path)
    for rel in kit_sounds():
        dur, ch, sr = probe(kit / rel)
        assert (ch, sr) == (2, 48000) and dur > 0.2, rel
