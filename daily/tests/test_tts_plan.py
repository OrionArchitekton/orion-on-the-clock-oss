"""The voicing plan is the agent-voice guard: an agent line is never spoken with the host's (the cloned narrator's)
conditioning. The fake model below behaves like Chatterbox where it matters: it holds ONE set of
conditionals (model.conds) and generate() mutates them, so a plan that fails to set a fresh copy before every chunk
speaks a later chunk or line with the wrong voice."""
import copy
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

import pytest

from daily import tts_plan
from media_helpers import AGENTS, voices as fixture_voices

Call = namedtuple("Call", "text tag seed params")
REPO = Path(__file__).resolve().parents[2]  # the folder that holds the daily package


class Conds:
    def __init__(self, tag):
        self.tag = tag


class Ops:
    """List-backed stand-ins for the torch helpers. Instance state, so no test inherits another's calls."""

    def __init__(self):
        self.copy = copy.deepcopy
        self.seeds = []
        self.saved = {}
        self.compared = []

    def same_voice(self, a, b):
        """A content comparison (the runner compares the speaker embedding with torch.equal), so a deep copy of the
        host's conds is the same voice as the host's conds."""
        self.compared.append((a, b))
        return a.tag == b.tag

    def seed(self, n):
        self.seeds.append(n)

    @staticmethod
    def silence(seconds, sr):
        return [0.0] * int(seconds * sr)

    @staticmethod
    def concat(pieces):
        return [x for p in pieces for x in p]

    def save(self, path, wav, sr):
        self.saved[Path(path).name] = len(wav)

    @staticmethod
    def seconds(wav, sr):
        return len(wav) / sr


class FakeModel:
    sr = 10

    def __init__(self, ops):
        self.ops = ops
        self.conds = None
        self.calls = []

    def generate(self, text, **params):
        self.calls.append(Call(text, self.conds.tag, self.ops.seeds[-1] if self.ops.seeds else None, params))
        self.conds.tag = "mutated-by-generate"  # the real model mutates its conds too
        return [0.0] * 10


P = {"exaggeration": 0.5, "cfg_weight": 0.5, "temperature": 0.8}


def line(i, voice, sentences, params=P):
    """A line made of known sentences, so a test can count chunks without asking the module under test."""
    return {"id": i, "voice": voice, "text": " ".join(sentences), "params": params}


def leaks(calls, expected_tags):
    """Indexes of the chunks that were spoken with conditioning other than their own line's."""
    assert len(calls) == len(expected_tags), (len(calls), len(expected_tags))
    return [i for i, (c, tag) in enumerate(zip(calls, expected_tags)) if c.tag != tag]


def expected_for(lines, tag_of):
    return [tag_of(ln["voice"]) for ln in lines for _ in sentences_of(ln)]


def sentences_of(ln):
    # every test sentence ends with a full stop and is joined by one space, so this is independent of tts_plan.chunks
    return [s for s in ln["text"].split(". ") if s]


def run(lines, conds, **kw):
    ops = Ops()
    model = FakeModel(ops)
    results = tts_plan.synthesize(model, lines, conds, ops, Path("out"), **kw)
    return model, ops, results


def tag_shared(voice):
    return "host" if voice == "host" else "builtin"


def alternating_lines():
    """host, agent, host, agent ... across every fixture agent; every line has two sentences so a missing per-chunk
    reset shows up inside a line, not only at a voice change."""
    lines = [line("open", "host", ["Hi there.", "Second one."])]
    for h in AGENTS:
        lines += [line(f"{h}_line", h, ["I did a thing.", "Then another."]),
                  line(f"after_{h}", "host", ["Back to me.", "Moving on."])]
    return lines


# --- chunks -------------------------------------------------------------------------------------------------------

def test_chunks_split_on_sentence_ends():
    assert tts_plan.chunks("One two. Three? Four!  Five") == ["One two.", "Three?", "Four!", "Five"]


def test_chunks_of_blank_text_is_empty():
    assert tts_plan.chunks("") == [] and tts_plan.chunks("  \n ") == []


