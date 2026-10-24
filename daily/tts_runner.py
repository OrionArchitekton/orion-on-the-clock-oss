#!/usr/bin/env python3
"""Voice the daily build's lines with Chatterbox. Run it with the Python of the environment Chatterbox is installed in
(settings.toml `tts.python`, or `tts.remote_python` on a remote host):

    /path/to/chatterbox-venv/bin/python tts_runner.py LINES_JSON OUT_DIR VOICES_JSON

LINES_JSON: [{"id", "voice", "text", "params"}], voice "host" or an agent id. Writes OUT_DIR/<id>.wav (24 kHz) and
OUT_DIR/tts-results.json, then prints RUNNER_OK as its last line. The host voice is the operator's reference recording;
its sha256 prefix is checked here through voiceconf as well as by the caller. Every agent voice is Chatterbox's
built-in voice, snapshotted at load and set fresh before each chunk (tts_plan.synthesize), so no agent line can inherit
the host's conditionals, and same_voice proves by content that an agent is not in the host's voice.

This process takes the shared TTS lock itself (one Chatterbox at a time on a shared GPU) and holds it until it exits,
so a caller must NOT hold that lock around this process: the two would wait for each other. The lock file is
$ORION_CLOCK_TTS_LOCK (settings.toml `tts.lock` sets it for a local run), else DEFAULT_LOCK; point other TTS jobs on
the machine at the same file to keep them apart. Every input is checked before the lock is taken (the voices file, the
host reference hash and tts_plan.check_lines on the lines), so a bad input never queues behind another voicing. Inside
the lock it first deletes the previous results file and each line's old <id>.wav, so a failed run leaves no stale take
to be read. CUDA is used only when both torch and nvidia-smi say at least 4.5 GiB is free. The Chatterbox, torch and
torchaudio imports are lazy: importing this module in a plain Python (the tests do) loads none of them."""
from __future__ import annotations

import contextlib
import copy as _copy
import dataclasses
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:  # imported as daily.tts_runner (tests)
    from . import files, tts_plan, voiceconf
except ImportError:  # run as a script in the Chatterbox venv, whose python cannot import the daily package
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import files  # noqa: E402
    import tts_plan  # noqa: E402
    import voiceconf  # noqa: E402

LOCK_ENV = "ORION_CLOCK_TTS_LOCK"
DEFAULT_LOCK = Path("~/.local/state/orion-on-the-clock/tts.lock")
LOCK_WAIT_S = 1800
LOCK_POLL_S = 2.0
SMI_TIMEOUT_S = 10
MIN_FREE_CUDA = 4.5 * 1024 ** 3  # Chatterbox needs about this much GPU memory free to load and run
RESULTS = "tts-results.json"
REQUIRE_CUDA_ENV = "ORION_CLOCK_TTS_REQUIRE_CUDA"  # "1": exit at once instead of voicing on CPU (a remote run sets it)
NO_CUDA_EXIT = 3

# The Conditionals tensors that say WHO is speaking (chatterbox 0.1.7 tts.py: Conditionals.t3 is a T3Cond, .gen is the
# S3Gen reference dict), as (part, name). Left out on purpose: the prompt lengths and emotion_adv, which are a length
# and the exaggeration setting, not a voice, and clap_emb, cond_prompt_speech_emb and prompt_feat_len, which are None.
IDENTITY = (
    ("t3", "speaker_emb"),                # voice-encoder speaker embedding
    ("t3", "cond_prompt_speech_tokens"),  # speech tokens of the reference the T3 model continues from
    ("gen", "prompt_token"),              # speech tokens of the reference for the S3Gen decoder
    ("gen", "prompt_feat"),               # mel features of the reference for the S3Gen decoder
    ("gen", "embedding"),                 # CAM++ x-vector speaker embedding
)
REQUIRED = (("t3", "speaker_emb"), ("gen", "embedding"))  # both always exist in a real voice


def default_lock_path() -> Path:
    """The shared TTS lock file: $ORION_CLOCK_TTS_LOCK when set, else DEFAULT_LOCK (read at call time)."""
    return Path(os.environ.get(LOCK_ENV) or DEFAULT_LOCK).expanduser()


