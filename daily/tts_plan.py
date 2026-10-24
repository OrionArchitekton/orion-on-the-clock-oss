"""The voicing plan the Chatterbox runner executes, kept free of torch so it can be tested anywhere.

Chatterbox keeps one set of voice conditionals on the model, and generate(audio_prompt_path=...) replaces it, so a
line voiced after a host line would inherit the host's cloned voice. synthesize() therefore sets a fresh copy of the
line's own conditionals before every chunk it generates: an agent line is never spoken in the host's voice. One
generation per sentence, a fixed seed per chunk index, chunks joined with a fixed gap."""
from __future__ import annotations

import re
from pathlib import Path

SEED = 4207
GAP_S = 0.2
HOST = "host"
_SENT = re.compile(r"(?<=[.?!])\s+")
_ID = re.compile(r"[a-z0-9_]+")


def chunks(text: str) -> list[str]:
    return [p for p in (s.strip() for s in _SENT.split(text.strip())) if p]


def check_lines(lines: list[dict], known_voices) -> None:
    """The structural line checks, which need no conditionals and no ops: id shape and uniqueness (an id becomes a file
    name), a known voice, text that makes at least one chunk, and no audio_prompt_path in params (it would replace the
    line's own conditionals). The runner calls this before it takes the lock or loads the model; _check calls it too."""
    seen = set()
    for line in lines:
        ident = line["id"]
        if not isinstance(ident, str) or not _ID.fullmatch(ident):
            raise ValueError(f"line id {ident!r} must be lowercase letters, digits and underscores")
        if ident in seen:
            raise ValueError(f"duplicate line id {ident!r}")
        seen.add(ident)
        voice = line["voice"]
        if voice not in known_voices:
            raise KeyError(f"{ident}: no conditionals for voice {voice!r}")
        if not chunks(line["text"]):
            raise ValueError(f"{ident}: empty text")
        if "audio_prompt_path" in line["params"]:
            raise ValueError(f"{ident}: params carry audio_prompt_path, which would replace the line's own "
                             "conditionals")


def _check(lines: list[dict], conds_by_voice: dict, ops) -> None:
    """Refuse the whole plan before any chunk is generated, so a plan that cannot finish spends no GPU time and
    writes no audio. The harness-versus-host test is by content as well as identity: a deep copy of the host's conditionals
    is a different object but the same voice, so ops.same_voice (a content comparison, required) decides, and the
    host's own conditionals are required in every plan so that comparison always has something to compare with."""
    same_voice = getattr(ops, "same_voice", None)
    if not callable(same_voice):
        raise ValueError("ops has no usable same_voice(a, b): cannot prove a harness is not in the host's voice")
    host = conds_by_voice.get(HOST)
    if host is None:  # with nothing to compare against, a copy of the host's conds on a harness could not be seen
        raise ValueError("conds_by_voice has no usable 'host' conditionals (missing or None): the host's voice must "
                         "always be loaded so every harness can be compared with it")
    check_lines(lines, conds_by_voice)
    for line in lines:
        ident, voice = line["id"], line["voice"]
        conds = conds_by_voice[voice]
        if conds is None:
            raise ValueError(f"{ident}: conditionals for voice {voice!r} are None")
        if voice == HOST:
            continue
        if conds is host:
            raise ValueError(f"{ident}: voice {voice!r} is set to the host's conditionals")
        same = same_voice(conds, host)
        if not isinstance(same, bool):
            raise ValueError(f"{ident}: same_voice must answer with a bool, not {type(same).__name__}")
        if same:
            raise ValueError(f"{ident}: voice {voice!r} has the same voice as the host's conditionals")


def synthesize(model, lines: list[dict], conds_by_voice: dict, ops, out_dir: Path,
               seed: int = SEED, gap_s: float = GAP_S, clock=None, progress=None) -> list[dict]:
    """lines: [{"id", "voice", "text", "params"}]. ops supplies copy, seed, silence, concat, save and seconds, so the
    same plan runs on torch tensors (tts_runner.TorchOps) or on lists (tests), plus same_voice(a, b), a content
    comparison of two sets of conditionals. With a clock (a monotonic seconds function) each result also carries
    "seconds", the time spent voicing that line. `progress`, when given, is called with each line's result row as soon
    as that line is saved, so a long run can report as it goes. A voice with no conditionals is a KeyError: there is no fallback voice."""
    out_dir = Path(out_dir)
    _check(lines, conds_by_voice, ops)
    results = []
    for line in lines:
        t0 = clock() if clock else None
        parts = chunks(line["text"])
        pieces = []
        for ci, text in enumerate(parts):
            model.conds = ops.copy(conds_by_voice[line["voice"]])
            ops.seed(seed + ci)
            pieces.append(model.generate(text, **line["params"]))
            if ci < len(parts) - 1:
                pieces.append(ops.silence(gap_s, model.sr))
        full = ops.concat(pieces)
        ops.save(out_dir / f"{line['id']}.wav", full, model.sr)
        results.append({"id": line["id"], "voice": line["voice"], "text": line["text"], "chunks": len(parts),
                        "dur": round(ops.seconds(full, model.sr), 3)})
        if clock:
            results[-1]["seconds"] = round(clock() - t0, 3)
        if progress:
            progress(results[-1])
    return results