def test_module_is_torch_free():
    code = f"import sys; sys.path.insert(0, {str(REPO)!r}); import daily.tts_plan; sys.exit('torch' in sys.modules)"
    assert subprocess.run([sys.executable, "-I", "-c", code]).returncode == 0


# --- the guard ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("shared", [True, False], ids=["one-builtin-snapshot", "conds-per-voice"])
def test_no_harness_line_ever_gets_host_conds(shared):
    """host, agent, host, agent ... for every fixture agent, as the runner shares one built-in snapshot."""
    lines = alternating_lines()
    builtin = Conds("builtin")
    conds = {"host": Conds("host"), **{h: (builtin if shared else Conds(h)) for h in AGENTS}}
    tag_of = tag_shared if shared else (lambda v: v)
    model, _, results = run(lines, conds)
    assert leaks(model.calls, expected_for(lines, tag_of)) == []
    assert [r["chunks"] for r in results] == [2] * len(lines)
    host_calls = [c for c in model.calls if c.tag == "host"]
    assert len(host_calls) == 2 * (1 + len(AGENTS))  # every host line got host conds
    assert len(model.calls) - len(host_calls) == 2 * len(AGENTS)  # and every harness line did not
    assert conds["host"].tag == "host" and builtin.tag == "builtin"  # the stored snapshots are never mutated


def naive_set_once(model, lines, conds, ops, out_dir):
    """Bug 1: conditionals set once, from the first line's voice, and never swapped (the host's clone sticks)."""
    model.conds = ops.copy(conds[lines[0]["voice"]])
    for ln in lines:
        for text in tts_plan.chunks(ln["text"]):
            ops.seed(0)
            model.generate(text, **ln["params"])


def naive_no_copy(model, lines, conds, ops, out_dir):
    """Bug 2: swapped on a voice change but handed the shared object, so generate() corrupts the snapshot."""
    current = None
    for ln in lines:
        if ln["voice"] != current:
            model.conds, current = conds[ln["voice"]], ln["voice"]
        for text in tts_plan.chunks(ln["text"]):
            ops.seed(0)
            model.generate(text, **ln["params"])


def naive_per_line(model, lines, conds, ops, out_dir):
    """Bug 3: a fresh copy per line but not per chunk, so the second sentence runs on conds the first one mutated."""
    for ln in lines:
        model.conds = ops.copy(conds[ln["voice"]])
        for text in tts_plan.chunks(ln["text"]):
            ops.seed(0)
            model.generate(text, **ln["params"])


@pytest.mark.parametrize("naive", [naive_set_once, naive_no_copy, naive_per_line])
def test_positive_control_the_checker_catches_each_leaking_plan(naive):
    """If these plans passed the guard test above, the guard test would prove nothing."""
    lines = alternating_lines()
    conds = {"host": Conds("host"), **{h: Conds("builtin") for h in AGENTS}}
    ops = Ops()
    model = FakeModel(ops)
    naive(model, lines, conds, ops, Path("out"))
    assert leaks(model.calls, expected_for(lines, tag_shared)) != []


def test_a_harness_line_after_a_harness_line_is_not_poisoned_by_the_first():
    lines = [line("a", "codex", ["One.", "Two."]), line("b", "my_agent", ["Three.", "Four."])]
    builtin = Conds("builtin")
    model, _, _ = run(lines, {"host": Conds("host"), "codex": builtin, "my_agent": builtin})
    assert [c.tag for c in model.calls] == ["builtin"] * 4 and builtin.tag == "builtin"


def test_unknown_voice_fails_closed_before_anything_is_voiced():
    lines = [line("ok", "host", ["Fine."]), line("x", "my_agent", ["Hi."])]
    ops = Ops()
    model = FakeModel(ops)
    with pytest.raises(KeyError):
        tts_plan.synthesize(model, lines, {"host": Conds("host")}, ops, Path("out"))
    assert model.calls == [] and ops.saved == {}  # no GPU time and no wav spent on a plan that cannot finish


