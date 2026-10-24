"""The episode's sound: each voice line at its timeline start; the show theme under the open and the out and a bed under
the talk (story, Steal-this and, when the day has one, the confessional), both ducked under speech; the sound kit's
cues; then two-pass loudnorm to -14 LUFS / TP -1.5 at exactly the timeline's length. Beds and cues come from your media
kit (settings.toml `render.kit`, docs/media-kit.md), never from git.

The confessional is optional: on a day without one there is no confessional scene, no agent sting and the bed ends
after the Steal-this scene. The scene table decides, never the agent's name. An agent's sting is the kit file
sound/entrant_<agent id>.wav and is optional: an agent without one gets no sting. Every other kit file is required,
and a missing one fails the mix before any work."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import numpy as np

SR = 48000
FFMPEG_TIMEOUT_S = 300
BEDS = {"theme": ("sound/theme.wav", 0.40), "bed": ("music/bed.wav", 0.26)}
CUES = {
    "jingle": ("sound/scoreboard_jingle.wav", -6.0),
    "section": ("sound/section_break.wav", -8.0),
    "pop": ("sound/word_pop.wav", -6.0),
    "cta": ("sound/cta_pop.wav", -3.0),
}
ENTRANT_GAIN = -6.0
_AGENT = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


def entrant_rel(harness) -> str | None:
    """The kit path of an agent's sting (sound/entrant_<agent id>.wav), or None for anything that is not an agent id."""
    return f"sound/entrant_{harness}.wav" if isinstance(harness, str) and _AGENT.match(harness) else None


def cue_file(cue: str) -> tuple:
    """(kit path, gain in dB) of a cue: a fixed cue from CUES, or an agent's sting ("entrant_<agent id>")."""
    if cue in CUES:
        return CUES[cue]
    rel = entrant_rel(cue.removeprefix("entrant_")) if cue.startswith("entrant_") else None
    if rel is None:
        raise KeyError(cue)
    return rel, ENTRANT_GAIN


XF_S = 0.6
TARGET = "I=-14:TP=-1.5:LRA=11"
_LUFS = re.compile(r"^\s*I:\s+(-?[\d.]+|-inf)\s+LUFS", re.M)
TALK = ("story", "steal_this", "confessional")  # the scenes the bed runs under, when present


def music_plan(tl: dict) -> list[dict]:
    sc = tl["scenes"]
    end = lambda s: sc[s]["start"] + sc[s]["dur"]  # noqa: E731
    talk_end = max(end(s) for s in TALK if s in sc)
    return [{"bed": "theme", "start": 0.0, "end": round(end("scoreboard") + XF_S, 3)},
            {"bed": "bed", "start": sc["story"]["start"], "end": round(min(tl["total"], talk_end + XF_S), 3)},
            {"bed": "theme", "start": sc["out"]["start"], "end": tl["total"]}]


def sfx_plan(tl: dict, kit: Path | None = None) -> list[dict]:
    """Cues by their locked meaning: the jingle at the scoreboard, the section break at the story, a pop for each story
    card and for the Steal-this card, the agent's own sting at the confessional (only when the kit holds one for it,
    and never on a day with no confessional), a pop for the subscribe button and the like sticker. Without a kit no
    sting is planned."""
    c, sc = tl["cues"], tl["scenes"]
    out = [{"t": sc["scoreboard"]["start"], "cue": "jingle"}, {"t": sc["story"]["start"], "cue": "section"}]
    out += [{"t": t, "cue": "pop"} for t in c["story_items"]]
    out.append({"t": c["steal_in"], "cue": "pop"})
    rel = entrant_rel(tl.get("confessional_harness"))
    if ("confessional" in sc and c.get("harness_in") is not None and rel is not None and kit is not None
            and (Path(kit) / rel).is_file()):
        out.append({"t": c["harness_in"], "cue": f"entrant_{tl['confessional_harness']}"})
    out += [{"t": c["subscribe"], "cue": "cta"}, {"t": c["like"], "cue": "cta"}]
    return out


def load(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar",
                          str(SR), "-"], check=True, capture_output=True, timeout=FFMPEG_TIMEOUT_S).stdout
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, 2).astype(np.float64)


def place(buf: np.ndarray, clip: np.ndarray, t: float) -> None:
    i = int(round(t * SR))
    j = min(len(buf), i + len(clip))
    if j > i:
        buf[i:j] += clip[: j - i]