def _identity(conds) -> dict:
    found = {}
    for part, name in IDENTITY:
        holder = getattr(conds, part, None)
        value = holder.get(name) if isinstance(holder, dict) else getattr(holder, name, None)
        if value is not None:
            found[f"{part}.{name}"] = value
    return found


def _equal(x, y) -> bool:
    import torch
    return bool(torch.equal(x.detach().cpu(), y.detach().cpu()))


def same_voice(a, b) -> bool:
    """True when two sets of Conditionals share the content of ANY identity tensor (IDENTITY), i.e. when some part of one
    voice is in the other: a harness must differ from the host in every one of them. A tensor absent from either side
    is no evidence of a match, but a missing speaker embedding on either side cannot prove a difference, so it raises."""
    ia, ib = _identity(a), _identity(b)
    for part, name in REQUIRED:
        key = f"{part}.{name}"
        if key not in ia or key not in ib:
            raise ValueError(f"cannot compare voices: {key} is missing")
    return any(_equal(ia[k], ib[k]) for k in ia if k in ib)


def copy_conds(obj):
    """Independent copy of Chatterbox Conditionals. copy.deepcopy fails on real conditionals (they hold non-leaf
    tensors), so walk dataclasses, dicts, lists and tuples and replace every tensor with detach().clone(); anything
    else (floats, None, ints) is copied by value. Without torch there are no tensors to special-case."""
    try:
        import torch
    except ImportError:
        torch = None
    if torch is not None and isinstance(obj, torch.Tensor):
        return obj.detach().clone()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.replace(obj, **{f.name: copy_conds(getattr(obj, f.name))
                                           for f in dataclasses.fields(obj) if f.init})
    if isinstance(obj, dict):
        return {k: copy_conds(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(copy_conds(v) for v in obj)
    return _copy.deepcopy(obj)


class TorchOps:
    """The ops tts_plan.synthesize runs on, over torch tensors. The torch and torchaudio imports are inside."""

    @staticmethod
    def copy(conds):
        return copy_conds(conds)

    same_voice = staticmethod(same_voice)

    @staticmethod
    def seed(n):
        import torch
        torch.manual_seed(n)

    @staticmethod
    def silence(seconds, sr):
        import torch
        return torch.zeros(1, int(seconds * sr))

    @staticmethod
    def concat(pieces):
        import torch
        return torch.cat([p.cpu() for p in pieces], dim=-1)

    @staticmethod
    def save(path, wav, sr):
        import torchaudio
        torchaudio.save(str(path), wav, sr)

    @staticmethod
    def seconds(wav, sr):
        return wav.shape[-1] / sr


def _smi_free_bytes(gpu_uuid=None):
    """Free memory in bytes as nvidia-smi reports it (for the GPU with this torch uuid, else the lowest of all GPUs),
    or None when nvidia-smi is missing, slow, fails or prints something that is not a number. On WSL torch's
    mem_get_info can read well above what nvidia-smi says is free, so both are asked."""
    argv = ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"]
    if gpu_uuid:
        argv.insert(1, f"--id=GPU-{gpu_uuid}")
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=SMI_TIMEOUT_S)
        if r.returncode != 0:
            return None
        mib = [int(line.strip()) for line in r.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not mib or min(mib) < 0:
        return None
    return min(mib) * 1024 ** 2


def _cuda_free_bytes():
    """Free GPU memory in bytes, or None without a usable CUDA: the LOWER of torch's figure and nvidia-smi's for this
    GPU, because the real run needs the memory to be free for the model, whichever reading is the honest one."""
    import torch
    if not torch.cuda.is_available():
        return None
    free = torch.cuda.mem_get_info()[0]
    props = torch.cuda.get_device_properties(torch.cuda.current_device())
    uuid = getattr(props, "uuid", None)
    smi = _smi_free_bytes(str(uuid) if uuid else None)
    return free if smi is None else min(free, smi)


def device() -> str:
    free = _cuda_free_bytes()
    return "cuda" if free is not None and free >= MIN_FREE_CUDA else "cpu"


@contextlib.contextmanager
def hold_tts_lock(path: Path, wait_s: float, poll_s: float):
    """Hold the exclusive TTS lock (flock) for the with-block, waiting up to wait_s; closing the file drops it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a")
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    sys.exit(f"TTS lock busy for {wait_s:.0f} s: {path}")
                time.sleep(poll_s)
        yield
    finally:
        fh.close()


def read_lines(path: Path) -> list[dict]:
    try:
        lines = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        sys.exit(f"lines: cannot read {path} as JSON")
    if not isinstance(lines, list) or not lines:
        sys.exit("lines: need a non-empty JSON list")
    for i, ln in enumerate(lines):
        if not (isinstance(ln, dict) and all(isinstance(ln.get(k), str) for k in ("id", "voice", "text"))
                and isinstance(ln.get("params"), dict)):
            sys.exit(f"lines: line {i} needs a string id, voice and text and an object params")
    return lines


def report_line(row: dict) -> None:
    """One flushed progress line per voiced line: its id, voice and synthesis seconds (ids and voice names are safe
    tokens), so the log shows how far a long CPU run has got."""
    print(f"voiced {row['id']} voice={row['voice']} seconds={row.get('seconds', 0.0)}", flush=True)


def voice_lines(lines: list[dict], out_dir: Path, voices: dict) -> tuple[str, list[dict]]:
    """Load the model once and voice every line: (device, per-line results). The only place the model is loaded."""
    dev = device()
    if dev == "cpu" and os.environ.get(REQUIRE_CUDA_ENV) == "1":  # before the model loads: nothing is wasted
        print("NO_CUDA: cuda required but not available", file=sys.stderr, flush=True)
        sys.exit(NO_CUDA_EXIT)
    from chatterbox.tts import ChatterboxTTS
    model = ChatterboxTTS.from_pretrained(device=dev)
    if model.conds is None:
        sys.exit("Chatterbox has no built-in voice (conds.pt missing)")
    builtin = copy_conds(model.conds)  # snapshot before the host's reference replaces model.conds
    host = voices["host"]
    model.prepare_conditionals(str(voiceconf.ref_path(host)), exaggeration=host["params"]["exaggeration"])
    conds = {"host": copy_conds(model.conds)}
    for name in voices:
        if name != "host":
            conds[name] = builtin
    model.conds = None  # nothing is left on the model to inherit: a chunk with no conditionals of its own fails loudly
    return dev, tts_plan.synthesize(model, lines, conds, TorchOps, out_dir, clock=time.monotonic,
                                 progress=report_line)


def main(argv: list[str], lock_path: Path | None = None, lock_wait_s: float = LOCK_WAIT_S,
         poll_s: float = LOCK_POLL_S) -> None:
    if len(argv) != 4:
        sys.exit(__doc__)
    out = Path(argv[2])
    try:
        voices = voiceconf.load_voices(Path(argv[3]))
    except voiceconf.VoiceConfigError as e:
        sys.exit(f"voices: {e}")
    ok, digest = voiceconf.ref_ok(voices["host"])
    if not ok:
        sys.exit(voiceconf.REF_MESSAGES.get(digest, "voice reference refused"))
    lines = read_lines(Path(argv[1]))
    try:
        tts_plan.check_lines(lines, voices)  # the plan's own line checks, before the lock and the model load
    except (ValueError, KeyError) as e:
        sys.exit(f"lines: {e.args[0] if e.args else e}")
    out.mkdir(parents=True, exist_ok=True)
    with hold_tts_lock(lock_path if lock_path is not None else default_lock_path(), lock_wait_s, poll_s):
        # a failed run must not leave a previous run's results, or a stale or partial take of a line, to be read
        (out / RESULTS).unlink(missing_ok=True)
        for ln in lines:  # ids are safe file names: check_lines allows only lowercase letters, digits and underscores
            (out / f"{ln['id']}.wav").unlink(missing_ok=True)
        dev, results = voice_lines(lines, out, voices)
        files.write_json(out / RESULTS, {"device": dev, "ref_sha256": digest, "lines": results,
                                         "synth_seconds": round(sum(r.get("seconds", 0.0) for r in results), 3)})
    print(f"device {dev}: {len(results)} line(s)", flush=True)
    print("RUNNER_OK", flush=True)


if __name__ == "__main__":
    main(sys.argv)