class Without:
    """An ops object that lacks one attribute."""

    def __init__(self, ops, name):
        self._ops, self._name = ops, name

    def __getattr__(self, name):
        if name == self._name:
            raise AttributeError(name)
        return getattr(self._ops, name)


def test_a_harness_pointed_at_the_host_conditionals_object_is_refused():
    """The identity check stands on its own: same_voice here always says 'different', so only identity can refuse."""
    host = Conds("host")
    ops = Ops()
    ops.same_voice = lambda a, b: False
    model = FakeModel(ops)
    with pytest.raises(ValueError, match="host's conditionals"):
        tts_plan.synthesize(model, [line("h", "codex", ["Hi."])], {"host": host, "codex": host}, ops, Path("out"))
    assert model.calls == []


def test_a_deep_copy_of_the_host_conditionals_on_a_harness_is_refused_by_content():
    host = Conds("host")
    clone = copy.deepcopy(host)
    assert clone is not host  # the identity check cannot see this one
    ops = Ops()
    model = FakeModel(ops)
    with pytest.raises(ValueError, match="same voice"):
        tts_plan.synthesize(model, [line("a", "host", ["Fine."]), line("h", "codex", ["Hi."])],
                            {"host": host, "codex": clone}, ops, Path("out"))
    assert model.calls == [] and ops.saved == {}


def test_positive_control_without_the_content_check_the_deep_copy_speaks_in_the_host_voice():
    """With a same_voice that never objects, the deep copy gets through and the harness line is spoken as the host,
    so the refusal above is the only thing standing between the plan and the host's voice on an agent."""
    host = Conds("host")
    ops = Ops()
    ops.same_voice = lambda a, b: False
    model = FakeModel(ops)
    tts_plan.synthesize(model, [line("h", "codex", ["Hi."])], {"host": host, "codex": copy.deepcopy(host)}, ops,
                        Path("out"))
    assert [c.tag for c in model.calls] == ["host"]


def test_each_harness_line_is_compared_with_the_host_conditionals_and_distinct_conds_pass():
    host = Conds("host")
    codex, mine = Conds("codex-voice"), Conds("my-agent-voice")
    lines = [line("a", "codex", ["One."]), line("h", "host", ["Two."]), line("b", "my_agent", ["Three."])]
    model, ops, _ = run(lines, {"host": host, "codex": codex, "my_agent": mine})
    assert [c.tag for c in model.calls] == ["codex-voice", "host", "my-agent-voice"]
    assert [(a.tag, b.tag) for a, b in ops.compared] == [("codex-voice", "host"), ("my-agent-voice", "host")]


@pytest.mark.parametrize("conds", [{"codex": Conds("builtin")}, {"codex": Conds("builtin"), "host": None}, {}],
                         ids=["host-key-missing", "host-conds-None", "no-voices-at-all"])
@pytest.mark.parametrize("lines", [[line("h", "codex", ["Hi."])], []], ids=["harness-only", "no-lines"])
def test_a_plan_without_host_conditionals_is_refused_so_the_content_comparison_always_runs(conds, lines):
    ops = Ops()
    model = FakeModel(ops)
    with pytest.raises(ValueError, match="host"):
        tts_plan.synthesize(model, lines, conds, ops, Path("out"))
    assert model.calls == [] and ops.saved == {}


def test_a_deep_copy_of_the_host_conditionals_under_a_harness_is_refused_when_the_host_key_is_missing():
    """The leak this closes: with no host key there would be nothing to compare against, so a copy of the host's
    conds on an agent could be voiced."""
    ops = Ops()
    model = FakeModel(ops)
    with pytest.raises(ValueError, match="host"):
        tts_plan.synthesize(model, [line("h", "codex", ["Hi."])], {"codex": copy.deepcopy(Conds("host"))}, ops,
                            Path("out"))
    assert model.calls == []


def test_a_normal_plan_with_host_conditionals_still_passes():
    model, ops, results = run([line("h", "codex", ["Hi."]), line("a", "host", ["Fine."])],
                              {"host": Conds("host"), "codex": Conds("builtin")})
    assert [c.tag for c in model.calls] == ["builtin", "host"] and len(results) == 2 and len(ops.compared) == 1


