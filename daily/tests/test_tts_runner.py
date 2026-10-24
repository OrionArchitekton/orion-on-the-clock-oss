"""The Chatterbox runner's contract, tested in plain Python with no torch: importing it loads nothing heavy, the CLI
checks its inputs before it takes the shared TTS lock, the lock is held for the whole voicing and always released, and
same_voice compares the identity tensors by content. Real-model behavior is a manual check; the two venv tests below
build real Conditionals out of random tensors (no model, no GPU) and run only when $ORION_CLOCK_TEST_TTS_PYTHON names
the Python of a Chatterbox environment."""
import copy
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from daily import tts_plan, tts_runner, voiceconf
from media_helpers import VOICES

REPO = Path(__file__).resolve().parents[2]  # the folder that holds the daily package
VENV_PY = os.environ.get("ORION_CLOCK_TEST_TTS_PYTHON")
needs_venv = pytest.mark.skipif(not VENV_PY, reason="set ORION_CLOCK_TEST_TTS_PYTHON to a Chatterbox Python to run this")
REF_BYTES = b"a reference recording"


# ---------- fixtures ----------

@pytest.fixture
def voices_path(tmp_path):
    """The synthetic voices.json with the host's reference pointed at a tiny file whose hash prefix matches."""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(REF_BYTES)
    data = copy.deepcopy(VOICES)
    data["voices"]["host"]["ref"] = str(ref)
    data["voices"]["host"]["sha256_prefix"] = hashlib.sha256(REF_BYTES).hexdigest()[:16]
    p = tmp_path / "voices.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


@pytest.fixture
def lines_path(tmp_path):
    p = tmp_path / "lines.json"
    p.write_text(json.dumps([
        {"id": "cold_open", "voice": "host", "text": "One. Two.", "params": {"exaggeration": 0.7}},
        {"id": "confessional", "voice": "codex", "text": "Three.", "params": {"exaggeration": 0.4}},
    ]), encoding="utf-8")
    return p


@pytest.fixture
def lock(tmp_path):
    return tmp_path / "locks" / "tts.lock"


def argv(lines_path, out, voices_path):
    return ["tts_runner.py", str(lines_path), str(out), str(voices_path)]


def run(lines_path, out, voices_path, lock, **kw):
    kw.setdefault("lock_wait_s", 0.3)
    kw.setdefault("poll_s", 0.01)
    return tts_runner.main(argv(lines_path, out, voices_path), lock_path=lock, **kw)


def lock_is_free(path) -> bool:
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


def never_voice(*a, **k):
    raise AssertionError("voicing started")


# ---------- torch-free import and the CLI contract ----------

def test_importing_the_runner_loads_no_torch():
    code = (f"import sys; sys.path.insert(0, {str(REPO)!r}); import daily.tts_runner; "
            "sys.exit(any(m in sys.modules for m in ('torch', 'torchaudio', 'chatterbox', 'numpy')))")
    assert subprocess.run([sys.executable, "-I", "-c", code]).returncode == 0


def test_script_mode_finds_its_siblings_and_prints_usage():
    r = subprocess.run([sys.executable, "-I", str(REPO / "daily" / "tts_runner.py")], capture_output=True, text=True)
    assert r.returncode != 0
    assert "LINES_JSON OUT_DIR VOICES_JSON" in r.stderr
    assert "Traceback" not in r.stderr and "RUNNER_OK" not in r.stdout


@pytest.mark.parametrize("n", [1, 2, 3, 5])
def test_wrong_argument_count_is_usage(n, lock):
    with pytest.raises(SystemExit) as e:
        tts_runner.main(["tts_runner.py"] + ["x"] * (n - 1), lock_path=lock)
    assert "LINES_JSON OUT_DIR VOICES_JSON" in str(e.value)
    assert not lock.exists()


def test_the_lock_file_is_the_environment_variable_read_at_call_time(monkeypatch, tmp_path):
    monkeypatch.delenv(tts_runner.LOCK_ENV, raising=False)
    assert tts_runner.LOCK_ENV == "ORION_CLOCK_TTS_LOCK"
    assert tts_runner.default_lock_path() == Path.home() / ".local/state/orion-on-the-clock/tts.lock"
    monkeypatch.setenv(tts_runner.LOCK_ENV, str(tmp_path / "shared" / "tts.lock"))
    assert tts_runner.default_lock_path() == tmp_path / "shared" / "tts.lock"


def test_main_without_a_lock_path_takes_the_environment_lock(tmp_path, lines_path, voices_path, monkeypatch):
    shared = tmp_path / "shared" / "tts.lock"
    monkeypatch.setenv(tts_runner.LOCK_ENV, str(shared))
    held = []

    def fake_voice(lines, out_dir, voices):
        held.append(not lock_is_free(shared))
        return "cpu", []

    monkeypatch.setattr(tts_runner, "voice_lines", fake_voice)
    tts_runner.main(argv(lines_path, tmp_path / "voice", voices_path), lock_wait_s=0.3, poll_s=0.01)
    assert held == [True] and lock_is_free(shared)


def test_success_writes_results_and_ends_with_runner_ok(tmp_path, lines_path, voices_path, lock, monkeypatch, capsys):
    out = tmp_path / "voice"
    seen = {}

    def fake_voice(lines, out_dir, voices):
        seen.update(lines=lines, out=out_dir, host_ref=voices["host"]["ref"])
        return "cpu", [{"id": ln["id"], "voice": ln["voice"], "dur": 1.0} for ln in lines]

    monkeypatch.setattr(tts_runner, "voice_lines", fake_voice)
    run(lines_path, out, voices_path, lock)
    res = json.loads((out / "tts-results.json").read_text(encoding="utf-8"))
    assert res["device"] == "cpu"
    assert res["ref_sha256"] == hashlib.sha256(REF_BYTES).hexdigest()[:16]
    assert [r["id"] for r in res["lines"]] == ["cold_open", "confessional"]
    assert [ln["id"] for ln in seen["lines"]] == ["cold_open", "confessional"] and seen["out"] == out
    assert capsys.readouterr().out.strip().splitlines()[-1] == "RUNNER_OK"