def looped(bed: np.ndarray, n: int, gain: float) -> np.ndarray:
    reps = int(np.ceil(n / len(bed))) + 1
    x = np.tile(bed, (reps, 1))[:n] * gain
    f = min(int(XF_S * SR), n // 2)
    if f > 0:
        ramp = np.linspace(0, 1, f)[:, None]
        x[:f] *= ramp
        x[-f:] *= ramp[::-1]
    return x


def duck_gain(voice: np.ndarray) -> np.ndarray:
    """Music gain under speech: an envelope follower on 10 ms blocks (fast attack, slow release); music drops to
    about 35% under the voice."""
    n, b = len(voice), SR // 100
    nb = int(np.ceil(n / b))
    blk = np.zeros(nb * b)
    blk[:n] = np.abs(voice).max(axis=1)
    blk = blk.reshape(nb, b).max(axis=1)
    att, rel = np.exp(-1 / (0.02 * 100)), np.exp(-1 / (0.35 * 100))
    e, acc = np.zeros(nb), 0.0
    for k in range(nb):
        v = blk[k]
        acc = att * acc + (1 - att) * v if v > acc else rel * acc + (1 - rel) * v
        e[k] = acc
    return np.repeat(1.0 - 0.65 * np.clip(e / 0.05, 0, 1), b)[:n]


def guard_peak(mixed: np.ndarray, ceiling: float = 0.98) -> np.ndarray:
    """Scale the whole mix down when its peak passes the ceiling, so the pcm conversion never clips."""
    peak = np.abs(mixed).max()
    return mixed * (ceiling / peak) if peak > ceiling else mixed


def parse_lufs(text: str) -> float | None:
    vals = _LUFS.findall(text)
    if not vals:
        return None
    return float("-inf") if vals[-1] == "-inf" else float(vals[-1])


def integrated_lufs(path: Path) -> float | None:
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-f",
                        "null", "-"], capture_output=True, text=True, errors="replace", timeout=FFMPEG_TIMEOUT_S)
    return parse_lufs(r.stderr)


def loudnorm(pre: Path, out: Path, n: int) -> dict:
    """Two-pass loudnorm of pre into out (written beside it and renamed into place) at exactly n samples."""
    meas = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(pre), "-af",
                           f"loudnorm={TARGET}:print_format=json", "-f", "null", "-"],
                          check=True, capture_output=True, text=True, errors="replace",
                          timeout=FFMPEG_TIMEOUT_S).stderr  # a crash is an error, never "silent"
    try:
        j = json.loads(meas[meas.rindex("{"): meas.rindex("}") + 1])
        if not all(np.isfinite(float(j[k])) for k in ("input_i", "input_tp", "input_lra", "input_thresh",
                                                      "target_offset")):
            raise ValueError("silent mix: nothing to normalise")
    except (ValueError, KeyError):
        raise ValueError("silent or unmeasurable mix: loudnorm found no signal") from None
    af = (f"loudnorm={TARGET}:measured_I={j['input_i']}:measured_TP={j['input_tp']}:measured_LRA={j['input_lra']}:"
          f"measured_thresh={j['input_thresh']}:offset={j['target_offset']}:linear=true,aresample={SR},"
          f"aformat=channel_layouts=stereo,apad=whole_len={n},atrim=end_sample={n}")
    part = out.with_name(f".{out.stem}.part{out.suffix}")
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(pre), "-af", af, "-c:a", "pcm_s16le",
                        str(part)], check=True, timeout=FFMPEG_TIMEOUT_S)
        os.replace(part, out)
    finally:
        part.unlink(missing_ok=True)
    return j


def mixdown(tl: dict, voice_dir: Path, kit: Path, out: Path) -> dict:
    voice_dir, kit, out = Path(voice_dir), Path(kit), Path(out)
    out.unlink(missing_ok=True)  # a failed mix never leaves an older one behind
    n = int(round(tl["total"] * SR))
    plan, cues = music_plan(tl), sfx_plan(tl, kit)
    need = [kit / BEDS[s["bed"]][0] for s in plan] + [kit / cue_file(c["cue"])[0] for c in cues]
    need += [voice_dir / ln["file"] for ln in tl["lines"]]
    for p in need:  # fail before any work, with a plain error
        if not p.is_file():
            raise FileNotFoundError(f"mix input missing: {p.name}")
    voice = np.zeros((n, 2))
    for ln in tl["lines"]:
        place(voice, load(voice_dir / ln["file"]), ln["start"])
    cache: dict[str, np.ndarray] = {}

    def kit_clip(rel: str) -> np.ndarray:
        if rel not in cache:
            cache[rel] = load(kit / rel)
        return cache[rel]

    music = np.zeros((n, 2))
    for seg in plan:
        rel, gain = BEDS[seg["bed"]]
        k = int(round((seg["end"] - seg["start"]) * SR))
        if k > 0:
            place(music, looped(kit_clip(rel), k, gain), seg["start"])
    music *= duck_gain(voice)[:, None]
    sfx = np.zeros((n, 2))
    for c in cues:
        rel, g = cue_file(c["cue"])
        place(sfx, kit_clip(rel) * 10 ** (g / 20), c["t"])
    mixed = guard_peak(voice + music + sfx)
    out.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(mixed, -1, 1) * 32767).astype("<i2").tobytes()
    fd, pre_name = tempfile.mkstemp(dir=out.parent, prefix=f".{out.stem}-pre.", suffix=".wav")
    os.close(fd)
    pre = Path(pre_name)
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR), "-ac", "2", "-i",
                        "-", "-c:a", "pcm_s16le", str(pre)], input=pcm, check=True, timeout=FFMPEG_TIMEOUT_S)
        meas = loudnorm(pre, out, n)
    finally:
        pre.unlink(missing_ok=True)
    return {"total": tl["total"], "samples": n, "music": len(plan), "sfx": len(cues), "input_i": meas["input_i"]}
