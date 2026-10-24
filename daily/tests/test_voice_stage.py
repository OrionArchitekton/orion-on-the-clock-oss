"""The voice stage: which lines are said, by whom and with what settings, the silent stand-in that lets compose and
render run without a GPU, and the stage itself (settings and the text gate first, voices.json read once and hashed, one
Chatterbox run proved by its results file, the agent chains, Whisper word times, the coverage floor). Chatterbox and
Whisper are always faked here; the tests that prove what ffmpeg does to audio need ffmpeg and are skipped without it."""
import ast
import copy
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

import media_helpers as M
from daily import align, compose, files, gate, paths, tts_plan, tts_remote, tts_runner, voice, voicelint, voiceconf
from daily.stage import Ctx

DAY = M.DAY
CASES = M.CASES
needs_ffmpeg = M.needs_ffmpeg
HOST_TAIL = ["steal_this", "confessional_intro", "confessional", "out"]
EXPECTED_TAIL = {"transcript": HOST_TAIL, "pr": HOST_TAIL, "none": ["steal_this", "out"]}
HERE = Path(__file__).resolve()
LONG_DASHES = "".join(chr(c) for c in (0x2013, 0x2014, 0x2015))  # built from code points: no dash in this file
REAL = '[tts]\npython = "/opt/tts/bin/python"\n\n[whisper]\npython = "/opt/whisper/bin/python"\n'
EXAMPLE_VOICES = voiceconf.load_voices(paths.REPO / "config.example" / "voices.json")
FIXTURE_VOICES = voiceconf.parse_voices_bytes(json.dumps(M.VOICES).encode("utf-8"))
# every agent voice the repository ships or tests with: (source, agent id)
ALL_AGENT_VOICES = [("example", h) for h in voiceconf.harnesses(EXAMPLE_VOICES)] + [("fixture", h) for h in M.AGENTS]
STATE = {}


def agent_voice(source, harness):
    return (EXAMPLE_VOICES if source == "example" else FIXTURE_VOICES)[harness]


@pytest.fixture(autouse=True)
def _config(monkeypatch, tmp_path_factory):
    """Every test gets a config folder (settings with the real-run interpreters, voices.json) at ORION_CLOCK_CONFIG,
    outside the test's own tmp_path so a test can list that folder."""
    d, settings, _cfg = M.use_config(monkeypatch, tmp_path_factory.mktemp("cfg-root"), extra=REAL)
    STATE.update(dir=d, settings=settings)
    yield
    STATE.clear()


def use(monkeypatch, tmp_path, name="alt", **kw):
    """Point the stage at another config folder (make_config's keywords); returns its Settings."""
    d, settings, _cfg = M.use_config(monkeypatch, tmp_path / name, **kw)
    STATE.update(dir=d, settings=settings)
    return settings


def VP():
    return paths.voices_path()


def voices_sha():
    return files.sha256_bytes(VP().read_bytes())


def ctx(w, dry=False, settings="current"):
    return Ctx(DAY, w, None, dry, STATE["settings"] if settings == "current" else settings)


def lines_for(case, mutate=None, voices=None, ledger=None):
    script = M.script(case)
    led = ledger if ledger is not None else M.ledger()
    if mutate:
        mutate(script)
    return voice.build_lines(script, led, voices or M.voices())


def quote(ledger, qid):
    return next(q for q in ledger["quotes"] if q["id"] == qid)


def tq():
    """The transcript quote's id in the nominal ledger."""
    return M.quote_of_kind(M.ledger(), "transcript")["id"]


def pq():
    return M.quote_of_kind(M.ledger(), "pr")["id"]


# ---- which lines, in which order ----

@pytest.mark.parametrize("case", CASES)
def test_line_ids_run_in_speaking_order(case):
    ids = [ln["id"] for ln in lines_for(case)]
    n_story = len(M.script(case)["story"]["host"])
    assert ids == ["cold_open", "scoreboard", *[f"story_{i}" for i in range(n_story)], *EXPECTED_TAIL[case]]
    assert n_story >= 2  # the story lines are a real run, not one stand-in


@pytest.mark.parametrize("case", CASES)
def test_host_line_ids_and_order_equal_the_lints(case):
    script = M.script(case)
    host = [(ln["id"], ln["text"]) for ln in lines_for(case) if ln["voice"] == "host"]
    assert host == [(i, t.strip()) for i, t in voicelint.host_lines(script)]


@pytest.mark.parametrize("case", CASES)
def test_scenes_name_the_beat_each_line_belongs_to(case):
    scenes = {ln["id"]: ln["scene"] for ln in lines_for(case)}
    for line_id, scene in scenes.items():
        if line_id.startswith("story_"):
            assert scene == "story"
        elif line_id == "confessional_intro":
            assert scene == "confessional"
        else:
            assert scene == line_id  # cold_open, scoreboard, steal_this, confessional, out
    assert scenes["steal_this"] == "steal_this"


def test_steal_this_is_a_host_line_with_the_hosts_settings():
    voices = M.voices()
    steal = next(ln for ln in lines_for("transcript", voices=voices) if ln["id"] == "steal_this")
    assert steal["voice"] == "host" and steal["params"] == voices["host"]["params"]
    assert steal["text"] == M.script()["steal_this"]["host"].strip()


@pytest.mark.parametrize("case", CASES)
def test_lines_pass_the_runners_structural_checks(case):
    voices = M.voices()
    lines = lines_for(case, voices=voices)
    tts_plan.check_lines(lines, voices.keys())
    json.dumps(lines)  # lines.json is plain data
    assert all({"id", "scene", "voice", "text", "params"} <= set(ln) for ln in lines)


# ---- the voice settings ----

def test_cold_open_uses_the_cold_open_settings_and_the_rest_the_normal_ones():
    voices = M.voices()
    host = voices["host"]
    assert host["params_cold_open"] != host["params"]  # the check below cannot pass by accident
    for ln in lines_for("transcript", voices=voices):
        if ln["voice"] != "host":
            continue
        assert ln["params"] == (host["params_cold_open"] if ln["id"] == "cold_open" else host["params"]), ln["id"]


def test_a_line_owns_its_settings():
    voices = M.voices()
    before = copy.deepcopy(voices)
    for ln in lines_for("transcript", voices=voices):
        ln["params"]["exaggeration"] = 9.0
    assert voices == before


# ---- the confessional ----

@pytest.mark.parametrize("case,kind", [("transcript", "transcript"), ("pr", "pr")])
def test_confessional_takes_its_words_voice_and_provenance_from_the_ledger(case, kind):
    led = M.ledger()
    script = M.script(case)
    q = quote(led, script["confessional"]["quote_id"])
    voices = M.voices()
    conf = next(ln for ln in lines_for(case, voices=voices, ledger=led) if ln["id"] == "confessional")
    assert q["kind"] == kind
    assert conf["text"] == q["text"] and conf["voice"] == q["harness"] == "codex"
    assert conf["params"] == voices["codex"]["params"] and conf["scene"] == "confessional"
    assert (conf["quote_id"], conf["quote_ts"], conf["quote_kind"]) == (q["id"], q["ts"], kind)


def test_the_two_quote_kinds_come_through_as_different_lines():
    conf = {c: next(ln for ln in lines_for(c) if ln["id"] == "confessional") for c in ("transcript", "pr")}
    assert conf["transcript"]["quote_kind"] == "transcript" and conf["pr"]["quote_kind"] == "pr"
    assert conf["transcript"]["text"] != conf["pr"]["text"]


def test_confessional_text_follows_the_ledger_not_the_script():
    led = M.ledger()
    script = M.script()
    quote(led, script["confessional"]["quote_id"])["text"] = "A different sentence from the ledger."
    conf = next(ln for ln in voice.build_lines(script, led, M.voices()) if ln["id"] == "confessional")
    assert conf["text"] == "A different sentence from the ledger."


def test_unknown_quote_id_fails():
    s = M.script()
    s["confessional"]["quote_id"] = "q:codex:ffffffff"
    with pytest.raises(LookupError):
        voice.build_lines(s, M.ledger(), M.voices())


def test_inactive_harness_has_no_voice():
    voices = M.voices()
    voices["codex"]["active"] = False
    with pytest.raises(voiceconf.VoiceConfigError):
        voice.build_lines(M.script(), M.ledger(), voices)


@pytest.mark.parametrize("harness", ["unknown", "host", ""])
def test_unknown_harness_has_no_voice(harness):
    """"host" is a voice but never an agent: a quote is never said in the narrator's voice."""
    led = M.ledger()
    quote(led, tq())["harness"] = harness
    with pytest.raises(voiceconf.VoiceConfigError):
        voice.build_lines(M.script(), led, M.voices())


@pytest.mark.parametrize("kind", ["", "tweet", None])
def test_a_quote_kind_the_render_cannot_label_fails_closed(kind):
    led = M.ledger()
    quote(led, tq())["kind"] = kind
    with pytest.raises(voice.VoiceError):
        voice.build_lines(M.script(), led, M.voices())


def test_an_empty_quote_text_fails_closed():
    led = M.ledger()
    quote(led, tq())["text"] = "   "
    with pytest.raises(voice.VoiceError):
        voice.build_lines(M.script(), led, M.voices())


# ---- no confessional ----

def test_no_quote_means_no_confessional_lines_even_with_quotes_in_the_ledger():
    led = M.ledger()
    assert led["quotes"]  # the ledger could have supplied one
    ids = [ln["id"] for ln in lines_for("none", ledger=led)]
    assert "confessional" not in ids and "confessional_intro" not in ids
    assert ids[-2:] == ["steal_this", "out"]


def test_no_quote_looks_up_no_harness_voice():
    voices = M.voices()
    for h in M.AGENTS:
        voices[h]["active"] = False
    assert [ln["id"] for ln in lines_for("none", voices=voices)][-2:] == ["steal_this", "out"]
    with pytest.raises(voiceconf.VoiceConfigError):  # and the same voices do stop a script that has a quote
        lines_for("transcript", voices=voices)


def test_no_quote_does_not_need_a_ledger_quote_list():
    assert [ln["id"] for ln in lines_for("none", ledger={})][-1] == "out"


@pytest.mark.parametrize("quote_id,intro", [
    ("TQ", ""),            # a quote with nothing to introduce it
    ("TQ", "   "),         # blank counts as empty, after strip
    ("", "And Codex had thoughts about that."),  # an introduction with nothing to play
    ("  ", "And Codex had thoughts about that."),
])
def test_quote_and_introduction_must_come_together(quote_id, intro):
    qid = tq() if quote_id == "TQ" else quote_id

    def mutate(s):
        s["confessional"]["quote_id"], s["confessional"]["host_intro"] = qid, intro
    with pytest.raises(voice.VoiceError):
        lines_for("transcript", mutate)


def test_a_blank_pair_is_no_confessional():
    def mutate(s):
        s["confessional"]["quote_id"], s["confessional"]["host_intro"] = " ", "\n"
    assert [ln["id"] for ln in lines_for("transcript", mutate)][-2:] == ["steal_this", "out"]


# ---- empty lines ----

@pytest.mark.parametrize("path", [("cold_open", "host"), ("scoreboard", "host"), ("steal_this", "host"),
                                  ("out", "host"), ("story", "host", 0), ("story", "host", 2)])
def test_an_empty_host_line_fails_closed(path):
    def mutate(s):
        node = s
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = "  \n"
    with pytest.raises(voice.VoiceError):
        lines_for("transcript", mutate)


def test_a_story_with_no_lines_fails_closed():
    def mutate(s):
        s["story"]["host"] = []
    with pytest.raises(voice.VoiceError):
        lines_for("transcript", mutate)


# ---- what is spoken is what the gate scanned ----

def assert_all_scanned(script, ledger, lines):
    """Every spoken word is in a field the text gate scans: a host line is one of gate.shipped_fields, and the
    confessional is the quote text that gate.shipped_text takes in."""
    fields = {f.strip() for f in gate.shipped_fields(script)}
    for ln in lines:
        if ln["voice"] == "host":
            assert ln["text"] in fields, f"{ln['id']} is spoken but not scanned"
        else:
            q = quote(ledger, script["confessional"]["quote_id"])
            assert ln["id"] == "confessional" and ln["quote_id"] == q["id"]
            assert ln["text"] == q["text"].strip()
            assert ln["text"] in gate.shipped_text(script, q["text"])
            assert ln["text"] not in gate.shipped_text(script)  # scanned only because the quote text is passed in


@pytest.mark.parametrize("case", CASES)
def test_every_spoken_line_is_text_the_gate_scans(case):
    script, led = M.script(case), M.ledger()
    assert_all_scanned(script, led, voice.build_lines(script, led, M.voices()))


@pytest.mark.parametrize("case", CASES)
def test_the_host_lines_are_exactly_the_gates_host_lines(case):
    """Neither side may add or drop a host line the other has: the gate counts and scans gate.host_lines."""
    script = M.script(case)
    spoken = [ln["text"] for ln in lines_for(case) if ln["voice"] == "host"]
    assert spoken == [t.strip() for t in gate.host_lines(script) if t.strip()]