def test_lock_is_held_for_the_whole_voicing_and_released_after(tmp_path, lines_path, voices_path, lock, monkeypatch):
    held = []

    def fake_voice(lines, out_dir, voices):
        held.append(not lock_is_free(lock))  # another process could not take it meanwhile
        return "cpu", []

    monkeypatch.setattr(tts_runner, "voice_lines", fake_voice)
    run(lines_path, tmp_path / "voice", voices_path, lock)
    assert held == [True]
    assert lock_is_free(lock)


def test_positive_control_a_free_lock_file_reads_as_free(lock):
    lock.parent.mkdir(parents=True)
    assert lock_is_free(lock)


def test_a_failed_voicing_releases_the_lock_leaves_no_stale_results_and_no_runner_ok(
        tmp_path, lines_path, voices_path, lock, monkeypatch, capsys):
    out = tmp_path / "voice"
    out.mkdir()
    (out / "tts-results.json").write_text('{"device": "stale"}', encoding="utf-8")

    def boom(lines, out_dir, voices):
        raise RuntimeError("cuda exploded")

    monkeypatch.setattr(tts_runner, "voice_lines", boom)
    with pytest.raises(RuntimeError, match="cuda exploded"):
        run(lines_path, out, voices_path, lock)
    assert not (out / "tts-results.json").exists()
    assert lock_is_free(lock)
    assert "RUNNER_OK" not in capsys.readouterr().out


def test_a_failed_run_leaves_none_of_the_lines_old_wavs_and_keeps_other_files(
        tmp_path, lines_path, voices_path, lock, monkeypatch):
    out = tmp_path / "voice"
    out.mkdir()
    for name in ("cold_open.wav", "confessional.wav", "unrelated.wav", "notes.txt"):
        (out / name).write_bytes(b"stale or partial")
    seen = []

    def boom(lines, out_dir, voices):
        seen.append(sorted(p.name for p in out_dir.iterdir()))  # what the voicing starts with
        raise RuntimeError("cuda exploded")

    monkeypatch.setattr(tts_runner, "voice_lines", boom)
    with pytest.raises(RuntimeError):
        run(lines_path, out, voices_path, lock)
    assert seen == [["notes.txt", "unrelated.wav"]]
    assert sorted(p.name for p in out.iterdir()) == ["notes.txt", "unrelated.wav"]


def test_a_busy_lock_exits_without_voicing(tmp_path, lines_path, voices_path, lock, monkeypatch):
    monkeypatch.setattr(tts_runner, "voice_lines", never_voice)
    lock.parent.mkdir(parents=True)
    with open(lock, "a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        with pytest.raises(SystemExit, match="TTS lock busy"):
            run(lines_path, tmp_path / "voice", voices_path, lock)
    assert not (tmp_path / "voice" / "tts-results.json").exists()


def test_it_waits_for_the_lock_and_then_voices(tmp_path, lines_path, voices_path, lock, monkeypatch):
    monkeypatch.setattr(tts_runner, "voice_lines", lambda lines, out, voices: ("cpu", []))
    lock.parent.mkdir(parents=True)
    holder = open(lock, "a")
    fcntl.flock(holder, fcntl.LOCK_EX)
    threading.Timer(0.15, holder.close).start()  # closing drops the flock
    t0 = time.monotonic()
    run(lines_path, tmp_path / "voice", voices_path, lock, lock_wait_s=5)
    assert time.monotonic() - t0 >= 0.1
    assert (tmp_path / "voice" / "tts-results.json").exists()


@pytest.mark.parametrize("break_it", ["missing", "bad_json", "bad_schema", "bad_voice_id", "agent_with_a_ref"])
def test_voices_file_problems_exit_before_the_lock(break_it, tmp_path, lines_path, voices_path, lock, monkeypatch):
    monkeypatch.setattr(tts_runner, "voice_lines", never_voice)
    if break_it == "missing":
        voices_path.unlink()
    elif break_it == "bad_json":
        voices_path.write_text("{nope", encoding="utf-8")
    elif break_it == "bad_schema":
        voices_path.write_text(json.dumps({"schema": "other", "voices": {}}), encoding="utf-8")
    elif break_it == "bad_voice_id":
        data = json.loads(voices_path.read_text(encoding="utf-8"))
        data["voices"]["Stranger Voice"] = data["voices"]["codex"]
        voices_path.write_text(json.dumps(data), encoding="utf-8")
    else:  # an agent voice may never clone a recording
        data = json.loads(voices_path.read_text(encoding="utf-8"))
        data["voices"]["codex"]["ref"] = data["voices"]["host"]["ref"]
        voices_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SystemExit, match="voices"):
        run(lines_path, tmp_path / "voice", voices_path, lock)
    assert not lock.exists()


def test_a_host_reference_that_does_not_match_its_hash_exits_before_the_lock(
        tmp_path, lines_path, voices_path, lock, monkeypatch):
    monkeypatch.setattr(tts_runner, "voice_lines", never_voice)
    data = json.loads(voices_path.read_text(encoding="utf-8"))
    Path(data["voices"]["host"]["ref"]).write_bytes(b"someone else's recording")
    with pytest.raises(SystemExit, match="does not start with") as e:
        run(lines_path, tmp_path / "voice", voices_path, lock)
    assert str(tmp_path) not in str(e.value) and str(e.value) == voiceconf.REF_MESSAGES["mismatch"]
    assert not lock.exists()