@pytest.mark.parametrize("missing", ["absent", "not-callable"])
def test_ops_without_a_usable_same_voice_is_refused(missing):
    ops = Without(Ops(), "same_voice")
    if missing == "not-callable":
        ops = Ops()
        ops.same_voice = None  # present but not callable
    model = FakeModel(ops)
    with pytest.raises(ValueError, match="same_voice"):
        tts_plan.synthesize(model, [line("a", "host", ["Fine."]), line("h", "codex", ["Hi."])],
                            {"host": Conds("host"), "codex": Conds("builtin")}, ops, Path("out"))
    assert model.calls == []


def test_ops_without_same_voice_is_refused_even_when_no_harness_speaks():
    ops = Without(Ops(), "same_voice")
    model = FakeModel(ops)
    with pytest.raises(ValueError, match="same_voice"):
        tts_plan.synthesize(model, [line("a", "host", ["Fine."])], {"host": Conds("host")}, ops, Path("out"))
    assert model.calls == []


@pytest.mark.parametrize("answer", [None, 1, "no"])
def test_a_same_voice_that_does_not_answer_with_a_bool_is_refused(answer):
    ops = Ops()
    ops.same_voice = lambda a, b: answer
    model = FakeModel(ops)
    with pytest.raises(ValueError, match="same_voice"):
        tts_plan.synthesize(model, [line("h", "codex", ["Hi."])], {"host": Conds("host"), "codex": Conds("builtin")},
                            ops, Path("out"))
    assert model.calls == []


def test_a_harness_with_no_conditionals_is_said_plainly():
    ops = Ops()
    model = FakeModel(ops)
    with pytest.raises(ValueError) as e:
        tts_plan.synthesize(model, [line("h", "codex", ["Hi."])], {"host": Conds("host"), "codex": None}, ops,
                            Path("out"))
    assert "None" in str(e.value) and "host" not in str(e.value)  # not the misleading 'set to the host's conditionals'
    assert model.calls == []


def test_a_host_with_no_conditionals_is_refused():
    ops = Ops()
    model = FakeModel(ops)
    with pytest.raises(ValueError, match="None"):
        tts_plan.synthesize(model, [line("a", "host", ["Fine."])], {"host": None}, ops, Path("out"))
    assert model.calls == []


def test_duplicate_line_ids_are_refused_before_anything_is_voiced():
    ops = Ops()
    model = FakeModel(ops)
    with pytest.raises(ValueError, match="duplicate.*story_0"):
        tts_plan.synthesize(model, [line("story_0", "host", ["One."]), line("story_0", "host", ["Two."])],
                            {"host": Conds("host")}, ops, Path("out"))
    assert model.calls == [] and ops.saved == {}


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", "Story", "a.b", "a b", "x\n", "", "..", None, 7])
def test_a_line_id_that_could_escape_the_out_dir_is_refused(bad):
    ops = Ops()
    model = FakeModel(ops)
    with pytest.raises(ValueError, match="line id"):
        tts_plan.synthesize(model, [line(bad, "host", ["Fine."])], {"host": Conds("host")}, ops, Path("out"))
    assert model.calls == [] and ops.saved == {}


def test_plain_line_ids_are_accepted():
    ids = ["cold_open", "story_0", "steal_this", "confessional_intro", "a1_b2"]
    _, ops, results = run([line(i, "host", ["Fine."]) for i in ids], {"host": Conds("host")})
    assert [r["id"] for r in results] == ids and set(ops.saved) == {f"{i}.wav" for i in ids}


def test_a_reference_recording_in_line_params_is_refused():
    """generate(audio_prompt_path=...) is exactly what replaces model.conds, so it can never ride in on a line."""
    bad = line("h", "codex", ["Hi."], params={**P, "audio_prompt_path": "/refs/host.wav"})
    ops = Ops()
    model = FakeModel(ops)
    with pytest.raises(ValueError, match="audio_prompt_path"):
        tts_plan.synthesize(model, [line("a", "host", ["Fine."]), bad],
                            {"host": Conds("host"), "codex": Conds("builtin")}, ops, Path("out"))
    assert model.calls == []