def test_the_guard_catches_a_host_line_the_gate_does_not_scan(monkeypatch):
    script, led = M.script(), M.ledger()
    real = voicelint.host_lines
    unscanned = script["story_item_id"]
    assert unscanned.strip() not in {f.strip() for f in gate.shipped_fields(script)}
    monkeypatch.setattr(voicelint, "host_lines", lambda s: [(i, unscanned if i == "out" else t) for i, t in real(s)])
    lines = voice.build_lines(script, led, M.voices())
    with pytest.raises(AssertionError, match="out is spoken but not scanned"):
        assert_all_scanned(script, led, lines)


def test_the_guard_catches_a_confessional_that_is_not_the_scanned_quote():
    script, led = M.script(), M.ledger()
    lines = voice.build_lines(script, led, M.voices())
    next(ln for ln in lines if ln["id"] == "confessional")["text"] = "Words the gate never read."
    with pytest.raises(AssertionError):
        assert_all_scanned(script, led, lines)


# ---- whitespace and tokens ----

def test_padding_on_a_host_line_or_the_quote_is_stripped():
    def pad(t):
        return f"  \n\t{t} \n "

    script, led = M.script(), M.ledger()
    script["cold_open"]["host"] = pad(script["cold_open"]["host"])
    script["scoreboard"]["host"] = pad(script["scoreboard"]["host"])
    script["story"]["host"] = [pad(t) for t in script["story"]["host"]]
    script["steal_this"]["host"] = pad(script["steal_this"]["host"])
    script["confessional"]["host_intro"] = pad(script["confessional"]["host_intro"])
    script["out"]["host"] = pad(script["out"]["host"])
    q = quote(led, script["confessional"]["quote_id"])
    clean_quote = q["text"]
    q["text"] = pad(clean_quote)
    plain = M.script()
    want = {"cold_open": plain["cold_open"]["host"], "scoreboard": plain["scoreboard"]["host"],
            "steal_this": plain["steal_this"]["host"], "confessional_intro": plain["confessional"]["host_intro"],
            "out": plain["out"]["host"], "confessional": clean_quote,
            **{f"story_{i}": t for i, t in enumerate(plain["story"]["host"])}}
    lines = voice.build_lines(script, led, M.voices())
    assert {ln["id"]: ln["text"] for ln in lines} == want
    assert all(ln["text"] == ln["text"].strip() for ln in lines)


@needs_ffmpeg
def test_dry_tokens_are_the_lines_own_words_even_across_a_newline_and_double_spaces(tmp_path):
    led = M.ledger()
    q = quote(led, tq())
    q["text"] = "First, I ran it.\nThen  I failed the build  on purpose.\n\nIt   was right."
    lines = voice.build_lines(M.script(), led, M.voices())
    conf = next(ln for ln in lines if ln["id"] == "confessional")
    assert "\n" in conf["text"] and "  " in conf["text"]  # the odd spacing survives the strip
    voice.dry_voice(lines, tmp_path)
    for ln in lines:
        rec = json.loads((tmp_path / f"{ln['id']}.words.json").read_text())
        spoken_tokens = [w["word"] for w in rec["words"]]
        assert spoken_tokens == ln["text"].split() == align.match_tokens(ln["text"], [])[0], ln["id"]
        assert "" not in spoken_tokens and not any(re.search(r"\s", t) for t in spoken_tokens)
    rec = json.loads((tmp_path / "confessional.words.json").read_text())
    assert len(rec["words"]) == 14 and rec["dur"] == round(0.3 * 14, 3)  # counted by eye: 14 words, not 16 or 12


# ---- compose.quote_by_id ----

def test_quote_by_id_finds_the_quote_and_names_a_missing_one():
    led = M.ledger()
    assert compose.quote_by_id(led, pq())["kind"] == "pr"
    with pytest.raises(LookupError):
        compose.quote_by_id(led, "q:codex:00000000")
    with pytest.raises(LookupError):
        compose.quote_by_id({}, pq())


def test_quote_by_id_fails_closed_on_a_duplicate_id():
    """The gate verified one quote under this id; with two, which one it checked is unknown (the gate itself takes
    a quote only when exactly one matches)."""
    led = M.ledger()
    twin = copy.deepcopy(quote(led, tq()))
    twin["text"] = "Another sentence under the same id."
    led["quotes"].append(twin)
    with pytest.raises(LookupError, match="more than once"):
        compose.quote_by_id(led, twin["id"])
    assert compose.quote_by_id(led, pq())["kind"] == "pr"  # the other ids still resolve
    with pytest.raises(LookupError):
        voice.build_lines(M.script(), led, M.voices())


@pytest.mark.parametrize("quotes", [None, "PQ", {"id": "PQ"}, 7])
def test_quote_by_id_fails_closed_on_a_ledger_without_a_quote_list(quotes):
    qid = pq()
    shaped = qid if quotes == "PQ" else {"id": qid} if isinstance(quotes, dict) else quotes
    with pytest.raises(LookupError):
        compose.quote_by_id({"quotes": shaped}, qid)


def test_quote_by_id_skips_entries_that_are_not_quotes():
    qid = pq()
    led = {"quotes": [qid, None, {"id": qid, "kind": "pr"}]}
    assert compose.quote_by_id(led, qid)["kind"] == "pr"


# ---- dry voicing ----

def expected_dur(text):
    return round(max(0.6, 0.3 * len(text.split())), 3)


@needs_ffmpeg
@pytest.mark.parametrize("case", CASES)
def test_dry_voice_writes_silence_and_word_times_for_every_line(case, tmp_path):
    lines = lines_for(case)
    voice.dry_voice(lines, tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [f"{ln['id']}.wav" for ln in lines] + [f"{ln['id']}.words.json" for ln in lines])
    for ln in lines:
        rec = json.loads((tmp_path / f"{ln['id']}.words.json").read_text())
        toks = ln["text"].split()
        assert (rec["id"], rec["voice"], rec["text"]) == (ln["id"], ln["voice"], ln["text"])
        assert rec["source"] == "dry" and rec["coverage"] == 1.0
        assert rec["dur"] == expected_dur(ln["text"])
        assert [w["word"] for w in rec["words"]] == toks and len(toks) >= 1
        assert abs(voice.wav_seconds(tmp_path / f"{ln['id']}.wav") - rec["dur"]) < 0.01


@needs_ffmpeg
def test_dry_word_times_run_forward_inside_the_line(tmp_path):
    lines = lines_for("transcript")
    voice.dry_voice(lines, tmp_path)
    for ln in lines:
        rec = json.loads((tmp_path / f"{ln['id']}.words.json").read_text())
        prev = 0.0
        for w in rec["words"]:
            assert set(w) == {"word", "start", "end"} and prev <= w["start"] < w["end"]
            prev = w["end"]
        assert prev <= rec["dur"]
        assert rec["words"][0]["start"] == 0.0 and rec["words"][1]["start"] == voice.DRY_WORD_S


@needs_ffmpeg
def test_story_line_length_comes_from_its_own_words(tmp_path):
    lines = lines_for("transcript")
    voice.dry_voice(lines, tmp_path)
    line = next(ln for ln in lines if ln["id"] == "story_1")
    rec = json.loads((tmp_path / "story_1.words.json").read_text())
    assert len(rec["words"]) == len(line["text"].split())
    assert rec["dur"] == expected_dur(line["text"]) == round(0.3 * len(line["text"].split()), 3)


@needs_ffmpeg
def test_a_one_word_line_still_gets_the_minimum_length(tmp_path):
    line = {"id": "one", "scene": "story", "voice": "host", "text": "Hi.", "params": {}}
    voice.dry_voice([line], tmp_path)
    rec = json.loads((tmp_path / "one.words.json").read_text())
    assert rec["dur"] == 0.6 and len(rec["words"]) == 1
    assert abs(voice.wav_seconds(tmp_path / "one.wav") - 0.6) < 0.01


def test_word_files_go_through_the_atomic_writer(tmp_path, monkeypatch):
    fast_audio(monkeypatch)
    seen = []
    real = voice.files.write_json
    monkeypatch.setattr(voice.files, "write_json", lambda p, o: (seen.append(Path(p).name), real(p, o)))
    lines = lines_for("pr")
    voice.dry_voice(lines, tmp_path)
    assert seen == [f"{ln['id']}.words.json" for ln in lines]
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]  # no temp file left behind


@needs_ffmpeg
def test_dry_work_helper_voices_every_case(dry_case):
    case, w = dry_case
    lines = json.loads((w / "voice" / "lines.json").read_text())
    for ln in lines:
        assert (w / "voice" / f"{ln['id']}.wav").is_file() and (w / "voice" / f"{ln['id']}.words.json").is_file()
    assert ("confessional" in [ln["id"] for ln in lines]) == (case != "none")


# ---- subprocess discipline ----