def test_a_missing_host_reference_exits_before_the_lock(tmp_path, lines_path, voices_path, lock, monkeypatch):
    monkeypatch.setattr(tts_runner, "voice_lines", never_voice)
    data = json.loads(voices_path.read_text(encoding="utf-8"))
    Path(data["voices"]["host"]["ref"]).unlink()
    with pytest.raises(SystemExit, match="reference missing") as e:
        run(lines_path, tmp_path / "voice", voices_path, lock)
    assert str(tmp_path) not in str(e.value)  # the exit message names the key, never the path
    assert not lock.exists()


@pytest.mark.parametrize("body", [
    "{nope",
    '{"id": "a"}',
    "[]",  # nothing to voice is a caller bug, not a success with no audio
    '["a line"]',
    '[{"id": "a", "voice": "host", "text": "Hi."}]',  # no params
    '[{"id": "a", "voice": "host", "text": "Hi.", "params": []}]',
    '[{"id": 1, "voice": "host", "text": "Hi.", "params": {}}]',
])
def test_bad_lines_exit_before_the_lock(body, tmp_path, voices_path, lock, monkeypatch):
    monkeypatch.setattr(tts_runner, "voice_lines", never_voice)
    bad = tmp_path / "bad-lines.json"
    bad.write_text(body, encoding="utf-8")
    with pytest.raises(SystemExit, match="lines"):
        run(bad, tmp_path / "voice", voices_path, lock)
    assert not lock.exists()


@pytest.mark.parametrize("line, match", [
    ({"id": "a", "voice": "stranger", "text": "Hi.", "params": {}}, "no conditionals for voice 'stranger'"),
    ({"id": "Bad Id", "voice": "host", "text": "Hi.", "params": {}}, "lowercase letters"),
    ({"id": "../escape", "voice": "host", "text": "Hi.", "params": {}}, "lowercase letters"),
    ({"id": "a", "voice": "host", "text": "  ", "params": {}}, "empty text"),
    ({"id": "a", "voice": "host", "text": "Hi.", "params": {"audio_prompt_path": "/x.wav"}}, "audio_prompt_path"),
])
def test_the_plans_line_checks_run_before_the_lock_and_the_model(line, match, tmp_path, voices_path, lock, monkeypatch):
    monkeypatch.setattr(tts_runner, "voice_lines", never_voice)
    bad = tmp_path / "bad-line.json"
    bad.write_text(json.dumps([line]), encoding="utf-8")
    with pytest.raises(SystemExit, match=f"lines: .*{match}"):
        run(bad, tmp_path / "voice", voices_path, lock)
    assert not lock.exists()


def test_a_duplicate_line_id_exits_before_the_lock(tmp_path, voices_path, lock, monkeypatch):
    monkeypatch.setattr(tts_runner, "voice_lines", never_voice)
    bad = tmp_path / "dup.json"
    one = {"id": "a", "voice": "host", "text": "Hi.", "params": {}}
    bad.write_text(json.dumps([one, one]), encoding="utf-8")
    with pytest.raises(SystemExit, match="lines: duplicate line id 'a'"):
        run(bad, tmp_path / "voice", voices_path, lock)
    assert not lock.exists()


def test_a_missing_lines_file_exits_before_the_lock(tmp_path, voices_path, lock, monkeypatch):
    monkeypatch.setattr(tts_runner, "voice_lines", never_voice)
    with pytest.raises(SystemExit, match="lines"):
        run(tmp_path / "absent.json", tmp_path / "voice", voices_path, lock)
    assert not lock.exists()


# ---------- device ----------

MIB = 1024 ** 2
UUID = "00000000-1111-2222-3333-444444444444"  # a synthetic GPU uuid


def test_the_device_threshold_is_four_and_a_half_gib():
    assert tts_runner.MIN_FREE_CUDA == 4.5 * 1024 ** 3


@pytest.mark.parametrize("free, expected", [
    (None, "cpu"),
    (0, "cpu"),
    (tts_runner.MIN_FREE_CUDA - 1, "cpu"),
    (tts_runner.MIN_FREE_CUDA, "cuda"),
    (tts_runner.MIN_FREE_CUDA * 2, "cuda"),
])
def test_cuda_only_with_enough_free_memory(free, expected, monkeypatch):
    monkeypatch.setattr(tts_runner, "_cuda_free_bytes", lambda: free)
    assert tts_runner.device() == expected


def fake_torch(monkeypatch, free_mib=6999, available=True):
    """A torch stand-in with the three calls _cuda_free_bytes makes; returns the list of calls it saw."""
    calls = []
    cuda = SimpleNamespace(
        is_available=lambda: available,
        mem_get_info=lambda: (calls.append("mem_get_info"), (free_mib * MIB, 8150 * MIB))[1],
        current_device=lambda: 0,
        get_device_properties=lambda i: SimpleNamespace(uuid=UUID))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=cuda))
    return calls


def fake_smi(monkeypatch, answer):
    asked = []
    monkeypatch.setattr(tts_runner, "_smi_free_bytes", lambda gpu_uuid=None: (asked.append(gpu_uuid), answer)[1])
    return asked


def test_no_cuda_is_none_and_nvidia_smi_is_not_even_asked(monkeypatch):
    fake_torch(monkeypatch, available=False)
    asked = fake_smi(monkeypatch, 1)
    assert tts_runner._cuda_free_bytes() is None and asked == []


