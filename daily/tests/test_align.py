"""Word alignment (stdlib) and the Whisper runner's contract, tested in plain Python with no faster-whisper: the
runner script is loaded against a fake `faster_whisper`, run_whisper against a fake subprocess. The one venv test only
proves a faster-whisper environment can import the script's dependencies, and runs only when
$ORION_CLOCK_TEST_WHISPER_PYTHON names that environment's Python; real transcription is a manual check."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from daily import align

LINE = "Gardens matter because seeds sprout every single week."
PY = "/opt/whisper-env/bin/python"  # a synthetic interpreter path: run_whisper only hands it to the (faked) child
VENV_PY = os.environ.get("ORION_CLOCK_TEST_WHISPER_PYTHON")


def W(word, s, e):
    return {"word": word, "start": s, "end": e}


def test_exact_match():
    words, cov = align.align_words("We shipped it today.",
                                   [W("We", 0.1, 0.3), W("shipped", 0.3, 0.7), W("it", 0.7, 0.8),
                                    W("today.", 0.8, 1.2)], 1.5)
    assert cov == 1.0
    assert [w["word"] for w in words] == ["We", "shipped", "it", "today."]
    assert words[1] == {"word": "shipped", "start": 0.3, "end": 0.7}


def test_joined_timed_words_cover_one_script_word():
    words, cov = align.align_words("state-of-the-art tool",
                                   [W("state", 0.0, 0.2), W("of", 0.2, 0.3), W("the", 0.3, 0.4),
                                    W("art", 0.4, 0.6), W("tool", 0.7, 1.0)], 1.0)
    assert cov == 1.0
    assert words[0] == {"word": "state-of-the-art", "start": 0.0, "end": 0.6}


def test_one_timed_word_covers_merged_script_words():
    words, cov = align.align_words("Set up now", [W("Setup", 0.0, 0.5), W("now", 0.6, 0.9)], 1.0)
    assert cov == 1.0
    assert words[0] == {"word": "Set", "start": 0.0, "end": 0.3}
    assert words[1] == {"word": "up", "start": 0.3, "end": 0.5}


def test_missed_words_are_interpolated_and_coverage_drops():
    words, cov = align.align_words("one two three four", [W("one", 0.0, 0.2), W("four", 0.8, 1.0)], 1.0)
    assert cov == 0.5
    assert words[1] == {"word": "two", "start": 0.2, "end": 0.5}
    assert words[2] == {"word": "three", "start": 0.5, "end": 0.8}


def test_times_clamp_to_audio_length_and_never_go_backwards():
    words, _ = align.align_words("one two three four", [W("one", 0.0, 0.2), W("four", 0.8, 1.0)], 0.9)
    assert words[-1]["end"] == 0.9
    starts = [w["start"] for w in words]
    assert all(b >= a for a, b in zip(starts, starts[1:]))
    assert all(w["end"] >= w["start"] for w in words)


def test_nothing_heard_is_zero_coverage():
    words, cov = align.align_words("one two", [], 1.0)
    assert cov == 0.0 and len(words) == 2


def test_glossary_leaves_out_the_first_word_and_sorts():
    assert align.glossary("Gardens matter because seeds sprout") == "Glossary: because, matter, seeds, sprout"


# ---------- the match floor and spelling ----------

def test_the_match_floor_is_sixty_percent():
    assert align.MIN_COVERAGE == 0.6
    # 3 of 5 line words heard is exactly the floor; 2 of 5 is under it
    heard3 = [W("one", 0.0, 0.1), W("three", 0.2, 0.3), W("five", 0.4, 0.5)]
    _, at_floor = align.align_words("one two three four five", heard3, 1.0)
    _, under = align.align_words("one two three four five", heard3[:2], 1.0)
    assert at_floor == 0.6 and not at_floor < align.MIN_COVERAGE
    assert under == 0.4 and under < align.MIN_COVERAGE


def test_captions_keep_the_lines_own_spelling_not_the_recognizers():
    words, cov = align.align_words("Codex shipped Nimbus.", [W("codecs", 0.0, 0.3), W("shipped", 0.3, 0.6),
                                                            W("nimbus", 0.6, 0.9)], 1.0)
    assert [w["word"] for w in words] == ["Codex", "shipped", "Nimbus."]
    assert cov == pytest.approx(2 / 3, abs=0.001)  # "Codex" was misheard, so it is interpolated, not counted


def test_punctuation_and_accents_do_not_break_a_match():
    words, cov = align.align_words("Cafe, “fast”!", [W("café", 0.0, 0.4), W("fast", 0.5, 0.8)], 1.0)
    assert cov == 1.0 and [w["word"] for w in words] == ["Cafe,", "“fast”!"]


def test_a_dash_token_is_timed_but_not_counted():
    words, cov = align.align_words("one - two", [W("one", 0.0, 0.2), W("two", 0.5, 0.7)], 1.0)
    assert cov == 1.0 and [w["word"] for w in words] == ["one", "-", "two"]
    assert 0.2 <= words[1]["start"] and words[1]["end"] <= 0.5


def test_empty_line_has_no_words():
    assert align.align_words("", [W("x", 0.0, 0.1)], 1.0) == ([], 0.0)


# ---------- the glossary: never the line itself ----------

def test_glossary_is_not_the_line_and_never_holds_its_first_word():
    g = align.glossary(LINE)
    assert g != LINE and LINE not in g
    assert "Gardens" not in g
    body = g.removeprefix("Glossary: ").split(", ")
    assert body == sorted(body, key=str.lower)  # alphabetical, so the line's word order is not replayed
    assert body != [w.strip(".") for w in LINE.split() if len(w.strip(".")) >= 4][1:]


def test_glossary_is_none_when_there_is_nothing_to_hint():
    assert align.glossary("Hi there") is None  # one eligible word, and it is the opening word
    assert align.glossary("") is None


def test_glossary_does_not_echo_its_own_label_as_the_opening_word():
    assert align.glossary("Glossary words matter").startswith("Terms: ")


def test_glossary_stays_inside_the_byte_limit():
    text = " ".join(f"word{c}{d}" for c in "abcdefghij" for d in "klmnop")
    g = align.glossary(text, limit=60)
    assert g is not None and len(g.encode("utf-8")) <= 60


# ---------- whisper_words.py against a fake faster_whisper ----------

def load_runner(monkeypatch, transcribe):
    """Load whisper_words.py as a script would see it, with `faster_whisper` replaced by a fake model class."""
    made = []

    class FakeModel:
        def __init__(self, name, **kw):
            made.append((name, kw))

        def transcribe(self, audio, **kw):
            return transcribe(audio, **kw)

    fake = ModuleType("faster_whisper")
    fake.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    monkeypatch.setattr(sys, "path", list(sys.path))  # the script prepends to sys.path; undo it afterwards
    spec = importlib.util.spec_from_file_location("whisper_words_under_test", align.WHISPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, made


def heard(*triples):
    return [SimpleNamespace(word=w, start=s, end=e) for w, s, e in triples]


def test_the_prompt_is_the_glossary_never_the_line(tmp_path, monkeypatch, capsys):
    calls = []

    def transcribe(audio, **kw):
        calls.append((audio, kw))
        return iter([SimpleNamespace(words=heard((" We ", 0.1, 0.3), ("ship", 0.3, 0.6)))]), SimpleNamespace()

    mod, _ = load_runner(monkeypatch, transcribe)
    out = tmp_path / "a.json"
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps([{"audio": str(tmp_path / "a.wav"), "text": LINE, "out": str(out)}]))
    mod.main(["whisper_words.py", str(manifest)])
    (audio, kw), = calls
    prompt = kw["initial_prompt"]
    assert prompt == align.glossary(LINE)
    assert prompt != LINE and LINE not in prompt and "Gardens" not in prompt
    assert kw["word_timestamps"] is True
    assert capsys.readouterr().out.strip().splitlines()[-1] == "WHISPER_OK"


def test_no_prompt_at_all_when_the_line_has_no_glossary(tmp_path, monkeypatch):
    calls = []

    def transcribe(audio, **kw):
        calls.append(kw)
        return iter([]), SimpleNamespace()

    mod, _ = load_runner(monkeypatch, transcribe)
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps([{"audio": "a.wav", "text": "Hi there", "out": str(tmp_path / "a.json")}]))
    mod.main(["whisper_words.py", str(manifest)])
    assert calls[0]["initial_prompt"] is None


def test_the_runner_writes_clean_word_lists_and_loads_the_model_once(tmp_path, monkeypatch):
    monkeypatch.delenv("ORION_CLOCK_WHISPER_MODEL", raising=False)
    monkeypatch.delenv("ORION_CLOCK_WHISPER_THREADS", raising=False)

    def transcribe(audio, **kw):
        segs = [SimpleNamespace(words=heard((" We ", 0.1004, 0.3), ("  ", 0.3, 0.4), ("x", None, 0.5),
                                            ("ship", 0.3, None), ("it", 0.4, 0.7))),
                SimpleNamespace(words=None)]
        return iter(segs), SimpleNamespace()

    mod, made = load_runner(monkeypatch, transcribe)
    manifest = tmp_path / "m.json"
    outs = [tmp_path / "a.json", tmp_path / "b.json"]
    manifest.write_text(json.dumps([{"audio": "a.wav", "text": LINE, "out": str(outs[0])},
                                    {"audio": "b.wav", "text": LINE, "out": str(outs[1])}]))
    mod.main(["whisper_words.py", str(manifest)])
    expected = [{"word": "We", "start": 0.1, "end": 0.3}, {"word": "it", "start": 0.4, "end": 0.7}]
    assert [json.loads(o.read_text()) for o in outs] == [expected, expected]
    assert len(made) == 1  # one model load for the whole batch
    name, kw = made[0]
    assert name == "small.en" and kw["device"] == "cpu" and kw["compute_type"] == "int8"
    assert kw["cpu_threads"] == 4  # the defaults when neither variable is set


def test_the_runner_reads_its_model_and_threads_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ORION_CLOCK_WHISPER_MODEL", "base.en")
    monkeypatch.setenv("ORION_CLOCK_WHISPER_THREADS", "2")
    mod, made = load_runner(monkeypatch, lambda audio, **kw: (iter([]), SimpleNamespace()))
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps([{"audio": "a.wav", "text": LINE, "out": str(tmp_path / "a.json")}]))
    mod.main(["whisper_words.py", str(manifest)])
    (name, kw), = made
    assert name == "base.en" and kw["cpu_threads"] == 2
    assert align.MODEL_ENV == "ORION_CLOCK_WHISPER_MODEL"  # what run_whisper sets for the child


def test_the_runner_needs_exactly_one_argument(monkeypatch):
    mod, _ = load_runner(monkeypatch, lambda audio, **kw: (iter([]), None))
    with pytest.raises(SystemExit):
        mod.main(["whisper_words.py"])


# ---------- run_whisper: argv, timeout, no shell ----------

def jobs_in(tmp_path):
    return [{"audio": str(tmp_path / "a.wav"), "text": LINE, "out": str(tmp_path / "a.json")}]


def fake_whisper(rc=0, text="WHISPER_OK\n", out=None):
    """A stand-in for subprocess.run that writes `text` to the stdout file it is handed, as the real child would."""
    def fake_run(argv, **kw):
        kw["stdout"].write(text)
        kw["stdout"].flush()
        if out is not None:
            Path(out).write_text("[]")
        return subprocess.CompletedProcess(argv, rc)
    return fake_run


def test_run_whisper_calls_the_script_with_the_given_python_as_argv_and_a_timeout(tmp_path, monkeypatch):
    jobs = jobs_in(tmp_path)
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        Path(jobs[0]["out"]).write_text("[]")
        kw["stdout"].write("a.wav: 0 words\nWHISPER_OK\n")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(align.subprocess, "run", fake_run)
    manifest = tmp_path / "m.json"
    align.run_whisper(jobs, manifest, PY)
    assert seen["argv"] == [PY, str(align.WHISPER), str(manifest)]
    assert isinstance(seen["argv"], list) and all(isinstance(a, str) for a in seen["argv"])
    assert not seen["kw"].get("shell")
    assert seen["kw"]["timeout"] == align.WHISPER_TIMEOUT_S == 1800
    assert json.loads(manifest.read_text()) == jobs
    assert align.WHISPER.name == "whisper_words.py" and align.WHISPER.is_file()


def test_the_model_reaches_the_child_through_its_environment(tmp_path, monkeypatch):
    jobs = jobs_in(tmp_path)
    seen = []

    def fake_run(argv, **kw):
        seen.append(kw["env"])
        Path(jobs[0]["out"]).write_text("[]")
        kw["stdout"].write("WHISPER_OK\n")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(align.subprocess, "run", fake_run)
    monkeypatch.delenv(align.MODEL_ENV, raising=False)
    align.run_whisper(jobs, tmp_path / "m.json", PY, "medium.en")
    align.run_whisper(jobs, tmp_path / "m.json", PY)
    assert seen[0][align.MODEL_ENV] == "medium.en"
    assert align.MODEL_ENV not in seen[1]  # no model: the script's own default (small.en)
    assert align.MODEL_ENV not in os.environ  # the parent's environment is never changed


def test_a_failure_message_names_no_path_and_no_log_text(tmp_path, monkeypatch):
    jobs = [{"audio": "/private/folder/a.wav", "text": LINE, "out": str(tmp_path / "secret-name.json")}]
    monkeypatch.setattr(align.subprocess, "run", fake_whisper(1, "Traceback: /private/folder/secret-name boom\n"))
    with pytest.raises(RuntimeError) as e:
        align.run_whisper(jobs, tmp_path / "m.json", PY)
    assert "private" not in str(e.value) and "secret" not in str(e.value) and "boom" not in str(e.value)
    monkeypatch.setattr(align.subprocess, "run", fake_whisper(0, "WHISPER_OK\n"))
    with pytest.raises(RuntimeError) as e:
        align.run_whisper(jobs, tmp_path / "m.json", PY)
    assert "no word file" in str(e.value) and "secret" not in str(e.value) and str(tmp_path) not in str(e.value)


@pytest.mark.parametrize("rc,stdout", [(1, "WHISPER_OK\n"), (0, "a.wav: 3 words\n"), (0, "")])
def test_run_whisper_fails_without_a_clean_exit_and_the_ok_marker(tmp_path, monkeypatch, rc, stdout):
    monkeypatch.setattr(align.subprocess, "run", fake_whisper(rc, stdout))
    with pytest.raises(RuntimeError, match="whisper failed"):
        align.run_whisper(jobs_in(tmp_path), tmp_path / "m.json", PY)


def test_run_whisper_lets_a_timeout_through(tmp_path, monkeypatch):
    def fake_run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(align.subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        align.run_whisper(jobs_in(tmp_path), tmp_path / "m.json", PY)


def test_a_failed_run_leaves_no_stale_word_file_to_be_read(tmp_path, monkeypatch):
    jobs = jobs_in(tmp_path)
    Path(jobs[0]["out"]).write_text('[{"word": "stale", "start": 0, "end": 1}]')
    monkeypatch.setattr(align.subprocess, "run", fake_whisper(1, "boom\n"))
    with pytest.raises(RuntimeError):
        align.run_whisper(jobs, tmp_path / "m.json", PY)
    assert not Path(jobs[0]["out"]).exists()


def test_an_ok_marker_without_the_word_file_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(align.subprocess, "run", fake_whisper(0, "WHISPER_OK\n"))
    with pytest.raises(RuntimeError, match="no word file"):
        align.run_whisper(jobs_in(tmp_path), tmp_path / "m.json", PY)


def test_whisper_has_no_stdin_and_streams_both_streams_to_a_log_beside_the_manifest(tmp_path, monkeypatch):
    jobs = jobs_in(tmp_path)
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        kw["stdout"].write("loading model\nWHISPER_OK\n")
        Path(jobs[0]["out"]).write_text("[]")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(align.subprocess, "run", fake_run)
    align.run_whisper(jobs, tmp_path / "m.json", PY)
    assert seen["stdin"] is subprocess.DEVNULL and seen["stderr"] == subprocess.STDOUT
    assert "capture_output" not in seen and seen["stdout"].name == str(tmp_path / "whisper.log")
    assert (tmp_path / "whisper.log").read_text() == "loading model\nWHISPER_OK\n"


def test_a_timed_out_whisper_leaves_its_partial_log(tmp_path, monkeypatch):
    def fake_run(argv, **kw):
        kw["stdout"].write("model loaded, 3 of 10 files\n")
        kw["stdout"].flush()
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(align.subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        align.run_whisper(jobs_in(tmp_path), tmp_path / "m.json", PY)
    assert (tmp_path / "whisper.log").read_text() == "model loaded, 3 of 10 files\n"


# ---------- the venv can run the script's imports (no model load) ----------

@pytest.mark.skipif(not VENV_PY, reason="set ORION_CLOCK_TEST_WHISPER_PYTHON to a faster-whisper Python to run this")
def test_in_the_venv_the_script_imports_align_and_faster_whisper():
    r = subprocess.run([VENV_PY, str(align.WHISPER)], capture_output=True, text=True, timeout=120)
    assert "Traceback" not in r.stderr and "ModuleNotFoundError" not in r.stderr
    assert r.returncode != 0 and "MANIFEST_JSON" in r.stderr  # the usage text: imports passed, no manifest given


# ---------- numbers: the script carries digits, Whisper may write words or digits ----------

def T(text, step=0.3):
    """Consecutive timed words for a whitespace-separated string, `step` seconds each."""
    return [W(w, round(i * step, 3), round((i + 1) * step, 3)) for i, w in enumerate(text.split())]


NUMS = "12 PRs, 3 arcs, 75% merged, 4 repos."


@pytest.mark.parametrize("heard_text", [
    "twelve PRs three arcs seventy five percent merged four repos",
    "twelve PRs three arcs seventy-five percent merged four repos",
    "12 PRs 3 arcs 75% merged 4 repos",
    "12 PRs 3 arcs 75 percent merged 4 repos",
    "12 PRs 3 arcs seventy five percent merged four repos",
])
def test_a_numbers_line_reaches_full_coverage_however_whisper_writes_the_numbers(heard_text):
    words, cov = align.align_words(NUMS, T(heard_text), 5.0)
    assert cov == 1.0
    assert [w["word"] for w in words] == NUMS.split()  # the captions keep the script's own spelling


def test_a_spoken_number_takes_its_time_from_the_first_to_the_last_heard_word():
    words, _ = align.align_words(NUMS, T("twelve PRs three arcs seventy five percent merged four repos"), 5.0)
    assert words[4] == {"word": "75%", "start": 1.2, "end": 2.1}


def test_heard_digits_cover_a_script_that_spells_them_out():
    words, cov = align.align_words("twelve PRs and seventy five percent merged", T("12 PRs and 75% merged"), 3.0)
    assert cov == 1.0
    seventy, five, percent = words[3], words[4], words[5]
    assert seventy["start"] == 0.9 and percent["end"] == 1.2  # the heard "75%" is the fourth word
    assert seventy["end"] == five["start"] and five["end"] == percent["start"]


def test_a_heard_digit_still_matches_the_script_percent_or_dollar_amount():
    words, cov = align.align_words("75% merged", T("75 merged"), 1.0)
    assert cov == 1.0 and words[0] == {"word": "75%", "start": 0.0, "end": 0.3}
    words, cov = align.align_words("$12 saved", T("12 saved"), 1.0)
    assert cov == 1.0 and words[0] == {"word": "$12", "start": 0.0, "end": 0.3}
    assert align.align_words("75 merged", T("75 merged"), 1.0)[1] == 1.0


@pytest.mark.parametrize("script,heard_text", [
    ("It cost $12 today", "It cost twelve dollars today"),
    ("It cost $1 today", "It cost one dollar today"),
    ("It cost $1,200 today", "It cost one thousand two hundred dollars today"),
    ("12345 lines", "twelve thousand three hundred forty five lines"),
    ("the 3rd arc", "the third arc"),
    ("the 21st arc", "the twenty-first arc"),
    ("the 21st arc", "the twenty first arc"),
    ("1.5 hours", "one point five hours"),
    ("0 failures", "zero failures"),
])
def test_other_spoken_number_shapes_match(script, heard_text):
    assert align.align_words(script, T(heard_text), 5.0)[1] == 1.0


def test_a_token_that_cannot_be_normalized_does_not_count_against_the_line_when_unheard():
    words, cov = align.align_words("Shipped 3x faster today", T("Shipped three times faster today"), 3.0)
    assert cov == 1.0 and [w["word"] for w in words] == ["Shipped", "3x", "faster", "today"]
    assert align.align_words("v2 shipped", T("v2 shipped"), 1.0)[1] == 1.0  # heard as written, it still matches


def test_a_normalizable_number_that_was_not_heard_still_counts_against_the_line():
    _, cov = align.align_words("one two 75% four", T("one four"), 2.0)
    assert cov == 0.5


def test_a_line_of_only_unheard_unnormalizable_tokens_has_zero_coverage():
    assert align.align_words("3x 4x", [], 1.0)[1] == 0.0


@pytest.mark.parametrize("raw,key", [
    ("0", "zero"), ("7", "seven"), ("11", "eleven"), ("13", "thirteen"), ("19", "nineteen"), ("20", "twenty"),
    ("21", "twentyone"),
    ("100", "onehundred"), ("101", "onehundredone"), ("999", "ninehundredninetynine"),
    ("1000", "onethousand"), ("1,200", "onethousandtwohundred"),
    ("12345", "twelvethousandthreehundredfortyfive"),
    ("999999", "ninehundredninetyninethousandninehundredninetynine"),
    ("75%", "seventyfivepercent"), ("(75%),", "seventyfivepercent"), ("12.5%", "twelvepointfivepercent"),
    ("$12", "twelvedollars"), ("$1", "onedollar"), ("$1,200.", "onethousandtwohundreddollars"),
    ("1st", "first"), ("2nd", "second"), ("3rd", "third"), ("5th", "fifth"), ("8th", "eighth"), ("9th", "ninth"),
    ("21st", "twentyfirst"), ("12th", "twelfth"), ("20th", "twentieth"), ("100th", "onehundredth"),
    ("1.5", "onepointfive"), ("0.5", "zeropointfive"), ("%", "percent"),
    ("Café", "cafe"), ("seventy-five", "seventyfive"), ("Hello,", "hello"),
    # left as digits (the caller treats these as unnormalizable): too big, zero padded, glued to letters
    ("1,000,000", "1000000"), ("007", "007"), ("v2", "v2"), ("3x", "3x"), ("$1.50", "150"),
])
def test_spoken_turns_numbers_into_the_words_a_voice_says(raw, key):
    assert align.spoken(raw) == key


def test_a_percent_or_dollar_amount_also_matches_by_its_bare_number():
    assert align.spoken_forms("75%") == ("seventyfivepercent", "seventyfive")
    assert align.spoken_forms("$12") == ("twelvedollars", "twelve")
    assert align.spoken_forms("hello") == ("hello",)


# ---------- repeated words, contractions, casing and punctuation ----------

def test_a_repeated_word_maps_to_two_distinct_timed_words_in_order():
    words, cov = align.align_words("the cat and the dog", T("the cat and the dog"), 2.0)
    assert cov == 1.0
    assert [w["start"] for w in words] == [0.0, 0.3, 0.6, 0.9, 1.2]
    words, cov = align.align_words("very very good", T("very very good"), 2.0)
    assert cov == 1.0 and [w["start"] for w in words] == [0.0, 0.3, 0.6]


def test_a_repeated_word_heard_once_goes_to_the_occurrence_that_keeps_the_most_words_matched():
    words, cov = align.align_words("the cat and the dog", T("cat and the dog"), 2.0)
    assert cov == 0.8  # the first "the" was not heard; it must not steal the heard "the"
    assert words[3] == {"word": "the", "start": 0.6, "end": 0.9}
    assert words[4] == {"word": "dog", "start": 0.9, "end": 1.2}


def test_contractions_as_written_match_as_heard():
    assert align.align_words("It's what we've got.", T("it's what we've got"), 2.0)[1] == 1.0
    ap = chr(0x2019)
    words, cov = align.align_words(f"It{ap}s what we{ap}ve got.", T("it's what we've got"), 2.0)
    assert cov == 1.0 and words[0]["word"] == f"It{ap}s"


def test_casing_quotes_and_trailing_punctuation_do_not_matter():
    script = 'He said "Glossary," then (quietly) left.'
    words, cov = align.align_words(script, T("he said glossary then quietly left"), 3.0)
    assert cov == 1.0 and [w["word"] for w in words] == script.split()
    lq, rq = chr(0x201c), chr(0x201d)
    assert align.align_words(f"{lq}Glossary{rq}, ok", T("GLOSSARY ok"), 1.0)[1] == 1.0


# ---------- the monotonic pass: order, minimum width, minimum step, and the audio's end ----------

def test_overlapping_heard_words_start_where_the_previous_one_ended():
    words, _ = align.align_words("one two", [W("one", 0.0, 0.5), W("two", 0.3, 0.6)], 1.0)
    assert words[1] == {"word": "two", "start": 0.5, "end": 0.6}


def test_a_zero_width_heard_word_keeps_a_minimum_width():
    words, _ = align.align_words("one two", [W("one", 0.2, 0.2), W("two", 0.5, 0.7)], 1.0)
    assert words[0] == {"word": "one", "start": 0.2, "end": 0.21}


def test_unheard_words_squeezed_between_touching_neighbours_get_a_minimum_step():
    words, _ = align.align_words("one two three", [W("one", 0.0, 0.5), W("three", 0.5, 1.0)], 1.0)
    assert words[1] == {"word": "two", "start": 0.5, "end": 0.55}
    assert words[2]["start"] == 0.55


def test_words_heard_past_the_audio_end_keep_a_width_and_stay_inside_the_audio():
    words, _ = align.align_words("one two three", [W("one", 0.0, 0.4), W("two", 1.2, 1.4), W("three", 1.4, 1.6)], 1.0)
    assert words[0] == {"word": "one", "start": 0.0, "end": 0.4}
    assert words[1] == {"word": "two", "start": 0.98, "end": 0.99}
    assert words[2] == {"word": "three", "start": 0.99, "end": 1.0}


def test_words_pushed_back_from_the_end_do_not_overlap_the_ones_before_them():
    heard = [W("one", 0.0, 0.5), W("two", 0.9, 1.0), W("three", 1.0, 1.1)]
    words, _ = align.align_words("one two three four five", heard, 1.0)
    assert all(a["end"] <= b["start"] for a, b in zip(words, words[1:]))
    assert all(w["end"] - w["start"] >= 0.0099 for w in words)
    assert words[-1]["end"] == 1.0 and words[0]["start"] >= 0.0


def test_audio_shorter_than_the_minimum_widths_is_shared_out_evenly():
    words, _ = align.align_words("one two three four", [], 0.02)
    assert words[0]["start"] == 0.0 and words[-1]["end"] == 0.02
    assert all(w["end"] > w["start"] for w in words)
    assert all(a["end"] <= b["start"] for a, b in zip(words, words[1:]))


def test_an_unknown_duration_clamps_nothing():
    words, _ = align.align_words("one two", [W("one", 0.0, 0.2), W("two", 0.3, 0.5)], 0.0)
    assert words == [{"word": "one", "start": 0.0, "end": 0.2}, {"word": "two", "start": 0.3, "end": 0.5}]


# ---------- a long join, the half-line floor under the unnormalizable exemption, and years ----------

def test_the_longest_figure_joins_ten_heard_words():
    heard_text = "It saved nine hundred ninety nine thousand nine hundred ninety nine dollars today"
    words, cov = align.align_words("It saved $999,999 today", T(heard_text), 5.0)
    assert cov == 1.0
    assert words[2] == {"word": "$999,999", "start": 0.6, "end": 3.6}  # heard words 2 to 11, ten of them


def test_unheard_unnormalizable_tokens_cannot_hide_a_mostly_unheard_line():
    _, cov = align.align_words("v2 3x 4k 8gb 16gb 2x now ok", T("now ok"), 5.0)
    assert cov == 0.5 and cov < align.MIN_COVERAGE  # 2 heard; the denominator keeps at least half of the 8 words


def test_the_half_line_floor_rounds_up():
    _, cov = align.align_words("v2 3x 4k now ok", T("now ok"), 5.0)
    assert cov == pytest.approx(2 / 3, abs=0.001)  # 5 words: the denominator keeps ceil(5 / 2) = 3, not 2


def test_one_unheard_version_tag_among_ordinary_words_still_scores_high():
    _, cov = align.align_words("v2 shipped now and it works", T("shipped now and it works"), 3.0)
    assert cov == 1.0


@pytest.mark.parametrize("raw,forms", [
    ("2026", ("twothousandtwentysix", "twentytwentysix")),
    ("1999", ("onethousandninehundredninetynine", "nineteenninetynine")),
    ("2010", ("twothousandten", "twentyten")),
    ("2099", ("twothousandninetynine", "twentyninetynine")),
    ("1905", ("onethousandninehundredfive", "nineteenohfive")),
    ("1900", ("onethousandninehundred", "nineteenhundred")),
    ("1100", ("onethousandonehundred", "elevenhundred")),
    ("(2026),", ("twothousandtwentysix", "twentytwentysix")),
    # no year reading: 2000 to 2009 are "two thousand ..." and the rest is outside 1100 to 2099
    ("2000", ("twothousand",)), ("2005", ("twothousandfive",)), ("2009", ("twothousandnine",)),
    ("1099", ("onethousandninetynine",)), ("2100", ("twothousandonehundred",)),
    ("1,999", ("onethousandninehundredninetynine",)), ("2026%", ("twothousandtwentysixpercent",
                                                                 "twothousandtwentysix")),
])
def test_a_four_digit_year_also_accepts_the_year_reading(raw, forms):
    assert align.spoken_forms(raw) == forms


@pytest.mark.parametrize("script,heard_text,span", [
    ("In 2026 we shipped", "In twenty twenty six we shipped", (0.3, 1.2)),
    ("In 2026 we shipped", "In two thousand twenty six we shipped", (0.3, 1.5)),
    ("In 2026 we shipped", "In 2026 we shipped", (0.3, 0.6)),
    ("Since 1999 it ran", "Since nineteen ninety nine it ran", (0.3, 1.2)),
    ("Since 1999 it ran", "Since nineteen ninety-nine it ran", (0.3, 0.9)),
    ("Since 1999 it ran", "Since one thousand nine hundred ninety nine it ran", (0.3, 2.1)),
    ("In 2005 we began", "In two thousand five we began", (0.3, 1.2)),
    ("In 1905 we began", "In nineteen oh five we began", (0.3, 1.2)),
])
def test_a_year_matches_both_readings_and_takes_the_span_of_the_words_heard(script, heard_text, span):
    words, cov = align.align_words(script, T(heard_text), 5.0)
    assert cov == 1.0
    assert (words[1]["start"], words[1]["end"]) == span