def test_empty_text_fails_closed_before_anything_is_voiced():
    lines = [line("ok", "host", ["Fine."]), {"id": "blank", "voice": "host", "text": "  ", "params": P}]
    ops = Ops()
    model = FakeModel(ops)
    with pytest.raises(ValueError, match="blank"):
        tts_plan.synthesize(model, lines, {"host": Conds("host")}, ops, Path("out"))
    assert model.calls == []


def test_no_lines_is_no_results():
    model, ops, results = run([], {"host": Conds("host")})
    assert results == [] and model.calls == [] and ops.saved == {}


# --- a real day's shape: steal_this, with and without a confessional ----------------------------------------------

def episode_lines(voices, confessional_harness):
    """The voice stage's order (host lines from the script, the quote in its harness's voice), no confessional lines
    at all when confessional_harness is None."""
    host = voices["host"]
    lines = [line("cold_open", "host", ["My agents shipped a lot.", "Here is the day."], host["params_cold_open"]),
             line("scoreboard", "host", ["Four agents reported."], host["params"]),
             line("story_0", "host", ["Claude Code closed a PR.", "It was small."], host["params"]),
             line("steal_this", "host", ["Steal this.", "Write the test first."], host["params"])]
    if confessional_harness:
        lines += [line("confessional_intro", "host", ["Here is what it said."], host["params"]),
                  line("confessional", confessional_harness, ["I fixed the thing.", "It took two tries."],
                       voices[confessional_harness]["params"])]
    lines.append(line("out", "host", ["See you tomorrow."], host["params"]))
    return lines


def by_line(model, results):
    """{line id: [Call, ...]} from the chunk counts the plan reported."""
    out, i = {}, 0
    for r in results:
        out[r["id"]] = model.calls[i:i + r["chunks"]]
        i += r["chunks"]
    assert i == len(model.calls)
    return out


@pytest.mark.parametrize("harness", AGENTS)
def test_episode_with_a_confessional_voices_only_the_quote_in_the_harness_voice(harness):
    voices = fixture_voices()
    lines = episode_lines(voices, harness)
    model, ops, results = run(lines, {"host": Conds("host"), **{h: Conds("builtin") for h in AGENTS}})
    calls = by_line(model, results)
    assert [r["id"] for r in results] == ["cold_open", "scoreboard", "story_0", "steal_this", "confessional_intro",
                                          "confessional", "out"]
    assert {c.tag for c in calls["confessional"]} == {"builtin"} and len(calls["confessional"]) == 2
    host_ids = [i for i in calls if i != "confessional"]
    assert all(c.tag == "host" for i in host_ids for c in calls[i])  # the host lines around it stay the host's voice
    assert {c.tag for c in calls["steal_this"]} == {"host"} and len(calls["steal_this"]) == 2
    assert set(ops.saved) == {f"{r['id']}.wav" for r in results}


def test_episode_without_a_confessional_has_no_confessional_audio_and_no_harness_conds():
    voices = fixture_voices()
    lines = episode_lines(voices, None)
    conds = {"host": Conds("host"), **{h: Conds("builtin") for h in AGENTS}}
    model, ops, results = run(lines, conds)
    assert [r["id"] for r in results] == ["cold_open", "scoreboard", "story_0", "steal_this", "out"]
    assert "confessional.wav" not in ops.saved and "confessional_intro.wav" not in ops.saved
    assert {c.tag for c in model.calls} == {"host"}  # no harness line, so the built-in voice is never used
    assert {c.tag for c in by_line(model, results)["steal_this"]} == {"host"}


def test_each_line_is_voiced_with_its_own_params():
    voices = fixture_voices()
    lines = episode_lines(voices, "night_bot")
    conds = {"host": Conds("host"), **{h: Conds("builtin") for h in AGENTS}}
    model, _, results = run(lines, conds)
    calls = by_line(model, results)
    host, harness = voices["host"], voices["night_bot"]
    assert all(c.params == host["params_cold_open"] for c in calls["cold_open"])
    assert all(c.params == host["params"] for c in calls["steal_this"] + calls["out"])
    assert all(c.params == harness["params"] for c in calls["confessional"])
    assert host["params_cold_open"] != host["params"] != harness["params"]  # so the checks above can tell them apart