def test_nvidia_smi_reading_lower_than_torchs_wins_and_is_asked_about_this_gpu(monkeypatch):
    """The WSL case: torch says 6999 MiB free while nvidia-smi says 4283 MiB, and the lower figure is the true one."""
    fake_torch(monkeypatch, free_mib=6999)
    asked = fake_smi(monkeypatch, 4283 * MIB)
    assert tts_runner._cuda_free_bytes() == 4283 * MIB
    assert asked == [UUID]
    assert tts_runner.device() == "cpu"


def test_torch_reading_lower_than_nvidia_smis_wins(monkeypatch):
    fake_torch(monkeypatch, free_mib=3000)
    fake_smi(monkeypatch, 7000 * MIB)
    assert tts_runner._cuda_free_bytes() == 3000 * MIB


def test_positive_control_both_readings_high_gives_cuda(monkeypatch):
    fake_torch(monkeypatch, free_mib=6999)
    fake_smi(monkeypatch, 6500 * MIB)
    assert tts_runner._cuda_free_bytes() == 6500 * MIB and tts_runner.device() == "cuda"


def test_without_nvidia_smi_torchs_figure_is_used(monkeypatch):
    fake_torch(monkeypatch, free_mib=6999)
    fake_smi(monkeypatch, None)
    assert tts_runner._cuda_free_bytes() == 6999 * MIB and tts_runner.device() == "cuda"


def smi_run(monkeypatch, *, stdout="", returncode=0, raises=None):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, kw=kw)
        if raises:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(tts_runner.subprocess, "run", fake_run)
    return seen


def test_nvidia_smi_is_called_with_argv_a_timeout_and_this_gpus_id(monkeypatch):
    seen = smi_run(monkeypatch, stdout="4283\n")
    assert tts_runner._smi_free_bytes(UUID) == 4283 * MIB
    assert seen["argv"] == ["nvidia-smi", f"--id=GPU-{UUID}", "--query-gpu=memory.free",
                            "--format=csv,noheader,nounits"]
    assert isinstance(seen["argv"], list) and not seen["kw"].get("shell")
    assert 0 < seen["kw"]["timeout"] <= 30 and seen["kw"]["capture_output"] is True


def test_without_a_gpu_id_the_lowest_of_all_gpus_is_used(monkeypatch):
    seen = smi_run(monkeypatch, stdout="7000\n4283\n")
    assert tts_runner._smi_free_bytes() == 4283 * MIB
    assert not any(a.startswith("--id") for a in seen["argv"])


@pytest.mark.parametrize("kw", [
    {"raises": FileNotFoundError("nvidia-smi")},
    {"raises": PermissionError("nvidia-smi")},
    {"raises": subprocess.TimeoutExpired("nvidia-smi", 10)},
    {"returncode": 9, "stdout": "4283\n"},
    {"stdout": ""},
    {"stdout": "[N/A]\n"},
    {"stdout": "N/A\n"},
    {"stdout": "lots\n"},
    {"stdout": "-5\n"},
    {"stdout": "4283\nN/A\n"},
])
def test_nvidia_smi_failing_or_unreadable_gives_none(kw, monkeypatch):
    smi_run(monkeypatch, **kw)
    assert tts_runner._smi_free_bytes(UUID) is None


# ---------- same_voice: a content comparison of the identity tensors ----------

IDENTITY_NAMES = ["t3.speaker_emb", "t3.cond_prompt_speech_tokens", "gen.prompt_token", "gen.prompt_feat",
                  "gen.embedding"]


def conds(tag):
    """Stand-in for chatterbox Conditionals. Tensors are plain lists here (the test patches the equality), unique per
    tag. The lengths and the exaggeration are the SAME for every tag, as they are for real voices of equal length."""
    return SimpleNamespace(
        t3=SimpleNamespace(speaker_emb=[tag, "spk"], clap_emb=None, cond_prompt_speech_tokens=[tag, "tok"],
                           cond_prompt_speech_emb=None, emotion_adv=[0.5]),
        gen={"prompt_token": [tag, "pt"], "prompt_token_len": [157], "prompt_feat": [tag, "pf"],
             "prompt_feat_len": None, "embedding": [tag, "emb"]})


def set_tensor(c, name, value):
    part, key = name.split(".")
    if part == "t3":
        setattr(c.t3, key, value)
    else:
        c.gen[key] = value


def drop_tensor(c, name):
    part, key = name.split(".")
    if part == "t3":
        setattr(c.t3, key, None)
    else:
        c.gen.pop(key)


@pytest.fixture(autouse=False)
def list_equal(monkeypatch):
    monkeypatch.setattr(tts_runner, "_equal", lambda x, y: x == y)


def test_the_identity_tensors_are_exactly_these():
    assert [f"{p}.{n}" for p, n in tts_runner.IDENTITY] == IDENTITY_NAMES


def test_a_deep_copy_of_the_host_conds_is_the_same_voice(list_equal):
    host = conds("host")
    twin = copy.deepcopy(host)
    assert twin is not host
    assert tts_runner.same_voice(twin, host) is True


def test_different_voices_are_not_the_same_voice_even_with_equal_lengths_and_exaggeration(list_equal):
    a, b = conds("host"), conds("builtin")
    assert a.gen["prompt_token_len"] == b.gen["prompt_token_len"] and a.t3.emotion_adv == b.t3.emotion_adv
    assert tts_runner.same_voice(a, b) is False


@pytest.mark.parametrize("name", IDENTITY_NAMES)
def test_sharing_any_one_identity_tensor_is_the_same_voice(name, list_equal):
    host, harness = conds("host"), conds("builtin")
    part, key = name.split(".")
    set_tensor(harness, name, copy.deepcopy(getattr(host.t3, key) if part == "t3" else host.gen[key]))
    assert tts_runner.same_voice(harness, host) is True