def capture_run(monkeypatch, stdout=""):
    """A stand-in for subprocess.run that records each call; the fake ffmpeg writes its output file (the last argument)."""
    calls = []

    def fake(argv, **kw):
        calls.append((argv, kw))
        if argv[0] == "ffmpeg":
            Path(argv[-1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(voice.subprocess, "run", fake)
    return calls


def test_ffmpeg_and_ffprobe_run_as_argv_lists_with_a_timeout(monkeypatch, tmp_path):
    calls = capture_run(monkeypatch, stdout="1.5\n")
    voice.silent_wav(tmp_path / "a.wav", 1.25)
    assert voice.wav_seconds(tmp_path / "a.wav") == 1.5
    assert [c[0][0] for c in calls] == ["ffmpeg", "ffprobe"]
    for argv, kw in calls:
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        assert not kw.get("shell") and kw.get("check") is True
        assert 0 < kw["timeout"] <= 120
    assert "1.250" in calls[0][0] and (tmp_path / "a.wav").is_file()
    assert Path(calls[0][0][-1]).parent == tmp_path and Path(calls[0][0][-1]) != tmp_path / "a.wav"  # a temp, then renamed


def probe_audio(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                          "stream=channels,sample_rate", "-of", "json", str(path)],
                         check=True, capture_output=True, text=True, timeout=60).stdout
    return json.loads(out)["streams"]


@needs_ffmpeg
def test_the_silent_stand_in_is_mono_at_the_voices_sample_rate(tmp_path):
    voice.silent_wav(tmp_path / "s.wav", 0.8)
    (stream,) = probe_audio(tmp_path / "s.wav")
    assert stream["channels"] == 1 and int(stream["sample_rate"]) == 24000 == voice.SR
    assert abs(voice.wav_seconds(tmp_path / "s.wav") - 0.8) < 0.01


@pytest.mark.parametrize("failure", [
    subprocess.CalledProcessError(1, "ffmpeg"),
    subprocess.TimeoutExpired("ffmpeg", 120),
    KeyboardInterrupt(),
])
def test_a_failing_ffmpeg_leaves_no_final_file_and_no_temp(monkeypatch, tmp_path, failure):
    """ffmpeg is killed or fails halfway: it has written part of the temp file, and nothing may sit at the final path."""
    def half_written(argv, **kw):
        Path(argv[-1]).write_bytes(b"RIFF-truncated")
        raise failure
    monkeypatch.setattr(voice.subprocess, "run", half_written)
    with pytest.raises(type(failure)):
        voice.silent_wav(tmp_path / "line.wav", 1.0)
    assert list(tmp_path.iterdir()) == []


def test_a_failing_ffmpeg_leaves_an_earlier_take_as_it_was(monkeypatch, tmp_path):
    (tmp_path / "line.wav").write_bytes(b"earlier take")

    def half_written(argv, **kw):
        Path(argv[-1]).write_bytes(b"RIFF-truncated")
        raise subprocess.CalledProcessError(1, argv)
    monkeypatch.setattr(voice.subprocess, "run", half_written)
    with pytest.raises(subprocess.CalledProcessError):
        voice.silent_wav(tmp_path / "line.wav", 1.0)
    assert [p.name for p in tmp_path.iterdir()] == ["line.wav"]
    assert (tmp_path / "line.wav").read_bytes() == b"earlier take"


@needs_ffmpeg
def test_a_successful_silent_wav_replaces_an_earlier_take_and_leaves_no_temp(tmp_path):
    (tmp_path / "line.wav").write_bytes(b"earlier take")
    voice.silent_wav(tmp_path / "line.wav", 0.7)
    assert [p.name for p in tmp_path.iterdir()] == ["line.wav"]
    assert abs(voice.wav_seconds(tmp_path / "line.wav") - 0.7) < 0.01


def test_a_missing_ffmpeg_stops_the_voicing(monkeypatch, tmp_path):
    def boom(argv, **kw):
        raise FileNotFoundError(argv[0])
    monkeypatch.setattr(voice.subprocess, "run", boom)
    with pytest.raises(FileNotFoundError):
        voice.dry_voice(lines_for("none"), tmp_path)
    assert list(tmp_path.iterdir()) == []  # no words file for audio that was never made


# ---- the module's own shape ----

def module_source(name):
    return (HERE.parent.parent / name).read_text(encoding="utf-8")


def test_voice_takes_no_lock_and_imports_no_fcntl():
    """The runner owns the shared TTS lock for the whole synthesis; a second flock here would make it wait out its
    1800 s and fail."""
    tree = ast.parse(module_source("voice.py"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert "fcntl" not in imported
    assert not hasattr(voice, "TTS_LOCK") and not hasattr(voice, "LOCK_WAIT_S")
    assert not re.search(r"flock|lockf|TtsLock", module_source("voice.py"))


def test_voice_keeps_its_constants():
    assert voice.TEXT_CHECKS is gate.REQUIRED
    assert voice.TTS_RUNNER.name == "tts_runner.py" and voice.TTS_RUNNER.is_file()
    assert voice.SR == 24000 and voice.DRY_WORD_S == 0.3
    assert voice.TTS_TIMEOUT_S > 0 and issubclass(voice.VoiceError, RuntimeError)
    assert not hasattr(voice, "TTS_PY")  # the interpreter comes from settings.toml tts.python, never a fixed path


@pytest.mark.parametrize("source", ["voice.py", "compose.py", "voicelint.py", "voiceconf.py"])
def test_no_long_dashes_in_the_new_files(source):
    assert not [c for c in module_source(source) if c in LONG_DASHES]


def test_no_long_dashes_in_this_test_file():
    assert not [c for c in HERE.read_text(encoding="utf-8") if c in LONG_DASHES]


# ---- the stage: shared fakes ----

def stage_lines(w):
    return voice.build_lines(json.loads((w / "script.json").read_text()), json.loads((w / "ledger.json").read_text()),
                             M.voices())


def fake_tts(lines, voice_dir, voices_path, ref):
    """Stands in for run_tts: a tone of 0.4 s a word per line (a different pitch each, so the agent chains change
    real samples), mono at the voice sample rate."""
    for n, ln in enumerate(lines):
        M.tone(Path(voice_dir) / f"{ln['id']}.wav", 0.4 * len(ln["text"].split()), 200 + 20 * n, sr=24000, ch=1)
    return {"device": "fake", "lines": []}


def heard_all(jobs, manifest, *args, **kw):
    for j in jobs:
        toks = j["text"].split()
        Path(j["out"]).write_text(json.dumps([{"word": t, "start": 0.4 * i, "end": 0.4 * (i + 1)}
                                              for i, t in enumerate(toks)]))


def write_wav(path, seconds, sr=24000):
    """Mono silence of `seconds`, written with the stdlib (no ffmpeg), for tests that only need a file of a length."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * round(sr * seconds))


def read_wav_seconds(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def fast_audio(monkeypatch):
    """Silence and length reading through the stdlib instead of ffmpeg and ffprobe. Used by every test that checks a
    reason code or a call; the tests that prove what ffmpeg does to audio keep the real thing."""
    monkeypatch.setattr(voice, "silent_wav", write_wav)
    monkeypatch.setattr(voice, "wav_seconds", read_wav_seconds)


def fast_tts(lines, voice_dir, voices_path, ref):
    for ln in lines:
        write_wav(Path(voice_dir) / f"{ln['id']}.wav", 0.4 * len(ln["text"].split()))
    return {"device": "fake", "lines": []}


def fast_post(wav, chain):
    """A chain that changes nothing audible: keeps the take as <id>.raw.wav and leaves the wav as it was."""
    wav = Path(wav)
    raw = wav.with_name(wav.stem + ".raw.wav")
    shutil.copyfile(wav, raw)
    # the length a real chain gives (the agent voices play at 0.93 speed); silence is silence, so only the length matters
    write_wav(wav, voice.wav_seconds(raw) / voice.chain_speed(chain))


class Fakes:
    """The real-path stage with Chatterbox and Whisper faked: the host reference passes (digest REF) unless
    fake_ref=False, run_tts is `tts`, post-processing is recorded, and Whisper is `heard`, recording the jobs it was
    given and the interpreter and model it was asked to use. By default the audio is stdlib silence and a chain is a
    copy (fast); real=True uses ffmpeg tones and the real chains."""
    REF = "abc123def4567890"

    def __init__(self, monkeypatch, heard=heard_all, tts=None, real=False, fake_ref=True):
        self.posted, self.jobs, self.tts_calls, self.whisper_calls = [], [], [], []
        if not real:
            fast_audio(monkeypatch)
        tts = tts or (fake_tts if real else fast_tts)
        real_post = voice.post_process if real else fast_post

        def post(wav, chain):
            self.posted.append(Path(wav).name)
            real_post(wav, chain)

        def run_tts(lines, voice_dir, voices_path, ref, **kw):
            self.tts_calls.append({"ids": [ln["id"] for ln in lines], "voices_path": voices_path, "ref": ref, **kw})
            return tts(lines, voice_dir, voices_path, ref)

        def whisper(jobs, manifest, python=None, model=None, log=None):
            self.jobs.extend(jobs)
            self.whisper_calls.append((python, model))
            heard(jobs, manifest)

        if fake_ref:
            monkeypatch.setattr(voiceconf, "ref_ok", lambda v: (True, self.REF))
        monkeypatch.setattr(voice, "run_tts", run_tts)
        monkeypatch.setattr(voice, "post_process", post)
        monkeypatch.setattr(align, "run_whisper", whisper)

    def job_audio(self):
        """line id -> the file name Whisper was given to listen to."""
        return {Path(j["out"]).name.removesuffix(".heard.json"): Path(j["audio"]).name for j in self.jobs}


def boom(*a, **k):
    raise AssertionError("must not run")


def words_of(w, line_id):
    return json.loads((w / "voice" / f"{line_id}.words.json").read_text())


# ---- the stage: settings and the text gate come first ----

def test_stage_refuses_without_settings_after_clearing_its_stale_outputs(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    (w / "voice").mkdir()
    (w / "hostlint.json").write_text("{}")
    monkeypatch.setattr(gate, "read_verified", boom)
    r = voice.run_stage(ctx(w, settings=None))
    assert (r.ok, r.reason) == (False, "settings_missing")
    assert not (w / "voice").exists() and not (w / "hostlint.json").exists()


def test_stage_refuses_without_a_verdict(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path, gated=False)
    assert not (w / "verdict.json").exists()
    monkeypatch.setattr(voice, "build_lines", boom)
    monkeypatch.setattr(voice, "run_tts", boom)
    monkeypatch.setattr(voicelint, "write_record", boom)
    monkeypatch.setattr(voiceconf, "load_voices_hashed", boom)
    r = voice.run_stage(ctx(w, dry=True))
    assert not r.ok and r.reason == "gate_not_accepted"
    assert not (w / "voice").exists() and not (w / "hostlint.json").exists()


def test_stage_asks_the_gate_for_the_text_checks_and_this_ledger(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    fast_audio(monkeypatch)
    calls = []
    real = gate.read_verified

    def spy(*args, **kw):
        calls.append(inspect.signature(real).bind(*args, **kw).arguments)
        return real(*args, **kw)

    monkeypatch.setattr(gate, "read_verified", spy)
    monkeypatch.setattr(gate, "accepts", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a second, separate read")))
    r = voice.run_stage(ctx(w, dry=True))
    assert r.ok, r.reason
    (asked,) = calls
    assert asked["verdict_path"] == w / "verdict.json" and asked["subject_path"] == w / "script.json"
    assert asked["required"] is gate.REQUIRED and asked["required"] is voice.TEXT_CHECKS
    assert asked["ledger_path"] == w / "ledger.json"


def test_the_gate_is_asked_before_anything_is_read_or_written(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    (w / "voice").mkdir()
    (w / "voice" / "old.wav").write_bytes(b"x")
    (w / "hostlint.json").write_text("{}")
    at_gate = {}

    def refuse(v, s, ledger, req):
        at_gate["voice"], at_gate["hostlint"] = (w / "voice").exists(), (w / "hostlint.json").exists()
        return None, None, "failed:slop"

    monkeypatch.setattr(gate, "read_verified", refuse)
    monkeypatch.setattr(voice, "build_lines", lambda *a: (_ for _ in ()).throw(AssertionError("lines before gate")))
    monkeypatch.setattr(voiceconf, "load_voices_hashed", boom)
    r = voice.run_stage(ctx(w))
    assert not r.ok and at_gate == {"voice": False, "hostlint": False}  # only the stale cleanup came first


@pytest.mark.parametrize("why", ["failed:slop,names", "unreadable:FileNotFoundError", "error:/home/someone/secret"])
def test_a_refusal_is_a_bare_code_whatever_the_gate_says(tmp_path, monkeypatch, why):
    w = M.work_dir(tmp_path)
    monkeypatch.setattr(gate, "read_verified", lambda v, s, ledger, req: (None, None, why))
    r = voice.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "gate_not_accepted")


def test_a_script_changed_after_the_gate_is_refused(tmp_path):
    w = M.work_dir(tmp_path)
    script = json.loads((w / "script.json").read_text())
    script["title"] += "!"
    files.write_json(w / "script.json", script)
    assert voice.run_stage(ctx(w, dry=True)).reason == "gate_not_accepted"
    assert not (w / "voice").exists()


def test_a_ledger_changed_after_the_gate_is_refused(tmp_path):
    w = M.work_dir(tmp_path)
    ledger = json.loads((w / "ledger.json").read_text())
    ledger["note"] = "edited after the verdict"
    files.write_json(w / "ledger.json", ledger)
    assert voice.run_stage(ctx(w, dry=True)).reason == "gate_not_accepted"
    assert not (w / "voice").exists()


def test_a_verdict_with_a_failing_check_is_refused(tmp_path):
    w = M.work_dir(tmp_path)
    verdict = json.loads((w / "verdict.json").read_text())
    verdict["checks"][0]["pass"] = False
    files.write_json(w / "verdict.json", verdict)
    assert voice.run_stage(ctx(w, dry=True)).reason == "gate_not_accepted"


def rebind_verdict(w):
    """Make verdict.json name the bytes script.json and ledger.json hold now, as a gate that had accepted them would."""
    verdict = json.loads((w / "verdict.json").read_text())
    verdict["subject_sha256"] = files.sha256_bytes((w / "script.json").read_bytes())
    verdict["ledger_sha256"] = files.sha256_bytes((w / "ledger.json").read_bytes())
    files.write_json(w / "verdict.json", verdict)


def test_a_quiet_script_at_the_voice_stage_stops_with_a_code(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    files.write_json(w / "script.json", {"quiet": True, "quiet_reason": "nothing happened"})
    rebind_verdict(w)
    r = voice.run_stage(ctx(w, dry=True))
    assert (r.ok, r.reason) == (False, "quiet_script_at_voice")
    assert not (w / "hostlint.json").exists()


# ---- the stage: voices.json, read once, every agent voiced ----

def test_a_missing_voices_file_is_a_code_naming_the_problem(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    VP().unlink()
    r = voice.run_stage(ctx(w, dry=True))
    assert (r.ok, r.reason) == (False, "voice_config:voices_file_unreadable")
    assert not (w / "voice").exists() and not (w / "hostlint.json").exists()


def test_a_broken_voices_file_is_a_code(tmp_path):
    w = M.work_dir(tmp_path)
    VP().write_text("{not json")
    assert voice.run_stage(ctx(w, dry=True)).reason == "voice_config:voices_bad_json"


def test_a_voices_file_without_the_host_reference_names_the_key(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    data = copy.deepcopy(M.VOICES)
    del data["voices"]["host"]["ref"]
    use(monkeypatch, tmp_path, extra=REAL, voices_json=data)
    r = voice.run_stage(ctx(w, dry=True))
    assert (r.ok, r.reason) == (False, "voice_config:missing:host.ref")


def test_a_config_error_never_carries_a_value(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    data = copy.deepcopy(M.VOICES)
    data["voices"]["codex"]["color"] = "/home/someone/secret"
    use(monkeypatch, tmp_path, extra=REAL, voices_json=data)
    r = voice.run_stage(ctx(w, dry=True))
    assert (r.ok, r.reason) == (False, "voice_config:bad:codex.color") and "secret" not in r.reason


def test_an_agent_in_the_ledger_with_no_voice_stops_the_stage_before_any_audio(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path, "none")
    data = copy.deepcopy(M.VOICES)
    del data["voices"]["night_bot"]  # in the coverage (unavailable) and on the scoreboard, but nobody to label it
    use(monkeypatch, tmp_path, extra=REAL, voices_json=data)
    Fakes(monkeypatch)
    r = voice.run_stage(ctx(w, dry=True))
    assert (r.ok, r.reason) == (False, "voice_missing:night_bot")
    assert not (w / "voice").exists()


def test_a_hostile_coverage_key_shows_as_a_placeholder(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path, "none")
    led = json.loads((w / "ledger.json").read_text())
    led["coverage"]["/home/someone/x y"] = "ok"
    files.write_json(w / "ledger.json", led)
    rebind_verdict(w)
    r = voice.run_stage(ctx(w, dry=True))
    assert (r.ok, r.reason) == (False, "voice_missing:?")


def test_unvoiced_agents_lists_the_coverage_ids_voices_json_lacks():
    voices = M.voices()
    assert voice.unvoiced_agents(M.ledger(), voices) == []  # positive control: the fixture voices every agent
    del voices["my_agent"]
    assert voice.unvoiced_agents(M.ledger(), voices) == ["my_agent"]
    assert voice.unvoiced_agents({"coverage": {"host": "ok"}}, voices) == ["host"]  # the narrator is never an agent
    assert voice.unvoiced_agents({}, voices) == [] and voice.unvoiced_agents({"coverage": []}, voices) == []


# ---- the stage: the bytes read are the bytes the gate accepted ----

def write_before_read(monkeypatch, write):
    """The stage's single read of the gate files: `write` changes something just before it, the latest moment a
    concurrent writer can act (after the stage has started, before any byte is read)."""
    real = gate.read_verified

    def write_then_read(*args, **kw):
        write()
        return real(*args, **kw)

    monkeypatch.setattr(gate, "read_verified", write_then_read)


def assert_nothing_voiced(w):
    assert not (w / "voice").exists() and not (w / "hostlint.json").exists()


@pytest.mark.parametrize("which", ["script", "ledger"])
def test_a_file_rewritten_after_the_gate_checked_it_is_not_voiced(tmp_path, monkeypatch, which):
    w = M.work_dir(tmp_path)
    fk = Fakes(monkeypatch)

    def rewrite():
        data = json.loads((w / f"{which}.json").read_text())
        data["note" if which == "ledger" else "title"] = "swapped in after the verdict"
        files.write_json(w / f"{which}.json", data)

    write_before_read(monkeypatch, rewrite)
    monkeypatch.setattr(voice, "build_lines", lambda *a: (_ for _ in ()).throw(AssertionError("built from unseen text")))
    r = voice.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "gate_not_accepted")
    assert_nothing_voiced(w)
    assert fk.tts_calls == []


def swap_verdict(w, mutate):
    """Replace verdict.json by the same verdict, hashes intact, after `mutate` has changed it."""
    v = json.loads((w / "verdict.json").read_text())
    mutate(v)
    files.write_json(w / "verdict.json", v)


SWAPS = {
    "pass_false": lambda v: v.update({"pass": False}),
    "pass_string": lambda v: v.update({"pass": "true"}),
    "no_checks": lambda v: v.update({"checks": []}),
    "missing_required": lambda v: v.update({"checks": [c for c in v["checks"] if c["name"] != gate.REQUIRED[-1]]}),
    "failing_check": lambda v: v["checks"][1].update({"pass": False}),
    "check_pass_string": lambda v: v["checks"][1].update({"pass": "true"}),
    "duplicated_check": lambda v: v["checks"].append(dict(v["checks"][1])),
}


@pytest.mark.parametrize("swap", list(SWAPS))
def test_a_verdict_swapped_in_before_the_read_is_judged_in_full(tmp_path, monkeypatch, swap):
    """Hashes valid, pass true or not, but the check list incomplete or failing: nothing is voiced."""
    w = M.work_dir(tmp_path)
    write_before_read(monkeypatch, lambda: swap_verdict(w, SWAPS[swap]))
    r = voice.run_stage(ctx(w, dry=True))
    assert (r.ok, r.reason) == (False, "gate_not_accepted")
    assert_nothing_voiced(w)


@pytest.mark.parametrize("damage", ["delete", "garbage", "list"])
def test_a_verdict_that_changes_after_the_check_is_not_trusted(tmp_path, monkeypatch, damage):
    w = M.work_dir(tmp_path)

    def spoil():
        v = w / "verdict.json"
        v.unlink() if damage == "delete" else v.write_text("{broken" if damage == "garbage" else "[]")

    write_before_read(monkeypatch, spoil)
    r = voice.run_stage(ctx(w, dry=True))
    assert (r.ok, r.reason) == (False, "gate_not_accepted")
    assert_nothing_voiced(w)


def test_the_text_that_is_voiced_is_the_text_that_was_hashed(tmp_path, monkeypatch):
    """The stage parses the bytes it hashed: a read of the file after the hash would see this swap, a parse of the
    hashed bytes does not."""
    w = M.work_dir(tmp_path)
    Fakes(monkeypatch)
    seen = {}
    real_read = Path.read_bytes

    def read_then_swap(self):
        data = real_read(self)
        if self.name == "script.json":
            seen["reads"] = seen.get("reads", 0) + 1
        if self.name == "script.json" and seen["reads"] == 1:  # the gate's one read (gate.read_verified)
            seen["swapped"] = True
            other = json.loads(data)
            other["cold_open"]["host"] = "Words the gate never saw."
            files.write_json(self, other)  # replaced right after the gate read the bytes it hashed and the stage parses
        return data

    monkeypatch.setattr(Path, "read_bytes", read_then_swap)
    r = voice.run_stage(ctx(w))
    monkeypatch.undo()
    assert r.ok, r.reason
    spoken = json.loads((w / "voice" / "lines.json").read_text())
    assert "Words the gate never saw." not in [ln["text"] for ln in spoken]
    assert seen.get("swapped")


# ---- the stage: stale outputs ----

@pytest.mark.parametrize("kind", ["dir", "symlink", "file", "dangling"])
def test_a_refused_run_leaves_no_voice_dir_and_never_follows_a_link(tmp_path, kind):
    w = M.work_dir(tmp_path, gated=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    target = w / "voice"
    if kind == "dir":
        target.mkdir()
        (target / "old.wav").write_bytes(b"old take")
        (target / "nested").mkdir()
        (target / "nested" / "deep.json").write_text("{}")
        (target / "link").symlink_to(outside, target_is_directory=True)  # rmtree must not follow this
    elif kind == "symlink":
        target.symlink_to(outside, target_is_directory=True)
    elif kind == "file":
        target.write_text("not a dir")
    else:
        target.symlink_to(tmp_path / "nowhere")
    (w / "hostlint.json").write_text("{}")
    r = voice.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "gate_not_accepted")
    assert not target.exists() and not target.is_symlink() and not (w / "hostlint.json").exists()
    assert (outside / "keep.txt").read_text() == "keep"


def test_a_hostlint_link_is_removed_not_followed(tmp_path):
    w = M.work_dir(tmp_path, gated=False)
    (tmp_path / "precious.json").write_text("precious")
    (w / "hostlint.json").symlink_to(tmp_path / "precious.json")
    voice.run_stage(ctx(w))
    assert not (w / "hostlint.json").is_symlink() and (tmp_path / "precious.json").read_text() == "precious"


def test_a_cleanup_that_fails_stops_the_stage_with_a_code(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    (w / "voice").mkdir()
    monkeypatch.setattr(voice.shutil, "rmtree", lambda p: (_ for _ in ()).throw(PermissionError("/home/someone/x")))
    monkeypatch.setattr(gate, "read_verified", lambda *a, **k: (_ for _ in ()).throw(AssertionError("gate after a bad clean")))
    r = voice.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "stale_cleanup")


def test_a_failed_rerun_leaves_no_take_of_the_run_before(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    Fakes(monkeypatch)
    assert voice.run_stage(ctx(w)).ok and list((w / "voice").glob("*.wav"))
    monkeypatch.setattr(voice, "run_tts", lambda *a, **k: (_ for _ in ()).throw(voice.VoiceError("tts_runner_failed")))
    r = voice.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "voice:tts_runner_failed")
    assert sorted(p.name for p in (w / "voice").iterdir()) == ["lines.json", voice.MANIFEST]  # nothing of the first run


def test_a_real_run_after_a_dry_one_starts_clean(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    Fakes(monkeypatch)
    assert voice.run_stage(ctx(w, dry=True)).ok
    assert words_of(w, "out")["source"] == "dry"
    assert voice.run_stage(ctx(w)).ok
    assert {words_of(w, ln["id"])["source"] for ln in stage_lines(w)} == {"whisper"}


# ---- the stage: dry ----

@pytest.fixture(scope="module", params=CASES)
def dry_case(request, tmp_path_factory):
    """One real-ffmpeg dry run per fixture case, shared by the tests that only read its outputs. The real voice, the
    host reference check and Whisper are made to fail loudly, so a dry run that touched them would fail the fixture."""
    if not M.HAS_FFMPEG:
        pytest.skip("ffmpeg and ffprobe are not installed")

    def loud(*a, **k):
        raise AssertionError("a dry stage touched the real voice")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(voice, "run_tts", loud)
        mp.setattr(voiceconf, "ref_ok", loud)
        mp.setattr(align, "run_whisper", loud)
        w = M.dry_work(tmp_path_factory.mktemp(f"dry-{request.param}"), request.param)
    return request.param, w


@needs_ffmpeg
def test_dry_stage_voices_every_line(dry_case):
    case, w = dry_case
    want = stage_lines(w)
    assert [ln["id"] for ln in json.loads((w / "voice" / "lines.json").read_text())] == [ln["id"] for ln in want]
    assert (w / "hostlint.json").exists()
    for ln in want:
        assert (w / "voice" / f"{ln['id']}.wav").is_file() and words_of(w, ln["id"])["source"] == "dry"


def open_flags(w):
    return voicelint.open_count(json.loads((w / "hostlint.json").read_text()))


def test_reasons_carry_the_line_count_and_the_lint_count(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    Fakes(monkeypatch)
    r = voice.run_stage(ctx(w, dry=True))
    assert r.reason == f"dry; hostlint open {open_flags(w)}" and isinstance(open_flags(w), int)
    r = voice.run_stage(ctx(w))
    assert r.ok and r.reason == f"{len(stage_lines(w))} lines; hostlint open {open_flags(w)}"


def test_the_lint_uses_the_operators_avoid_words(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    fast_audio(monkeypatch)
    assert voice.run_stage(ctx(w, dry=True)).ok
    before = json.loads((w / "hostlint.json").read_text())
    assert not [f for f in before["result"]["flags"] if f["match"] == "recipe"]  # positive control: not a built-in tell
    use(monkeypatch, tmp_path, extra=REAL.replace("[tts]", '[writer]\navoid_words = ["recipe"]\n\n[tts]'))
    assert voice.run_stage(ctx(w, dry=True)).ok
    after = json.loads((w / "hostlint.json").read_text())
    assert [f["line"] for f in after["result"]["flags"] if f["match"] == "recipe"]


@pytest.mark.parametrize("dry", [True, False])
def test_an_unavailable_lint_is_said_and_does_not_block(tmp_path, monkeypatch, dry):
    w = M.work_dir(tmp_path)
    monkeypatch.setattr(voicelint.Linter, "lint", lambda self, lines: (_ for _ in ()).throw(RuntimeError("lint broke")))
    fast_audio(monkeypatch)
    if not dry:
        Fakes(monkeypatch)
    r = voice.run_stage(ctx(w, dry=dry))
    rec = json.loads((w / "hostlint.json").read_text())
    assert rec["lint"] == "unavailable" and rec["reason"] == "run" and voicelint.open_count(rec) is None
    assert r.ok and r.reason == ("dry" if dry else f"{len(stage_lines(w))} lines") + "; hostlint unavailable"


@pytest.mark.parametrize("count", [True, False, "3", 2.5, None])
def test_a_lint_count_that_is_not_a_whole_number_counts_as_unavailable(tmp_path, monkeypatch, count):
    """open_count can hand back whatever the lint record held; a bool is an int to Python, so it is refused by name."""
    w = M.work_dir(tmp_path)
    fast_audio(monkeypatch)
    monkeypatch.setattr(voicelint, "write_record", lambda *a, **k: {"lint": "ok", "result": {"open": count}})
    r = voice.run_stage(ctx(w, dry=True))
    assert r.ok and r.reason == "dry; hostlint unavailable"


def test_a_whole_number_lint_count_is_said(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    fast_audio(monkeypatch)
    monkeypatch.setattr(voicelint, "write_record", lambda *a, **k: {"lint": "ok", "result": {"open": 0}})
    assert voice.run_stage(ctx(w, dry=True)).reason == "dry; hostlint open 0"


def test_a_dry_run_needs_no_interpreters_and_no_reference(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    use(monkeypatch, tmp_path)  # no [tts] and no [whisper] at all
    fast_audio(monkeypatch)
    monkeypatch.setattr(voiceconf, "ref_ok", boom)
    monkeypatch.setattr(voice, "run_tts", boom)
    monkeypatch.setattr(align, "run_whisper", boom)
    r = voice.run_stage(ctx(w, dry=True))
    assert r.ok, r.reason


# ---- the stage: what a real run needs before any voicing ----

@pytest.mark.parametrize("extra,code", [
    ('[whisper]\npython = "/opt/whisper/bin/python"\n', "missing:tts.python"),
    ('[tts]\npython = "/opt/tts/bin/python"\n', "missing:whisper.python"),
    ("", "missing:whisper.python"),
])
def test_a_real_run_names_the_interpreter_it_lacks_before_any_voicing(tmp_path, monkeypatch, extra, code):
    w = M.work_dir(tmp_path)
    use(monkeypatch, tmp_path, extra=extra)
    fast_audio(monkeypatch)
    monkeypatch.setattr(voiceconf, "ref_ok", boom)
    monkeypatch.setattr(voice, "run_tts", boom)
    r = voice.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, code)
    assert (w / "hostlint.json").exists()  # the advisory lint was already written


def test_a_forced_remote_run_needs_no_local_tts_interpreter(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    use(monkeypatch, tmp_path, extra='[tts]\ntarget = "remote"\nhost = "gpu-box"\nremote_python = "/opt/tts/bin/python"\n'
                                     'remote_dir = "/var/tmp/tts-runs"\n\n[whisper]\npython = "/opt/whisper/bin/python"\n')
    fk = Fakes(monkeypatch)
    assert voice.run_stage(ctx(w)).ok and len(fk.tts_calls) == 1


# ---- the stage: the host reference ----

def ref_config(monkeypatch, tmp_path, content):
    """A config whose host reference is a file under tmp_path holding `content` (None: no file there)."""
    ref = tmp_path / "refs" / "host.wav"
    if content is not None:
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_bytes(content)
    data = copy.deepcopy(M.VOICES)
    data["voices"]["host"]["ref"] = str(ref)
    use(monkeypatch, tmp_path, extra=REAL, voices_json=data)
    return ref


@pytest.mark.parametrize("content,code", [(None, "missing"), (b"someone else's recording", "mismatch")])
def test_a_reference_that_is_missing_or_not_the_hashed_one_fails_before_tts_with_a_bare_code(tmp_path, monkeypatch,
                                                                                            content, code):
    w = M.work_dir(tmp_path)
    ref_config(monkeypatch, tmp_path, content)
    fk = Fakes(monkeypatch, fake_ref=False)
    asked = []
    real = voiceconf.ref_ok
    monkeypatch.setattr(voiceconf, "ref_ok", lambda v: (asked.append(v["display"]), real(v))[1])
    r = voice.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, f"voice_ref:{code}")
    assert asked == ["Sam"]  # the host's voice, nobody else's
    assert fk.tts_calls == [] and str(tmp_path) not in r.reason
    assert (w / "hostlint.json").exists()  # the lint is advisory and was already written


def test_the_verified_reference_digest_goes_to_voicing_and_the_manifest(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    ref_config(monkeypatch, tmp_path, M.HOST_REF_BYTES)
    fk = Fakes(monkeypatch, fake_ref=False)
    assert voice.run_stage(ctx(w)).ok
    digest = hashlib.sha256(M.HOST_REF_BYTES).hexdigest()[:16]
    assert fk.tts_calls[0]["ref"] == digest and manifest_of(w)["ref_sha256"] == digest


# ---- the stage: the real path ----

@needs_ffmpeg
@pytest.mark.parametrize("case", CASES)
def test_real_path_posts_only_harness_lines_and_aligns(case, tmp_path, monkeypatch):
    w = M.work_dir(tmp_path, case)
    fk = Fakes(monkeypatch, real=True)  # one end-to-end run per case with real ffmpeg
    r = voice.run_stage(ctx(w))
    assert r.ok, r.reason
    lines = stage_lines(w)
    assert fk.posted == ([] if case == "none" else ["confessional.wav"])
    assert [t["ids"] for t in fk.tts_calls] == [[ln["id"] for ln in lines]]  # one runner pass for every line
    for ln in lines:
        rec = words_of(w, ln["id"])
        assert (rec["id"], rec["text"], rec["voice"]) == (ln["id"], ln["text"], ln["voice"])
        assert rec["source"] == "whisper" and rec["coverage"] == 1.0
        assert [x["word"] for x in rec["words"]] == ln["text"].split()
    assert (w / "voice" / "confessional.words.json").exists() == (case != "none")
    assert (w / "voice" / "confessional.raw.wav").exists() == (case != "none")
    assert not [p for p in (w / "voice").iterdir() if p.name.startswith(".")]  # no temp file left behind


def test_the_runner_gets_the_voices_file_and_the_digest_the_stage_checked(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    fk = Fakes(monkeypatch)
    assert voice.run_stage(ctx(w)).ok
    (call,) = fk.tts_calls
    assert call["voices_path"] == VP() == STATE["dir"] / "voices.json" and call["ref"] == Fakes.REF
    assert call["settings"] is STATE["settings"]


def test_whisper_runs_with_the_configured_interpreter_and_model(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    use(monkeypatch, tmp_path, extra=REAL + 'model = "base.en"\n')
    fk = Fakes(monkeypatch)
    assert voice.run_stage(ctx(w)).ok
    assert fk.whisper_calls == [(Path("/opt/whisper/bin/python"), "base.en")]


def manifest_of(w):
    return json.loads((w / "voice" / voice.MANIFEST).read_text())


def test_a_real_run_records_what_it_used(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    Fakes(monkeypatch)
    assert voice.run_stage(ctx(w)).ok
    m, lines = manifest_of(w), stage_lines(w)
    voices = M.voices()
    assert m["schema"] == voice.MANIFEST_SCHEMA
    assert m["voices_sha256"] == voices_sha()
    assert m["ref_sha256"] == Fakes.REF
    assert m["lines"] == [{"id": ln["id"], "voice": ln["voice"], "params": ln["params"],
                           "post": voices[ln["voice"]]["post"]} for ln in lines]
    assert not [p for p in (w / "voice").iterdir() if p.name.startswith(".") and p.name != "."]  # written atomically


def test_a_dry_run_records_the_same_with_a_null_reference(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    fast_audio(monkeypatch)
    assert voice.run_stage(ctx(w, dry=True)).ok
    m = manifest_of(w)
    assert m["voices_sha256"] == voices_sha() and m["ref_sha256"] is None
    assert [r["id"] for r in m["lines"]] == [ln["id"] for ln in stage_lines(w)]


def test_the_manifest_hashes_the_voices_bytes_the_lines_were_built_from(tmp_path, monkeypatch):
    """voices.json is read once for the lines; a rewrite right after that read is what the post-run check catches."""
    w = M.work_dir(tmp_path)
    Fakes(monkeypatch)
    real = voiceconf.load_voices_hashed

    def load_then_edit(*a, **k):
        got = real(*a, **k)
        VP().write_bytes(VP().read_bytes() + b"\n")  # after the one read the lines are built from
        return got

    monkeypatch.setattr(voiceconf, "load_voices_hashed", load_then_edit)
    r = voice.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "voice_config_changed")


def test_a_voices_file_changed_while_the_runner_ran_fails_the_stage(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    fk = Fakes(monkeypatch)
    inner = voice.run_tts

    def tts_then_edit(lines, voice_dir, voices_path, ref, **kw):
        out = inner(lines, voice_dir, voices_path, ref, **kw)
        VP().write_bytes(VP().read_bytes() + b"\n")
        return out

    monkeypatch.setattr(voice, "run_tts", tts_then_edit)
    r = voice.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "voice_config_changed")
    assert fk.posted == []


def test_lines_json_is_what_the_stage_built(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    Fakes(monkeypatch)
    written = []
    real = voice.files.write_json
    monkeypatch.setattr(voice.files, "write_json", lambda p, o: (written.append(Path(p).name), real(p, o)))
    assert voice.run_stage(ctx(w)).ok
    assert json.loads((w / "voice" / "lines.json").read_text()) == stage_lines(w)
    assert "lines.json" in written and not [p for p in (w / "voice").iterdir() if p.name.endswith(".tmp")]


@needs_ffmpeg
def test_harness_lines_are_heard_on_the_clean_take_and_timed_by_the_final_one(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    fk = Fakes(monkeypatch, real=True)
    assert voice.run_stage(ctx(w)).ok
    audio = fk.job_audio()
    for ln in stage_lines(w):
        want = "confessional.raw.wav" if ln["id"] == "confessional" else f"{ln['id']}.wav"
        assert audio[ln["id"]] == want, ln["id"]
    vd = w / "voice"
    assert (vd / "confessional.raw.wav").read_bytes() != (vd / "confessional.wav").read_bytes()  # the chain really ran
    rec = words_of(w, "confessional")
    assert rec["dur"] == round(voice.wav_seconds(vd / "confessional.wav"), 3)
    chain = M.voices()[next(ln["voice"] for ln in stage_lines(w) if ln["id"] == "confessional")]["post"]
    expected = voice.wav_seconds(vd / "confessional.raw.wav") / voice.chain_speed(chain)
    assert -voice.RAW_TOLERANCE_S <= rec["dur"] - expected <= voice.echo_tail_s(chain) + voice.RAW_TOLERANCE_S
    assert rec["dur"] > voice.wav_seconds(vd / "confessional.raw.wav")  # an agent voice is slowed on purpose


@pytest.mark.parametrize("skew", [0.3, -0.3])
def test_a_raw_take_of_another_length_fails_before_whisper(tmp_path, monkeypatch, skew):
    w = M.work_dir(tmp_path)
    fk = Fakes(monkeypatch)

    def skewed(wav, chain):
        wav = Path(wav)
        raw = wav.with_name(wav.stem + ".raw.wav")
        wav.replace(raw)
        write_wav(wav, voice.wav_seconds(raw) + skew)  # the final take is 0.3 s off from its raw

    monkeypatch.setattr(voice, "post_process", skewed)
    r = voice.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "voice:raw_length") and fk.jobs == []


def test_a_host_chain_in_the_config_is_applied_and_heard_raw_too(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path, "none")
    fk = Fakes(monkeypatch)
    voices = M.voices()
    voices["host"]["post"] = voices["codex"]["post"]
    monkeypatch.setattr(voiceconf, "load_voices_hashed", lambda *a: (voices, voices_sha()))
    assert voice.run_stage(ctx(w)).ok
    assert sorted(fk.posted) == sorted(f"{ln['id']}.wav" for ln in stage_lines(w))
    assert set(fk.job_audio().values()) == {f"{ln['id']}.raw.wav" for ln in stage_lines(w)}


# ---- the stage: the coverage floor ----

def heard_none_for_out(jobs, manifest):
    heard_all(jobs, manifest)
    Path(next(j for j in jobs if j["audio"].endswith("/out.wav"))["out"]).write_text("[]")


def test_low_coverage_fails_the_stage_and_names_the_line(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    Fakes(monkeypatch, heard=heard_none_for_out)
    r = voice.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "align_coverage:out=0.0")


def test_every_low_line_is_named_in_speaking_order(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)

    def heard_nothing_for(jobs, manifest):
        heard_all(jobs, manifest)
        for j in jobs:
            if Path(j["out"]).name in ("steal_this.heard.json", "cold_open.heard.json", "out.heard.json"):
                Path(j["out"]).write_text("[]")

    Fakes(monkeypatch, heard=heard_nothing_for)
    r = voice.run_stage(ctx(w))
    assert r.reason == "align_coverage:cold_open=0.0,steal_this=0.0,out=0.0"


def test_the_floor_is_align_min_coverage_inclusive(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path, "none")
    Fakes(monkeypatch)
    real = align.align_words
    cov = {"value": align.MIN_COVERAGE}
    monkeypatch.setattr(align, "align_words", lambda text, heard, dur: (real(text, heard, dur)[0], cov["value"]))
    assert voice.run_stage(ctx(w)).ok  # exactly on the floor passes
    cov["value"] = align.MIN_COVERAGE - 0.001
    r = voice.run_stage(ctx(w))
    assert not r.ok and r.reason.startswith("align_coverage:cold_open=0.599,") and r.reason.endswith("out=0.599")


def test_a_coverage_that_is_not_a_number_fails_the_line(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path, "none")
    Fakes(monkeypatch)
    real = align.align_words
    monkeypatch.setattr(align, "align_words", lambda text, heard, dur: (real(text, heard, dur)[0], float("nan")))
    assert voice.run_stage(ctx(w)).reason.startswith("align_coverage:cold_open=nan")


# ---- the stage: failures are codes, never text ----

SECRET = "/home/someone/private/quote about the hidden notes"


@pytest.mark.parametrize("exc", [voiceconf.VoiceConfigError(SECRET), voice.VoiceError(SECRET), LookupError(SECRET),
                                 KeyError(SECRET), TypeError(SECRET), ValueError(SECRET), OSError(SECRET)])
def test_a_line_building_failure_is_its_class_name_only(tmp_path, monkeypatch, exc):
    w = M.work_dir(tmp_path)
    monkeypatch.setattr(voice, "build_lines", lambda *a: (_ for _ in ()).throw(exc))
    r = voice.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, f"voice_lines:{type(exc).__name__}")
    assert not (w / "voice").exists()  # nothing is written for lines that were never built


def test_a_real_lookup_error_keeps_the_quote_id_out_of_the_reason(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    script = json.loads((w / "script.json").read_text())
    script["confessional"]["quote_id"] = "q:codex:deadbeef"
    files.write_json(w / "script.json", script)
    rebind_verdict(w)
    r = voice.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "voice_lines:LookupError")


def test_an_inactive_harness_voice_is_a_code_not_its_message(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    voices = M.voices()
    voices["codex"]["active"] = False
    monkeypatch.setattr(voiceconf, "load_voices_hashed", lambda *a: (voices, voices_sha()))
    r = voice.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "voice_lines:VoiceConfigError")


@pytest.mark.parametrize("exc", [OSError(SECRET), voice.VoiceError(SECRET), RuntimeError(SECRET), ValueError(SECRET),
                                 subprocess.CalledProcessError(1, ["ffmpeg", SECRET]),
                                 subprocess.TimeoutExpired(["ffmpeg", SECRET], 120)])
@pytest.mark.parametrize("where", ["tts", "post", "whisper"])
def test_a_voicing_failure_is_its_class_name_only(tmp_path, monkeypatch, exc, where):
    w = M.work_dir(tmp_path)
    fk = Fakes(monkeypatch)
    raise_it = lambda *a, **k: (_ for _ in ()).throw(exc)  # noqa: E731
    if where == "tts":
        monkeypatch.setattr(voice, "run_tts", raise_it)
    elif where == "post":
        monkeypatch.setattr(voice, "post_process", raise_it)
    else:
        monkeypatch.setattr(align, "run_whisper", raise_it)
    r = voice.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, f"voice:{type(exc).__name__}")
    assert fk.jobs == []


@pytest.mark.parametrize("where", ["tts", "post", "whisper"])
def test_a_termination_signal_is_not_swallowed(tmp_path, monkeypatch, where):
    """run.py's RunTerminated is a BaseException: the stage's own except clauses must let it through."""
    w = M.work_dir(tmp_path)
    Fakes(monkeypatch)
    stop = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())  # noqa: E731
    monkeypatch.setattr(voice if where != "whisper" else align,
                        {"tts": "run_tts", "post": "post_process", "whisper": "run_whisper"}[where], stop)
    with pytest.raises(KeyboardInterrupt):
        voice.run_stage(ctx(w))


@pytest.mark.parametrize("bad", ['{"not": "a list"}', "[1, 2]", "{broken"])
def test_a_malformed_whisper_file_fails_the_stage_with_a_code(tmp_path, monkeypatch, bad):
    w = M.work_dir(tmp_path, "none")

    def heard_garbage(jobs, manifest):
        for j in jobs:
            Path(j["out"]).write_text(bad)

    Fakes(monkeypatch, heard=heard_garbage)
    r = voice.run_stage(ctx(w))
    assert not r.ok and r.reason.startswith("voice:") and "/" not in r.reason


# ---- run_tts: proved by the runner's results file ----

FAKE_RUNNER = r'''
import json, os, sys, time, wave
from pathlib import Path

lines = json.loads(Path(sys.argv[1]).read_text())
out = Path(sys.argv[2])
mode = os.environ.get("FAKE_RUNNER_MODE", "ok")
ref = os.environ.get("FAKE_RUNNER_REF", "abc123def4567890")
Path(out / "runner-seen.json").write_text(json.dumps({"argv": sys.argv[1:], "alloc": os.environ.get("PYTORCH_CUDA_ALLOC_CONF"),
                                                      "lock": os.environ.get("ORION_CLOCK_TTS_LOCK")}))
if mode == "sleep":
    print("voiced cold_open voice=host seconds=1.5", flush=True)
    print("partial stderr", file=sys.stderr, flush=True)
    time.sleep(30)


def silent(path, seconds):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * int(24000 * seconds))


for i, ln in enumerate(lines):
    if mode == "missing_wav" and i == 2:
        continue
    if mode == "empty_wav" and i == 2:
        (out / (ln["id"] + ".wav")).write_bytes(b"")
        continue
    silent(out / (ln["id"] + ".wav"), 0.4 * len(ln["text"].split()))
rows = [{"id": ln["id"], "voice": ln["voice"], "text": ln["text"], "chunks": 1, "dur": 1.0} for ln in lines]
if mode == "wrong_ids":
    rows[-1]["id"] = "surprise"
if mode == "short_ids":
    rows = rows[:-1]
if mode == "extra_id":
    rows = rows + [dict(rows[0], id="extra")]
if mode == "reordered":
    rows = rows[::-1]
body = {"device": "fake", "ref_sha256": "0000000000000000" if mode == "wrong_ref" else ref, "lines": rows}
if mode == "no_ref_key":
    del body["ref_sha256"]
if mode == "lines_not_a_list":
    body["lines"] = "none"
if mode == "rows_not_objects":
    body["lines"] = [ln["id"] for ln in lines]
if mode == "list_results":
    body = rows
if mode != "no_results":
    (out / "tts-results.json").write_text("{not json" if mode == "bad_json" else json.dumps(body))
print("device fake: %d line(s)" % len(rows))
print("stderr noise", file=sys.stderr)
if mode != "no_marker":
    print("RUNNER_OK")
if mode == "exit_1":
    sys.exit(1)
'''
RUNNER_SETTINGS = f'[tts]\npython = "{sys.executable}"\n{{lock}}\n[whisper]\npython = "{sys.executable}"\n'


@pytest.fixture
def fake_runner(tmp_path, monkeypatch):
    """voice.run_tts pointed at a fake runner process (this Python, settings.toml tts.python) instead of Chatterbox."""
    script = tmp_path / "fake_runner.py"
    script.write_text(FAKE_RUNNER)
    monkeypatch.setattr(voice, "TTS_RUNNER", script)
    monkeypatch.delenv(tts_runner.LOCK_ENV, raising=False)
    return use(monkeypatch, tmp_path, name="runner-cfg", extra=RUNNER_SETTINGS.format(lock=""))


@pytest.fixture
def runner(tmp_path, fake_runner):
    """The fake runner, a voice dir holding lines.json, and the lines."""
    voice_dir = tmp_path / "vd"
    voice_dir.mkdir()
    lines = lines_for("transcript")
    files.write_json(voice_dir / "lines.json", lines)
    return voice_dir, lines


def run_tts(voice_dir, lines, digest="abc123def4567890"):
    return voice.run_tts(lines, voice_dir, VP(), digest, settings=STATE["settings"])


def test_run_tts_returns_the_results_when_everything_checks(runner):
    voice_dir, lines = runner
    results = run_tts(voice_dir, lines)
    assert [r["id"] for r in results["lines"]] == [ln["id"] for ln in lines] and results["device"] == "fake"
    assert results["tts_target"] == "local" and "remote_skip" not in results
    log = (voice_dir / "tts.log").read_text()
    assert "RUNNER_OK" in log and "stderr noise" in log  # the runner's output, both streams, stays in the work dir


def test_run_tts_runs_the_runner_as_an_argv_list_and_takes_no_lock(runner, monkeypatch):
    voice_dir, lines = runner
    seen = {}
    real = subprocess.run

    def spy(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return real(argv, **kw)

    monkeypatch.setattr(voice.subprocess, "run", spy)
    run_tts(voice_dir, lines)
    assert seen["argv"] == [sys.executable, str(voice.TTS_RUNNER), str(voice_dir / "lines.json"), str(voice_dir),
                            str(VP())]
    assert not seen["kw"].get("shell") and seen["kw"]["timeout"] == voice.TTS_TIMEOUT_S
    assert seen["kw"]["env"]["PYTORCH_CUDA_ALLOC_CONF"] == "expandable_segments:True"
    got = json.loads((voice_dir / "runner-seen.json").read_text())
    assert got["alloc"] == "expandable_segments:True" and got["lock"] is None  # no lock setting: the runner's default


def test_the_configured_lock_file_reaches_the_runner(runner, monkeypatch, tmp_path):
    voice_dir, lines = runner
    lock = tmp_path / "locks" / "tts.lock"
    use(monkeypatch, tmp_path, name="lock-cfg", extra=RUNNER_SETTINGS.format(lock=f'lock = "{lock}"'))
    run_tts(voice_dir, lines)
    assert json.loads((voice_dir / "runner-seen.json").read_text())["lock"] == str(lock)


@pytest.mark.parametrize("mode", ["wrong_ids", "short_ids", "extra_id", "reordered", "wrong_ref", "no_ref_key",
                                  "missing_wav", "empty_wav", "no_results", "bad_json", "lines_not_a_list",
                                  "rows_not_objects", "list_results", "no_marker", "exit_1"])
def test_run_tts_refuses_a_run_its_results_do_not_prove(runner, monkeypatch, mode):
    voice_dir, lines = runner
    monkeypatch.setenv("FAKE_RUNNER_MODE", mode)
    with pytest.raises(voice.VoiceError):
        run_tts(voice_dir, lines)


@pytest.mark.parametrize("mode,runner_ref,digest", [("no_ref_key", "x", ""), ("no_ref_key", "x", None),
                                                    ("ok", "", ""), ("ok", "", None)])
def test_run_tts_needs_a_real_digest_to_compare_with(runner, monkeypatch, mode, runner_ref, digest):
    """No digest from the stage's own ref check must never be matched by a results file that has none either, nor by one
    that carries an empty string."""
    voice_dir, lines = runner
    monkeypatch.setenv("FAKE_RUNNER_MODE", mode)
    monkeypatch.setenv("FAKE_RUNNER_REF", runner_ref)
    with pytest.raises(voice.VoiceError):
        run_tts(voice_dir, lines, digest)


def test_the_runner_is_started_with_no_stdin(runner, monkeypatch):
    voice_dir, lines = runner
    seen = {}
    real = subprocess.run
    monkeypatch.setattr(voice.subprocess, "run", lambda argv, **kw: (seen.update(kw), real(argv, **kw))[1])
    run_tts(voice_dir, lines)
    assert seen["stdin"] is subprocess.DEVNULL


def test_run_tts_gives_up_when_the_runner_outlasts_the_timeout(runner, monkeypatch):
    voice_dir, lines = runner
    monkeypatch.setenv("FAKE_RUNNER_MODE", "sleep")
    monkeypatch.setattr(voice, "TTS_TIMEOUT_S", 1)
    with pytest.raises(subprocess.TimeoutExpired):
        run_tts(voice_dir, lines)


def test_a_timeout_leaves_the_runners_partial_log(runner, monkeypatch):
    voice_dir, lines = runner
    monkeypatch.setenv("FAKE_RUNNER_MODE", "sleep")
    monkeypatch.setattr(voice, "TTS_TIMEOUT_S", 2)
    with pytest.raises(subprocess.TimeoutExpired):
        run_tts(voice_dir, lines)
    log = (voice_dir / "tts.log").read_text()
    assert "voiced cold_open voice=host seconds=1.5" in log and "partial stderr" in log
    assert "RUNNER_OK" not in log


def test_the_runner_streams_to_the_log_file_instead_of_capturing(runner, monkeypatch):
    voice_dir, lines = runner
    seen = {}
    real = subprocess.run
    monkeypatch.setattr(voice.subprocess, "run", lambda argv, **kw: (seen.update(kw), real(argv, **kw))[1])
    run_tts(voice_dir, lines)
    assert "capture_output" not in seen and seen["stderr"] == subprocess.STDOUT
    assert seen["stdout"].name == str(voice_dir / "tts.log")


@pytest.mark.parametrize("tail", ["RUNNER_OK\n", "RUNNER_OK", "device fake: 1 line(s)\nRUNNER_OK\n"])
def test_runner_ok_is_read_from_the_log_tail(tmp_path, tail):
    log = tmp_path / "tts.log"
    log.write_text(tail)
    assert voice._runner_ok(log)


@pytest.mark.parametrize("text", ["", "no marker here\n", "RUNNER_OK is not the last word\nmore\nlines\nafter\nit\n"])
def test_runner_ok_needs_the_marker_as_a_line_near_the_end(tmp_path, text):
    log = tmp_path / "tts.log"
    log.write_text(text)
    assert not voice._runner_ok(log)
    assert not voice._runner_ok(tmp_path / "missing.log")


def test_the_timeout_outlasts_the_runners_lock_wait():
    """The runner may wait LOCK_WAIT_S for the shared lock, then synthesize the budget maximum (210 words) at the
    measured CPU rate (~30 s a word) and load the model (~300 s with margin)."""
    assert voice.TTS_TIMEOUT_S >= tts_runner.LOCK_WAIT_S + 210 * 30 + 300
    assert gate.WORDS_MAX == 210  # the pin above is sized for this maximum


@needs_ffmpeg
@pytest.mark.parametrize("case", ["transcript", "none"])
def test_the_whole_stage_with_a_fake_runner_process(case, tmp_path, monkeypatch, fake_runner):
    """Real run_tts and real post_process around a runner process, with only the host's hash check and Whisper faked."""
    w = M.work_dir(tmp_path, case)
    monkeypatch.setattr(voiceconf, "ref_ok", lambda v: (True, "abc123def4567890"))
    monkeypatch.setattr(align, "run_whisper", heard_all)
    r = voice.run_stage(ctx(w))
    assert r.ok, r.reason
    lines = stage_lines(w)
    assert (w / "voice" / "confessional.raw.wav").exists() == (case != "none")
    assert [words_of(w, ln["id"])["source"] for ln in lines] == ["whisper"] * len(lines)
    assert manifest_of(w)["tts_target"] == "local" and manifest_of(w)["device"] == "fake"


@pytest.mark.parametrize("mode,code", [("wrong_ref", "tts_results_ref"), ("wrong_ids", "tts_results_ids"),
                                       ("missing_wav", "tts_wav_missing"), ("no_results", "tts_results_unreadable"),
                                       ("exit_1", "tts_runner_failed"), ("no_marker", "tts_runner_failed")])
def test_the_stage_fails_with_a_code_when_the_runners_work_is_not_proved(tmp_path, monkeypatch, fake_runner, mode,
                                                                         code):
    w = M.work_dir(tmp_path)
    monkeypatch.setattr(voiceconf, "ref_ok", lambda v: (True, "abc123def4567890"))
    monkeypatch.setattr(align, "run_whisper", lambda *a, **k: (_ for _ in ()).throw(AssertionError("whisper ran")))
    monkeypatch.setenv("FAKE_RUNNER_MODE", mode)
    r = voice.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, f"voice:{code}")


def test_a_runner_that_timed_out_is_a_code(tmp_path, monkeypatch, fake_runner):
    w = M.work_dir(tmp_path)
    monkeypatch.setattr(voiceconf, "ref_ok", lambda v: (True, "abc123def4567890"))
    monkeypatch.setenv("FAKE_RUNNER_MODE", "sleep")
    monkeypatch.setattr(voice, "TTS_TIMEOUT_S", 1)
    r = voice.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "voice:TimeoutExpired")


# ---- run_tts: where voicing runs (tts.target), with the remote and the local runner faked ----

REF = "abc123def4567890"
REMOTE_EXTRA = ('[tts]\npython = "/opt/tts/bin/python"\n{target}host = "gpu-box"\nremote_python = "/opt/tts/bin/python"\n'
                'remote_dir = "/var/tmp/tts-runs"\nremote_gpu_allow = ["model-server"]\n\n'
                '[whisper]\npython = "/opt/whisper/bin/python"\n')
TLINES = [{"id": "cold_open", "scene": "cold_open", "voice": "host", "text": "hello there", "params": {}},
          {"id": "out", "scene": "out", "voice": "host", "text": "goodbye now", "params": {}}]


class Targets:
    """Fakes for both targets. The remote either succeeds (fetching a results file that also carries bogus
    tts_target and remote_skip keys) or raises RemoteSkip(skip), after opening the log when log_opened. The local
    runner writes its own results. Every call is recorded."""

    def __init__(self, monkeypatch, skip=None, log_opened=True, device="cuda"):
        self.remote_calls, self.local_calls, self.pruned = [], [], []
        self.skip, self.log_opened, self.device = skip, log_opened, device

        def remote(rem, lines, voice_dir, voices_bytes, day, check):
            self.remote_calls.append({"remote": rem, "day": day, "voices_bytes": voices_bytes, "check": check})
            if self.skip:
                if self.log_opened:
                    (Path(voice_dir) / "tts.log").write_text("device cuda: 2 line(s)\nRUNNER_OK\n")
                e = tts_remote.RemoteSkip(self.skip)
                e.log_opened = self.log_opened
                raise e
            for ln in lines:
                (Path(voice_dir) / f"{ln['id']}.wav").write_bytes(b"RIFF")
            files.write_json(Path(voice_dir) / voice.TTS_RESULTS,
                             {"device": self.device, "ref_sha256": REF, "lines": [{"id": ln["id"]} for ln in lines],
                              "tts_target": "bogus", "remote_skip": "bogus"})
            return check(lines, voice_dir)

        def local(lines, voice_dir, voices_path, ref, settings):
            self.local_calls.append(dict(os.environ))
            for ln in lines:
                (Path(voice_dir) / f"{ln['id']}.wav").write_bytes(b"RIFF")
            files.write_json(Path(voice_dir) / voice.TTS_RESULTS,
                             {"device": "cpu", "ref_sha256": ref, "lines": [{"id": ln["id"]} for ln in lines]})
            return voice.check_results(lines, voice_dir, ref)

        monkeypatch.setattr(tts_remote, "run", remote)
        monkeypatch.setattr(voice, "_run_local", local)
        monkeypatch.setattr(tts_remote, "prune_after", lambda rem, vd, log_name: self.pruned.append(log_name))


@pytest.fixture
def tvd(tmp_path):
    vd = tmp_path / "voice"
    vd.mkdir()
    files.write_json(vd / "lines.json", TLINES)
    return vd


def remote_settings(monkeypatch, tmp_path, target=None):
    line = f'target = "{target}"\n' if target else ""
    return use(monkeypatch, tmp_path, name=f"remote-{target or 'auto'}", extra=REMOTE_EXTRA.format(target=line))


def go(tvd, settings, **kw):
    return voice.run_tts(TLINES, tvd, VP(), REF, settings=settings, voices_sha=voices_sha(), day=DAY, **kw)


def test_auto_with_a_clear_remote_voices_there_and_never_touches_the_local_runner(tvd, tmp_path, monkeypatch):
    t = Targets(monkeypatch)
    res = go(tvd, remote_settings(monkeypatch, tmp_path))
    assert t.local_calls == [] and len(t.remote_calls) == 1
    assert res["tts_target"] == "remote" and res["device"] == "cuda" and "remote_skip" not in res
    saved = json.loads((tvd / voice.TTS_RESULTS).read_text())
    assert saved["tts_target"] == "remote" and "remote_skip" not in saved  # the fetched keys are never kept
    assert t.pruned == ["tts.log"]


def test_the_remote_gets_the_settings_the_hashed_bytes_and_the_day(tvd, tmp_path, monkeypatch):
    t = Targets(monkeypatch)
    go(tvd, remote_settings(monkeypatch, tmp_path))
    (call,) = t.remote_calls
    assert call["remote"] == tts_remote.Remote("gpu-box", "/opt/tts/bin/python", "/var/tmp/tts-runs",
                                               ("model-server",))
    assert call["voices_bytes"] == VP().read_bytes() and call["day"] == DAY


def test_the_remote_check_is_the_stages_evidence_check_with_this_digest(tvd, tmp_path, monkeypatch):
    t = Targets(monkeypatch)
    go(tvd, remote_settings(monkeypatch, tmp_path))
    check = t.remote_calls[0]["check"]
    assert check(TLINES, tvd)["device"] == "cuda"  # positive control: the fetched results prove the run
    results = json.loads((tvd / voice.TTS_RESULTS).read_text())
    results["ref_sha256"] = "0" * 16
    files.write_json(tvd / voice.TTS_RESULTS, results)
    with pytest.raises(voice.VoiceError, match="tts_results_ref"):
        check(TLINES, tvd)


@pytest.mark.parametrize("code", tts_remote.CODES)
def test_every_remote_failure_falls_back_to_local_once_with_a_code(tvd, tmp_path, monkeypatch, code):
    t = Targets(monkeypatch, skip=code)
    res = go(tvd, remote_settings(monkeypatch, tmp_path))
    assert len(t.remote_calls) == 1 and len(t.local_calls) == 1  # never retried
    assert res["tts_target"] == "local" and res["remote_skip"] == code
    assert json.loads((tvd / voice.TTS_RESULTS).read_text())["remote_skip"] == code
    assert t.pruned == ([] if code == "unreachable" else ["tts-remote.log"])  # ssh works otherwise


def test_the_remote_log_is_kept_beside_the_local_one(tvd, tmp_path, monkeypatch):
    Targets(monkeypatch, skip="runner_failed")
    go(tvd, remote_settings(monkeypatch, tmp_path))
    assert "RUNNER_OK" in (tvd / "tts-remote.log").read_text()


def test_the_log_is_not_renamed_when_the_remote_never_opened_it(tvd, tmp_path, monkeypatch):
    (tvd / "tts.log").write_text("an earlier log\n")
    Targets(monkeypatch, skip="busy", log_opened=False)
    go(tvd, remote_settings(monkeypatch, tmp_path))
    assert not (tvd / "tts-remote.log").exists()


def test_target_remote_forces_the_remote_and_never_falls_back(tvd, tmp_path, monkeypatch):
    settings = remote_settings(monkeypatch, tmp_path, "remote")
    t = Targets(monkeypatch)
    assert go(tvd, settings)["tts_target"] == "remote" and t.local_calls == []
    t = Targets(monkeypatch, skip="busy")
    with pytest.raises(voice.VoiceError) as e:
        go(tvd, settings)
    assert t.local_calls == [] and voice.failure_code(e.value) == "voice:tts_remote_busy"


def test_target_local_never_calls_the_remote(tvd, tmp_path, monkeypatch):
    t = Targets(monkeypatch)
    res = go(tvd, remote_settings(monkeypatch, tmp_path, "local"))
    assert t.remote_calls == [] and len(t.local_calls) == 1
    assert res["tts_target"] == "local" and "remote_skip" not in res


def test_auto_without_a_host_runs_locally_only(tvd, tmp_path, monkeypatch):
    t = Targets(monkeypatch)
    res = go(tvd, STATE["settings"])
    assert t.remote_calls == [] and res["tts_target"] == "local" and voice._targets(STATE["settings"]) == ("local",)


def test_a_remote_target_with_no_host_is_a_code():
    s = SimpleNamespace(tts_target="remote", tts_host=None)
    with pytest.raises(voice.VoiceError) as e:
        voice.run_tts(TLINES, Path("/nonexistent"), paths.REPO / "config.example" / "voices.json", REF, settings=s)
    assert voice.failure_code(e.value) == "voice:tts_remote_unconfigured"


def test_targets_follow_the_setting():
    assert voice.TTS_TARGETS == ("remote", "local")
    host = dict(tts_host="gpu-box")
    assert voice._targets(SimpleNamespace(tts_target="auto", **host)) == ("remote", "local")
    assert voice._targets(SimpleNamespace(tts_target="auto", tts_host=None)) == ("local",)
    assert voice._targets(SimpleNamespace(tts_target="remote", **host)) == ("remote",)
    assert voice._targets(SimpleNamespace(tts_target="local", **host)) == ("local",)


def test_changed_voices_bytes_stop_before_any_remote_call(tvd, tmp_path, monkeypatch):
    settings = remote_settings(monkeypatch, tmp_path)
    t = Targets(monkeypatch)
    with pytest.raises(voice.VoiceError) as e:
        voice.run_tts(TLINES, tvd, VP(), REF, settings=settings, voices_sha="0" * 64, day=DAY)
    assert e.value.args == ("voice_config_changed",) and t.remote_calls == [] and t.local_calls == []


def test_local_runs_never_require_cuda_and_leave_the_hub_online(tvd, tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    t = Targets(monkeypatch)
    go(tvd, remote_settings(monkeypatch, tmp_path, "local"))
    assert tts_runner.REQUIRE_CUDA_ENV not in t.local_calls[0] and "HF_HUB_OFFLINE" not in os.environ


def test_the_manifest_records_where_voicing_ran(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)

    def tts(lines, voice_dir, voices_path, ref):
        fast_tts(lines, voice_dir, voices_path, ref)
        return {"device": "cpu", "tts_target": "local", "remote_skip": "busy", "lines": []}

    Fakes(monkeypatch, tts=tts)
    r = voice.run_stage(ctx(w))
    assert r.ok
    m = manifest_of(w)
    assert (m["tts_target"], m["device"], m["remote_skip"]) == ("local", "cpu", "busy")


def test_write_manifest_keeps_only_the_target_keys(tmp_path):
    voice.write_manifest(tmp_path, TLINES, {"cold_open": "", "out": ""}, "v" * 64, REF,
                         tts={"tts_target": "remote", "device": "cuda", "lines": ["x"], "synth_seconds": 3.0})
    m = json.loads((tmp_path / voice.MANIFEST).read_text())
    assert m["tts_target"] == "remote" and "remote_skip" not in m and "synth_seconds" not in m


def test_the_stage_passes_the_hashed_voices_and_its_day_to_voicing(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    fk = Fakes(monkeypatch)
    assert voice.run_stage(ctx(w)).ok
    call = fk.tts_calls[0]
    assert call["voices_sha"] == voices_sha() and call["day"] == DAY


def test_a_voices_file_that_changed_before_voicing_is_voice_config_changed(tmp_path, monkeypatch):
    w = M.work_dir(tmp_path)
    Fakes(monkeypatch, tts=lambda *a, **k: (_ for _ in ()).throw(voice.VoiceError("voice_config_changed")))
    r = voice.run_stage(ctx(w))
    assert (r.ok, r.reason) == (False, "voice_config_changed")


# ---- post_process ----

def make_take(path, seconds=1.0, freq=220):
    return M.tone(path, seconds, freq, sr=24000, ch=1)


@needs_ffmpeg
def test_post_process_keeps_the_raw_take(tmp_path):
    wav = make_take(tmp_path / "confessional.wav", 1.0)
    original = wav.read_bytes()
    voice.post_process(wav, M.voices()["codex"]["post"])
    raw = tmp_path / "confessional.raw.wav"
    assert raw.read_bytes() == original and wav.read_bytes() != original
    assert abs(voice.wav_seconds(wav) - 1.0 / 0.93) < 0.05  # the agent voice plays at 0.93 speed
    (stream,) = probe_audio(wav)
    assert stream["channels"] == 1 and int(stream["sample_rate"]) == voice.SR
    assert sorted(p.name for p in tmp_path.iterdir()) == ["confessional.raw.wav", "confessional.wav"]  # no temp left


# every chain at 4 s; the chain with an echo (the one the length rule needed care for) also short and long
CHAIN_LENGTHS = [(src, h, 4.0) for src, h in ALL_AGENT_VOICES] + [("fixture", "my_agent", s) for s in (1.0, 7.3)]


@needs_ffmpeg
@pytest.mark.parametrize("source,harness,seconds", CHAIN_LENGTHS)
def test_every_shipped_agent_chain_passes_the_raw_length_check(tmp_path, monkeypatch, source, harness, seconds):
    """The measurement behind the check: real ffmpeg, the example and fixture chains. A chain's take is raw /
    chain_speed long (0.93 speed on purpose); an echo adds its tail."""
    chain = agent_voice(source, harness)["post"]
    wav = make_take(tmp_path / "line.wav", seconds)
    voice.post_process(wav, chain)
    skew = voice.wav_seconds(wav) - voice.wav_seconds(tmp_path / "line.raw.wav") / voice.chain_speed(chain)
    assert -voice.RAW_TOLERANCE_S <= skew <= voice.echo_tail_s(chain) + voice.RAW_TOLERANCE_S
    monkeypatch.setattr(align, "run_whisper", heard_all)
    line = {"id": "line", "scene": "story", "voice": harness, "text": "one two three", "params": {}}
    assert voice.align_lines([line], tmp_path, {"line": chain})[0][0] == "line"  # no VoiceError


@needs_ffmpeg
def test_an_echo_makes_the_take_longer_by_its_delay_and_a_plain_chain_does_not(tmp_path):
    """Pins the premise of echo_tail_s: if ffmpeg stops adding the tail, or adds more, this says so."""
    voices = M.voices()
    for harness, tail in (("claude_code", 0.0), ("codex", 0.0), ("my_agent", 0.04), ("night_bot", 0.0)):
        wav = make_take(tmp_path / f"{harness}.wav", 4.0)
        voice.post_process(wav, voices[harness]["post"])
        skew = voice.wav_seconds(wav) - voice.wav_seconds(tmp_path / f"{harness}.raw.wav") / voice.chain_speed(
            voices[harness]["post"])
        assert voice.echo_tail_s(voices[harness]["post"]) == pytest.approx(tail)
        assert tail - 0.02 <= skew <= tail + 0.02, harness


@pytest.mark.parametrize("chain,tail", [
    ("asetrate=24000*1.06,aresample=24000,atempo=0.9434", 0.0),
    ("asetrate=24000*0.9,aresample=24000,atempo=1.1111,aecho=0.8:0.5:14:0.2", 0.014),
    ("asetrate=24000*0.9,aecho=0.8:0.5:60:0.2,atempo=1.1,aecho=0.8:0.5:120:0.2", 0.18),
    ("asetrate=24000*0.9,aecho=0.8:0.5", 0.0),
    ("asetrate=24000*0.9,aecho=0.8:0.5:slow:0.2", 0.0),
    ("asetrate=24000*0.9,aechox=0.8:0.5:99:0.2", 0.0),
])
def test_echo_tail_reads_the_delays_of_a_chain(chain, tail):
    assert voice.echo_tail_s(chain) == pytest.approx(tail)


def aligned_with_a_final_take_that_is_off_by(tmp_path, monkeypatch, skew, chain):
    fast_audio(monkeypatch)
    write_wav(tmp_path / "line.raw.wav", 2.0)
    write_wav(tmp_path / "line.wav", 2.0 + skew)
    monkeypatch.setattr(align, "run_whisper", heard_all)
    line = {"id": "line", "scene": "story", "voice": "codex", "text": "one two three", "params": {}}
    return voice.align_lines([line], tmp_path, {"line": chain})


PLAIN = "asetrate=24000*1.06,aresample=24000,atempo=0.9434"
ECHO_120 = PLAIN + ",aecho=0.8:0.6:120:0.25"


@pytest.mark.parametrize("skew,chain", [(0.04, PLAIN), (-0.04, PLAIN), (0.16, ECHO_120), (-0.04, ECHO_120)])
def test_a_final_take_within_the_allowed_skew_aligns(tmp_path, monkeypatch, skew, chain):
    assert aligned_with_a_final_take_that_is_off_by(tmp_path, monkeypatch, skew, chain) == [("line", 1.0)]


@pytest.mark.parametrize("skew,chain", [(0.07, PLAIN), (-0.07, PLAIN), (0.2, ECHO_120), (-0.07, ECHO_120)])
def test_a_final_take_outside_the_allowed_skew_is_refused(tmp_path, monkeypatch, skew, chain):
    with pytest.raises(voice.VoiceError):
        aligned_with_a_final_take_that_is_off_by(tmp_path, monkeypatch, skew, chain)


def test_align_lines_hands_whisper_the_interpreter_and_model(tmp_path, monkeypatch):
    fast_audio(monkeypatch)
    write_wav(tmp_path / "line.wav", 1.2)
    seen = []

    def whisper(jobs, manifest, python=None, model=None, log=None):
        seen.append((python, model, Path(manifest).name))
        heard_all(jobs, manifest)

    monkeypatch.setattr(align, "run_whisper", whisper)
    line = {"id": "line", "scene": "story", "voice": "host", "text": "one two three", "params": {}}
    assert voice.align_lines([line], tmp_path, python="/opt/whisper/bin/python", model="tiny.en") == [("line", 1.0)]
    assert seen == [("/opt/whisper/bin/python", "tiny.en", "whisper-manifest.json")]


def test_an_empty_chain_changes_nothing_and_runs_nothing(tmp_path, monkeypatch):
    wav = tmp_path / "line.wav"
    write_wav(wav, 1.0)
    before = wav.read_bytes()
    calls = capture_run(monkeypatch)
    voice.post_process(wav, "")
    assert calls == [] and wav.read_bytes() == before and [p.name for p in tmp_path.iterdir()] == ["line.wav"]


@pytest.mark.parametrize("chain", ["lowpass=f=100", "asetrate=24000*1.1,aevalsrc=0", "asetrate=24000*1.1;volume=2",
                                   "asetrate=24000*1.1,amovie=file=/etc/passwd", "asetrate=24000*1.1,volume=2 "])
def test_a_chain_outside_the_allowlist_is_refused_before_ffmpeg_runs(tmp_path, monkeypatch, chain):
    wav = tmp_path / "line.wav"
    write_wav(wav, 1.0)
    before = wav.read_bytes()
    calls = capture_run(monkeypatch)
    with pytest.raises(voiceconf.VoiceConfigError):
        voice.post_process(wav, chain)
    assert calls == [] and wav.read_bytes() == before and [p.name for p in tmp_path.iterdir()] == ["line.wav"]


def test_post_process_runs_ffmpeg_as_one_argv_list_with_nostdin_and_a_timeout(tmp_path, monkeypatch):
    wav = tmp_path / "line.wav"
    write_wav(wav, 1.0)
    chain = M.voices()["my_agent"]["post"]
    calls = capture_run(monkeypatch)
    voice.post_process(wav, chain)
    ((argv, kw),) = calls
    assert isinstance(argv, list) and argv[0] == "ffmpeg" and "-nostdin" in argv
    assert argv[argv.index("-af") + 1] == chain and argv[argv.index("-i") + 1] == str(wav)
    assert not kw.get("shell") and kw.get("check") is True and 0 < kw["timeout"] <= 120
    assert Path(argv[-1]).parent == tmp_path and Path(argv[-1]) not in (wav, tmp_path / "line.raw.wav")  # a temp file


@pytest.mark.parametrize("failure", [
    subprocess.CalledProcessError(1, "ffmpeg"),
    subprocess.TimeoutExpired("ffmpeg", 120),
    KeyboardInterrupt(),
])
def test_a_failing_chain_leaves_the_take_as_it_was(tmp_path, monkeypatch, failure):
    wav = tmp_path / "line.wav"
    write_wav(wav, 1.0)
    before = wav.read_bytes()

    def half_written(argv, **kw):
        Path(argv[-1]).write_bytes(b"RIFF-truncated")
        raise failure

    monkeypatch.setattr(voice.subprocess, "run", half_written)
    with pytest.raises(type(failure)):
        voice.post_process(wav, M.voices()["codex"]["post"])
    assert [p.name for p in tmp_path.iterdir()] == ["line.wav"] and wav.read_bytes() == before


@needs_ffmpeg
def test_a_replace_that_fails_halfway_puts_the_take_back(tmp_path, monkeypatch):
    wav = make_take(tmp_path / "line.wav")
    before = wav.read_bytes()
    real = os.replace

    def flaky(src, dst):
        if Path(dst) == wav and Path(src).name.endswith(".part.wav"):
            raise OSError("disk full")
        return real(src, dst)

    monkeypatch.setattr(voice.os, "replace", flaky)
    with pytest.raises(OSError):
        voice.post_process(wav, M.voices()["codex"]["post"])
    assert [p.name for p in tmp_path.iterdir()] == ["line.wav"] and wav.read_bytes() == before


# ---- the helper that dry-voices a work dir goes through the stage ----

@needs_ffmpeg
def test_dry_work_is_the_stage_in_dry_mode(dry_case):
    case, w = dry_case
    assert (w / "hostlint.json").exists()  # only the stage writes it
    assert {words_of(w, ln["id"])["source"] for ln in stage_lines(w)} == {"dry"}


@pytest.mark.parametrize("code", sorted(voice.VOICE_CODES))
def test_each_voice_code_is_carried_into_the_reason(code):
    assert voice.failure_code(voice.VoiceError(code)) == f"voice:{code}"


@pytest.mark.parametrize("msg", ["tts_runner_failed /home/someone/secret", "/home/someone/secret", "",
                                 "empty lines: story", "quote_kind", "TTS_RUNNER_FAILED"])
def test_voice_error_text_that_is_not_a_listed_code_never_leaks(msg):
    assert voice.failure_code(voice.VoiceError(msg)) == "voice:VoiceError"
    assert voice.failure_code(voice.VoiceError("raw_length", "/home/someone/secret")) == "voice:VoiceError"
    assert voice.failure_code(RuntimeError("raw_length")) == "voice:RuntimeError"


def test_a_lines_scene_is_composes_scene_for_every_id():
    lines = [ln for case in CASES for ln in lines_for(case)]
    assert lines and all(ln["scene"] == compose.scene_of(ln["id"]) for ln in lines)
    assert not hasattr(voice, "SCENES")  # one scene list: compose.SCENES


# ---- the agent voices: intelligible, slowed to 0.93, never the host ----

@pytest.mark.parametrize("source,harness", ALL_AGENT_VOICES)
def test_every_shipped_agent_voice_runs_at_93_percent_speed(source, harness):
    assert voice.chain_speed(agent_voice(source, harness)["post"]) == pytest.approx(0.93, abs=0.002)


@pytest.mark.parametrize("source", ["example", "fixture"])
def test_the_shipped_agent_chains_are_light_wide_band_and_keep_a_distinct_pitch_step(source):
    voices = EXAMPLE_VOICES if source == "example" else FIXTURE_VOICES
    pitches = []
    for harness in voiceconf.harnesses(voices):
        chain = voices[harness]["post"]
        names = {part.partition("=")[0]: part.partition("=")[2] for part in chain.split(",")}
        assert chain.startswith(voiceconf.POST_START) and not {"flanger", "acrusher"} & set(names), harness
        if "highpass" in names:
            assert float(names["highpass"].removeprefix("f=")) <= 100, harness
        if "lowpass" in names:
            assert float(names["lowpass"].removeprefix("f=")) >= 7000, harness
        assert voice.echo_tail_s(chain) <= 0.06, harness  # an echo, if any, is light
        pitches.append(float(chain.split(",")[0].split("*")[1]))
    assert len(set(pitches)) == len(pitches) and all(abs(p - 1.0) >= 0.02 for p in pitches)  # none at the host's pitch


@needs_ffmpeg
@pytest.mark.parametrize("source,harness", ALL_AGENT_VOICES)
def test_an_agent_take_that_lost_its_atempo_is_refused(tmp_path, monkeypatch, source, harness):
    """The real chain's length is allowed; the same take with the tempo step missing is not."""
    chain = agent_voice(source, harness)["post"]
    wav = make_take(tmp_path / "line.wav", 4.0)
    voice.post_process(wav, chain)
    stripped = ",".join(p for p in chain.split(",") if not p.startswith("atempo="))
    monkeypatch.setattr(align, "run_whisper", heard_all)
    line = {"id": "line", "scene": "story", "voice": harness, "text": "one two three", "params": {}}
    assert voice.align_lines([line], tmp_path, {"line": chain})[0][0] == "line"
    with pytest.raises(voice.VoiceError):
        voice.align_lines([line], tmp_path, {"line": stripped})


@pytest.mark.parametrize("chain,speed", [("asetrate=24000*1.06,aresample=24000,atempo=0.8774", 0.93),
                                         ("asetrate=24000*0.9,aresample=24000,atempo=1.0333", 0.93),
                                         ("asetrate=24000*1.1,aresample=24000", 1.1),
                                         ("asetrate=24000*x,atempo=y", 1.0), ("", 1.0)])
def test_chain_speed_multiplies_the_pitch_and_tempo_steps(chain, speed):
    assert voice.chain_speed(chain) == pytest.approx(speed, abs=0.001)