# --- results, seeds, gaps -----------------------------------------------------------------------------------------

def test_results_seeds_and_gaps(tmp_path):
    ops = Ops()
    model = FakeModel(ops)
    r = tts_plan.synthesize(model, [line("a", "host", ["One.", "Two."])], {"host": Conds("host")}, ops, tmp_path,
                            seed=100, gap_s=0.5)
    assert ops.seeds == [100, 101]
    assert [c.seed for c in model.calls] == [100, 101]  # the seed is set before each generation, not after
    assert ops.saved == {"a.wav": 25}  # two 10-sample chunks and one 5-sample gap, no trailing gap
    assert r == [{"id": "a", "voice": "host", "text": "One. Two.", "chunks": 2, "dur": 2.5}]


def test_a_single_sentence_line_has_no_gap():
    _, ops, results = run([line("a", "host", ["Only one."])], {"host": Conds("host")})
    assert ops.saved == {"a.wav": 10} and results[0]["dur"] == 1.0


def test_seeds_restart_per_line_and_are_deterministic_across_runs():
    lines = alternating_lines()
    conds = lambda: {"host": Conds("host"), **{h: Conds("builtin") for h in AGENTS}}  # noqa: E731
    m1, o1, r1 = run(lines, conds())
    m2, o2, r2 = run(lines, conds())
    assert o1.seeds == o2.seeds and m1.calls == m2.calls and r1 == r2
    assert o1.seeds == [tts_plan.SEED, tts_plan.SEED + 1] * len(lines)  # per chunk index, restarting for each line
    assert tts_plan.SEED == 4207
    m3, o3, _ = run(lines, conds(), seed=1)
    assert o3.seeds == [1, 2] * len(lines)


def test_the_plan_does_not_change_its_inputs():
    lines = alternating_lines()
    snapshot = copy.deepcopy(lines)
    run(lines, {"host": Conds("host"), **{h: Conds("builtin") for h in AGENTS}})
    assert lines == snapshot


# ---------- check_lines: the structural line checks, runnable before any model is loaded ----------

def _line(**kw):
    return {"id": "a", "voice": "host", "text": "Hi.", "params": {}, **kw}


def test_check_lines_passes_good_lines_with_no_conditionals_and_no_ops():
    tts_plan.check_lines([_line(), _line(id="b", voice="codex")], {"host", "codex"})
    tts_plan.check_lines([], set())


@pytest.mark.parametrize("lines, exc, match", [
    ([_line(id="Bad")], ValueError, "lowercase letters"),
    ([_line(id="../x")], ValueError, "lowercase letters"),
    ([_line(id="")], ValueError, "lowercase letters"),
    ([_line(id=7)], ValueError, "lowercase letters"),
    ([_line(), _line()], ValueError, "duplicate line id"),
    ([_line(text="   ")], ValueError, "empty text"),
    ([_line(params={"audio_prompt_path": "/x.wav"})], ValueError, "audio_prompt_path"),
    ([_line(voice="stranger")], KeyError, "no conditionals for voice 'stranger'"),
])
def test_check_lines_refuses_each_structural_fault(lines, exc, match):
    with pytest.raises(exc, match=match):
        tts_plan.check_lines(lines, {"host", "codex"})


def test_progress_is_called_once_per_line_as_each_is_saved(tmp_path):
    ops = Ops()
    model = FakeModel(ops)
    seen = []
    results = tts_plan.synthesize(
        model, [line("a", "host", ["One."]), line("b", "host", ["Two."])], {"host": Conds("host")}, ops, tmp_path,
        clock=iter([0.0, 1.5, 2.0, 5.0]).__next__, progress=lambda row: seen.append((row["id"], row["seconds"], len(ops.saved))))
    assert seen == [("a", 1.5, 1), ("b", 3.0, 2)] and [r["id"] for r in results] == ["a", "b"]