@pytest.mark.parametrize("name", [n for n in IDENTITY_NAMES if n not in ("t3.speaker_emb", "gen.embedding")])
def test_an_optional_tensor_missing_on_both_sides_is_not_evidence_of_a_match(name, list_equal):
    a, b = conds("host"), conds("builtin")
    drop_tensor(a, name)
    drop_tensor(b, name)
    assert tts_runner.same_voice(a, b) is False


@pytest.mark.parametrize("name", [n for n in IDENTITY_NAMES if n not in ("t3.speaker_emb", "gen.embedding")])
def test_an_optional_tensor_missing_on_one_side_still_compares_the_rest(name, list_equal):
    a, b = conds("host"), conds("builtin")
    drop_tensor(a, name)
    assert tts_runner.same_voice(a, b) is False
    set_tensor(b, "gen.embedding", copy.deepcopy(a.gen["embedding"]))
    assert tts_runner.same_voice(a, b) is True


@pytest.mark.parametrize("name", ["t3.speaker_emb", "gen.embedding"])
@pytest.mark.parametrize("side", ["first", "second"])
def test_a_speaker_embedding_missing_cannot_prove_a_difference_so_it_raises(name, side, list_equal):
    a, b = conds("host"), conds("builtin")
    drop_tensor(a if side == "first" else b, name)
    with pytest.raises(ValueError, match=name.replace(".", r"\.")):
        tts_runner.same_voice(a, b)


def test_equality_goes_through_torch_equal_on_cpu_copies(monkeypatch):
    """The real _equal: torch.equal, after moving both sides to the CPU so a cuda tensor and a cpu one compare."""
    calls = []

    class T:
        def __init__(self, v):
            self.v = v

        def detach(self):
            calls.append("detach")
            return self

        def cpu(self):
            calls.append("cpu")
            return self

    fake_torch = SimpleNamespace(equal=lambda x, y: x.v == y.v)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    assert tts_runner._equal(T(1), T(1)) is True
    assert tts_runner._equal(T(1), T(2)) is False
    assert calls.count("cpu") == 4


# ---------- the ops interface tts_plan.synthesize needs ----------

@pytest.mark.parametrize("name", ["copy", "seed", "silence", "concat", "save", "seconds", "same_voice"])
def test_torch_ops_has_every_op_the_plan_uses(name):
    assert callable(getattr(tts_runner.TorchOps, name))


class FakeModel:
    sr = 10

    def __init__(self):
        self.conds = None
        self.used = []

    def generate(self, text, **params):
        self.used.append((text, self.conds.t3.speaker_emb[0]))
        return [0.0] * 5


class ListOps(tts_runner.TorchOps):
    """The runner's real copy and same_voice with list stand-ins for the torch audio helpers."""
    seed = staticmethod(lambda n: None)
    silence = staticmethod(lambda seconds, sr: [0.0] * int(seconds * sr))
    concat = staticmethod(lambda pieces: [x for p in pieces for x in p])
    save = staticmethod(lambda path, wav, sr: Path(path).write_text("wav"))
    seconds = staticmethod(lambda wav, sr: len(wav) / sr)


def lines_for(*voices):
    return [{"id": f"line_{i}", "voice": v, "text": "One. Two.", "params": {}} for i, v in enumerate(voices)]


def test_a_harness_given_a_copy_of_the_host_conds_is_refused_before_anything_is_voiced(tmp_path, list_equal):
    host = conds("host")
    model = FakeModel()
    with pytest.raises(ValueError, match="same voice as the host"):
        tts_plan.synthesize(model, lines_for("host", "codex"), {"host": host, "codex": copy.deepcopy(host)},
                            ListOps, tmp_path)
    assert model.used == [] and list(tmp_path.iterdir()) == []


def test_positive_control_distinct_builtin_conds_voice_every_chunk_in_the_right_voice(tmp_path, list_equal):
    model = FakeModel()
    tts_plan.synthesize(model, lines_for("host", "codex", "host"),
                        {"host": conds("host"), "codex": conds("builtin")}, ListOps, tmp_path)
    assert [tag for _, tag in model.used] == ["host", "host", "builtin", "builtin", "host", "host"]


# ---------- voice_lines and main against a fake chatterbox.tts module ----------

class FakeChatterbox:
    """Stand-in for chatterbox.tts.ChatterboxTTS with what voice_lines touches, behaving like 0.1.7 where it matters:
    one set of conds on the model, prepare_conditionals(wav) replaces it with conds built from that file, and
    generate() uses whatever the model holds. `events` is the ordered log of what was called with what."""
    sr = 10
    created: list = []
    has_builtin = True
    in_place = False

    def __init__(self, device):
        self.device = device
        self.conds = conds("builtin") if self.has_builtin else None
        self.events = [("load", device)]
        type(self).created.append(self)

    @classmethod
    def from_pretrained(cls, device):
        return cls(device)

    def prepare_conditionals(self, wav_fpath, exaggeration=0.5):
        tag = "host:" + hashlib.sha256(Path(wav_fpath).read_bytes()).hexdigest()[:16]  # built from the file's content
        self.events.append(("prepare", str(wav_fpath), exaggeration, self.conds.t3.speaker_emb[0]))
        new = conds(tag)
        if self.in_place:  # a library that mutates its conds object rather than replacing it
            self.conds.t3, self.conds.gen = new.t3, new.gen
        else:
            self.conds = new

    def generate(self, text, **params):
        assert self.conds is not None, "Please `prepare_conditionals` first"
        self.events.append(("generate", text, self.conds.t3.speaker_emb[0], params))
        return [0.0] * 5


@pytest.fixture
def fake_chatterbox(monkeypatch, list_equal):
    """Inject a fake chatterbox.tts, put list stand-ins under the torch audio ops and force the CPU."""

    class Fake(FakeChatterbox):
        created = []

    pkg, mod = ModuleType("chatterbox"), ModuleType("chatterbox.tts")
    mod.ChatterboxTTS = Fake
    pkg.tts = mod
    monkeypatch.setitem(sys.modules, "chatterbox", pkg)
    monkeypatch.setitem(sys.modules, "chatterbox.tts", mod)
    monkeypatch.setattr(tts_runner, "TorchOps", ListOps)
    monkeypatch.setattr(tts_runner, "_cuda_free_bytes", lambda: None)
    return Fake


HOST_TAG = "host:" + hashlib.sha256(REF_BYTES).hexdigest()[:16]
COLD = {"exaggeration": 0.7, "cfg_weight": 0.3, "temperature": 0.8}
CODEX = {"exaggeration": 0.4, "cfg_weight": 0.5, "temperature": 0.7}
CLAUDE = {"exaggeration": 0.5, "cfg_weight": 0.5, "temperature": 0.8}
HOST = {"exaggeration": 0.55, "cfg_weight": 0.45, "temperature": 0.8}


def plan_lines():
    return [{"id": "cold_open", "voice": "host", "text": "One. Two.", "params": COLD},
            {"id": "confessional", "voice": "codex", "text": "Three.", "params": CODEX},
            {"id": "story", "voice": "claude_code", "text": "Four.", "params": CLAUDE},
            {"id": "out", "voice": "host", "text": "Five.", "params": HOST}]


@pytest.fixture
def voices(voices_path):
    return voiceconf.load_voices(voices_path)


@pytest.mark.parametrize("in_place", [False, True])
def test_voice_lines_snapshots_builtin_first_and_voices_each_line_in_its_own_voice(
        fake_chatterbox, voices, tmp_path, in_place, monkeypatch):
    fake_chatterbox.in_place = in_place
    entry = {}
    real = tts_plan.synthesize

    def spy(model, lines, conds_by_voice, ops, out_dir, **kw):
        entry.update(model_conds=model.conds, tags={k: v.t3.speaker_emb[0] for k, v in conds_by_voice.items()})
        return real(model, lines, conds_by_voice, ops, out_dir, **kw)

    monkeypatch.setattr(tts_plan, "synthesize", spy)
    dev, results = tts_runner.voice_lines(plan_lines(), tmp_path, voices)
    model, = fake_chatterbox.created
    assert dev == "cpu" and [r["id"] for r in results] == ["cold_open", "confessional", "story", "out"]
    # the model is loaded once, on the chosen device
    assert [e for e in model.events if e[0] == "load"] == [("load", "cpu")]
    # the built-in snapshot was taken before the host's reference was prepared: the model held "builtin" when
    # prepare_conditionals ran, and every harness is mapped to that snapshot, whether or not the library mutates in place
    prepares = [e for e in model.events if e[0] == "prepare"]
    assert len(prepares) == 1 and prepares[0][3] == "builtin"
    assert entry["tags"] == {"host": HOST_TAG, **{h: "builtin" for h in voiceconf.harnesses(voices)}}
    # nothing is left on the model for a chunk to inherit when the plan starts
    assert entry["model_conds"] is None
    # every chunk was voiced in its own line's voice
    voiced = [(e[1], e[2]) for e in model.events if e[0] == "generate"]
    assert voiced == [("One.", HOST_TAG), ("Two.", HOST_TAG), ("Three.", "builtin"), ("Four.", "builtin"),
                      ("Five.", HOST_TAG)]


def test_voice_lines_prepares_the_host_from_the_configured_reference_and_exaggeration(
        fake_chatterbox, voices, tmp_path):
    tts_runner.voice_lines(plan_lines(), tmp_path, voices)
    model, = fake_chatterbox.created
    (_, path, exaggeration, _), = [e for e in model.events if e[0] == "prepare"]
    assert path == str(voiceconf.ref_path(voices["host"])) and Path(path).read_bytes() == REF_BYTES
    assert exaggeration == voices["host"]["params"]["exaggeration"] == 0.55
    assert exaggeration not in (0.5, voices["host"]["params_cold_open"]["exaggeration"])  # not the library default


def test_voice_lines_passes_each_lines_params_through_unchanged(fake_chatterbox, voices, tmp_path):
    tts_runner.voice_lines(plan_lines(), tmp_path, voices)
    model, = fake_chatterbox.created
    params = [e[3] for e in model.events if e[0] == "generate"]
    assert params == [COLD, COLD, CODEX, CLAUDE, HOST]


def test_voice_lines_without_a_builtin_voice_exits_before_preparing_the_host(fake_chatterbox, voices, tmp_path):
    fake_chatterbox.has_builtin = False
    with pytest.raises(SystemExit, match="no built-in voice"):
        tts_runner.voice_lines(plan_lines(), tmp_path, voices)
    model, = fake_chatterbox.created
    assert [e[0] for e in model.events] == ["load"]


def test_voice_lines_uses_cuda_when_there_is_room(fake_chatterbox, voices, tmp_path, monkeypatch):
    monkeypatch.setattr(tts_runner, "_cuda_free_bytes", lambda: tts_runner.MIN_FREE_CUDA)
    dev, _ = tts_runner.voice_lines(plan_lines(), tmp_path, voices)
    assert dev == "cuda" and fake_chatterbox.created[0].events[0] == ("load", "cuda")


def test_main_voices_end_to_end_from_the_verified_reference(
        fake_chatterbox, lines_path, voices_path, lock, tmp_path, capsys):
    out = tmp_path / "voice"
    run(lines_path, out, voices_path, lock)
    model, = fake_chatterbox.created
    (_, path, _, _), = [e for e in model.events if e[0] == "prepare"]
    ref = json.loads(voices_path.read_text(encoding="utf-8"))["voices"]["host"]["ref"]
    assert path == ref
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest().startswith(
        json.loads(voices_path.read_text(encoding="utf-8"))["voices"]["host"]["sha256_prefix"])
    assert sorted(p.name for p in out.glob("*.wav")) == ["cold_open.wav", "confessional.wav"]
    res = json.loads((out / "tts-results.json").read_text(encoding="utf-8"))
    assert res["device"] == "cpu" and [r["id"] for r in res["lines"]] == ["cold_open", "confessional"]
    assert capsys.readouterr().out.strip().splitlines()[-1] == "RUNNER_OK"
    assert lock_is_free(lock)


def test_results_record_per_line_and_total_synthesis_seconds(
        fake_chatterbox, lines_path, voices_path, lock, tmp_path, monkeypatch):
    ticks = iter(range(0, 1000, 3))  # every clock read advances 3 s, so each line takes a positive, known time
    monkeypatch.setattr(tts_runner.time, "monotonic", lambda: float(next(ticks)))
    out = tmp_path / "voice"
    run(lines_path, out, voices_path, lock)
    res = json.loads((out / "tts-results.json").read_text(encoding="utf-8"))
    assert res["device"] == "cpu"
    secs = [r["seconds"] for r in res["lines"]]
    assert len(secs) == 2 and all(isinstance(x, float) and x > 0 for x in secs)
    assert isinstance(res["synth_seconds"], float) and res["synth_seconds"] >= sum(secs) - 1e-6


def test_a_reference_that_fails_its_hash_never_loads_the_model(
        fake_chatterbox, lines_path, voices_path, lock, tmp_path):
    ref = Path(json.loads(voices_path.read_text(encoding="utf-8"))["voices"]["host"]["ref"])
    ref.write_bytes(b"someone else's recording")
    with pytest.raises(SystemExit, match="does not start with"):
        run(lines_path, tmp_path / "voice", voices_path, lock)
    assert fake_chatterbox.created == []


# ---------- real tensors, no model: needs the Chatterbox venv ----------

@needs_venv
def test_in_the_venv_same_voice_and_the_audio_ops_work_on_real_tensors(tmp_path):
    script = textwrap.dedent(f"""
        import importlib.util, sys, wave
        import torch
        from chatterbox.tts import Conditionals
        from chatterbox.models.t3.modules.cond_enc import T3Cond
        spec = importlib.util.spec_from_file_location("tts_runner_venv", {str(REPO / "daily" / "tts_runner.py")!r})
        r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)

        def mk(seed):
            g = torch.Generator().manual_seed(seed)
            t3 = T3Cond(speaker_emb=torch.randn(1, 256, generator=g),
                        cond_prompt_speech_tokens=torch.randint(0, 6000, (1, 150), generator=g),
                        emotion_adv=0.5 * torch.ones(1, 1, 1))
            gen = dict(prompt_token=torch.randint(0, 6000, (1, 157), generator=g),
                       prompt_token_len=torch.tensor([157]), prompt_feat=torch.randn(1, 314, 80, generator=g),
                       prompt_feat_len=None, embedding=torch.randn(1, 192, generator=g))
            return Conditionals(t3, gen)

        host, other = mk(1), mk(2)
        assert r.same_voice(other, host) is False
        assert r.same_voice(r.TorchOps.copy(host), host) is True
        leak = mk(2); leak.gen["embedding"] = host.gen["embedding"].clone()
        assert r.same_voice(leak, host) is True
        assert r.same_voice(mk(3), mk(4)) is False
        assert r._equal(torch.ones(2), torch.ones(2)) is True and r._equal(torch.ones(2), torch.ones(3)) is False

        ops = r.TorchOps
        wav = ops.concat([torch.zeros(1, 12000), ops.silence(0.5, 24000)])
        assert abs(ops.seconds(wav, 24000) - 1.0) < 1e-6
        ops.seed(7); a = torch.rand(1); ops.seed(7); assert torch.equal(a, torch.rand(1))
        ops.save({str(tmp_path / "x.wav")!r}, wav, 24000)
        with wave.open({str(tmp_path / "x.wav")!r}) as w:
            assert w.getframerate() == 24000 and w.getnframes() == 24000 and w.getnchannels() == 1
        print("VENV_OK")
    """)
    env = {"PATH": "/usr/bin:/bin", "CUDA_VISIBLE_DEVICES": "", "HOME": str(Path.home())}
    r = subprocess.run([VENV_PY, "-I", "-c", script], capture_output=True, text=True, env=env, timeout=300)
    assert "VENV_OK" in r.stdout, r.stderr[-2000:]


@needs_venv
def test_in_the_venv_copy_conds_handles_non_leaf_tensors_independently(tmp_path):
    script = textwrap.dedent(f"""
        import importlib.util
        import torch
        from chatterbox.tts import Conditionals
        from chatterbox.models.t3.modules.cond_enc import T3Cond
        spec = importlib.util.spec_from_file_location("tts_runner_venv", {str(REPO / "daily" / "tts_runner.py")!r})
        r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)

        def nl(*shape):
            return torch.ones(*shape, requires_grad=True) * 2  # non-leaf: has grad history

        t3 = T3Cond(speaker_emb=nl(1, 4), cond_prompt_speech_tokens=torch.arange(5).view(1, 5), emotion_adv=0.5)
        gen = dict(embedding=nl(1, 3), prompt_token_len=torch.tensor([7]), prompt_feat_len=None,
                   parts=[nl(2), (nl(2),)])
        orig = Conditionals(t3, gen)
        assert not orig.t3.speaker_emb.is_leaf
        try:
            import copy; copy.deepcopy(orig); raise SystemExit("deepcopy unexpectedly worked")
        except RuntimeError:
            pass
        for cp in (r.copy_conds(orig), r.TorchOps.copy(orig)):
            assert isinstance(cp, Conditionals) and isinstance(cp.t3, T3Cond)
            for t in (cp.t3.speaker_emb, cp.gen["embedding"], cp.gen["parts"][0], cp.gen["parts"][1][0]):
                assert t.requires_grad is False and t.is_leaf
            assert cp.t3.emotion_adv == 0.5 and isinstance(cp.t3.emotion_adv, float)
            assert cp.t3.clap_emb is None and cp.gen["prompt_feat_len"] is None
            assert isinstance(cp.gen["parts"], list) and isinstance(cp.gen["parts"][1], tuple)
            assert torch.equal(cp.t3.speaker_emb, orig.t3.speaker_emb.detach())
            assert torch.equal(cp.t3.cond_prompt_speech_tokens, orig.t3.cond_prompt_speech_tokens)
            assert cp.t3.cond_prompt_speech_tokens.dtype == torch.int64
            before = orig.t3.speaker_emb.detach().clone()
            cp.t3.speaker_emb.zero_(); cp.gen["embedding"].zero_(); cp.gen["parts"][0].zero_()
            assert torch.equal(orig.t3.speaker_emb.detach(), before)
            assert float(orig.gen["embedding"].detach().sum()) == 6.0 and float(orig.gen["parts"][0].detach().sum()) == 4.0
        c2 = r.copy_conds(orig)
        assert r.same_voice(c2, orig) is True
        other = Conditionals(T3Cond(speaker_emb=nl(1, 4) + 1), dict(embedding=nl(1, 3) + 1))
        assert r.same_voice(other, orig) is False
        print("VENV_OK")
    """)
    env = {"PATH": "/usr/bin:/bin", "CUDA_VISIBLE_DEVICES": "", "HOME": str(Path.home())}
    r = subprocess.run([VENV_PY, "-I", "-c", script], capture_output=True, text=True, env=env, timeout=300)
    assert "VENV_OK" in r.stdout, r.stderr[-2000:]


def test_report_line_prints_one_flushed_progress_line(capsys):
    tts_runner.report_line({"id": "story_0", "voice": "host", "seconds": 227.5, "text": "never printed"})
    assert capsys.readouterr().out == "voiced story_0 voice=host seconds=227.5\n"


def test_voice_lines_reports_each_line_as_it_finishes(fake_chatterbox, voices, tmp_path, capsys):
    tts_runner.voice_lines(plan_lines(), tmp_path, voices)
    out = capsys.readouterr().out.splitlines()
    assert [ln.split()[1] for ln in out] == ["cold_open", "confessional", "story", "out"]
    assert all(ln.startswith("voiced ") and "seconds=" in ln for ln in out)


class ModelLoad(Exception):
    """Raised by the fake Chatterbox below when the runner goes on to load the model."""


@pytest.fixture
def loads_recorded(monkeypatch):
    """A fake chatterbox.tts whose from_pretrained records the device and raises ModelLoad, so a test can tell "went on
    to load the model" from "stopped before it" without any real model (or a real Chatterbox install) involved."""
    seen = []

    class Recorder:
        @classmethod
        def from_pretrained(cls, device):
            seen.append(device)
            raise ModelLoad(device)

    pkg, mod = ModuleType("chatterbox"), ModuleType("chatterbox.tts")
    mod.ChatterboxTTS = Recorder
    pkg.tts = mod
    monkeypatch.setitem(sys.modules, "chatterbox", pkg)
    monkeypatch.setitem(sys.modules, "chatterbox.tts", mod)
    return seen


def test_the_require_cuda_switch_is_named_for_this_project():
    assert tts_runner.REQUIRE_CUDA_ENV == "ORION_CLOCK_TTS_REQUIRE_CUDA" and tts_runner.NO_CUDA_EXIT == 3


@pytest.mark.parametrize("flag,exits", [("1", True), (None, False), ("0", False)])
def test_require_cuda_exits_at_once_on_cpu_before_the_model_loads(monkeypatch, tmp_path, flag, exits, loads_recorded):
    monkeypatch.setattr(tts_runner, "device", lambda: "cpu")
    if flag:
        monkeypatch.setenv(tts_runner.REQUIRE_CUDA_ENV, flag)
    else:
        monkeypatch.delenv(tts_runner.REQUIRE_CUDA_ENV, raising=False)
    if exits:
        with pytest.raises(SystemExit) as e:
            tts_runner.voice_lines([], tmp_path, {})
        assert e.value.code == tts_runner.NO_CUDA_EXIT and loads_recorded == []  # nothing was loaded
    else:  # no switch: it goes on to load the model on the CPU
        with pytest.raises(ModelLoad):
            tts_runner.voice_lines([], tmp_path, {})
        assert loads_recorded == ["cpu"]


def test_require_cuda_lets_a_cuda_device_through(monkeypatch, tmp_path, loads_recorded):
    monkeypatch.setattr(tts_runner, "device", lambda: "cuda")
    monkeypatch.setenv(tts_runner.REQUIRE_CUDA_ENV, "1")
    with pytest.raises(ModelLoad):
        tts_runner.voice_lines([], tmp_path, {})
    assert loads_recorded == ["cuda"]
