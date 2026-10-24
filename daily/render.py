"""The render stage: compose, mix, HyperFrames check and render (optionally inside your sandbox command), master-mix
remux, the five render checks, a contact sheet, and render.json.

The stage follows the voice stage's read-once pattern: stale outputs and the build dir go first, the text verdict must
accept the script and ledger, the bytes read are hashed against that verdict and parsed, voices.json is read once and
must hash to what the voice stage recorded, and the voice outputs must be the ones voice.build_lines gives for that
script under those voices. So a script edited after the gate, or voices changed after voicing, is never rendered: the
episode is always the reviewed text in the recorded voices. Every failure reason is a short code, never exception or
log text.

Dry runs write episode-dry.mp4, render-dry.json and contact-sheet-dry.jpg and never touch the real artifacts, so a
dry render can never be published.

HyperFrames runs as settings.toml `render.hyperframes` (an argv prefix, default `hyperframes` on PATH) from the build's
comps folder, with no display variables and its telemetry and update checks off, in a session of its own that is
killed whole on a timeout. `render.sandbox`, when set, is an argv prefix put in front of it (a `{timeout}` argument is
replaced by the call's timeout in seconds): use it to run the headless browser with no network and in its own PID
namespace (docs/how-it-works.md has an example)."""
from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
from pathlib import Path

from daily import align, compose, downstream, files, gate, mix, paths, sheet, voice, voiceconf
from daily.settings import SettingsError
from daily.stage import StageResult

RENDER_CHECKS = gate.RENDER_REQUIRED  # single source: gate.py
MIN_COVERAGE = align.MIN_COVERAGE  # single source: align.py
DURATION = (60.0, 75.0)
LUFS = (-15.0, -13.0)
FORMAT = ("h264", 1080, 1920, "yuv420p", "30/1")
FRAME = 1 / 30
TOLERATED = ("timeline_track_too_dense", "composition_file_too_large")
WARN, FAIL = chr(0x26A0), chr(0x2717)
RENDER_LOG_BAD = re.compile(r"Unresolved font|PAGEERROR|net::ERR|Failed to load resource|Continuing render despite")
CHECK_TIMEOUT, RENDER_TIMEOUT, PROBE_TIMEOUT = 600, 3600, 120
REMUX_TIMEOUT = 600
KILL_GRACE_S = 60  # past the step's own timeout before the whole session is killed
# HyperFrames' own privacy and quiet switches, as its documentation names them; set for every run
HF_QUIET_ENV = {"HYPERFRAMES_NO_TELEMETRY": "1", "DO_NOT_TRACK": "1", "HYPERFRAMES_NO_UPDATE_CHECK": "1",
                "HYPERFRAMES_SKIP_SKILLS": "1", "CI": "1"}
_ID = re.compile(r"[a-z0-9_]{1,32}\Z")


class ProbeError(Exception):
    """ffprobe or ffmpeg could not read the file; the message is a short code."""


PROBE_CODES = frozenset({"probe_timeout", "probe_failed", "probe_unreadable"})


def probe_failure_code(e: BaseException) -> str:
    """`render_probe:<code>` for a ProbeError whose message is one of PROBE_CODES, else `render_probe:<class name>`."""
    if isinstance(e, ProbeError) and len(e.args) == 1 and e.args[0] in PROBE_CODES:
        return f"render_probe:{e.args[0]}"
    return f"render_probe:{type(e).__name__}"


def check_log_problems(text: str) -> list[str]:
    """Every warning or error line of `hyperframes check` except the two advisory code-organization lints."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith((WARN, FAIL)) and not any(t in s for t in TOLERATED):
            out.append(s[:240])
    return out


def render_log_problems(text: str) -> list[str]:
    return [ln.strip()[:240] for ln in text.splitlines()
            if RENDER_LOG_BAD.search(ln) and "despite lint issues" not in ln]


def _ffprobe_json(path: Path, *args: str) -> dict:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", *args, "-of", "json", str(path)], check=True,
                             capture_output=True, text=True, timeout=PROBE_TIMEOUT).stdout
        return json.loads(out)
    except subprocess.TimeoutExpired:
        raise ProbeError("probe_timeout") from None
    except subprocess.CalledProcessError:
        raise ProbeError("probe_failed") from None
    except (OSError, ValueError):
        raise ProbeError("probe_unreadable") from None


def probe(path: Path) -> dict:
    """The facts the checks read: video format, audio presence, duration and integrated loudness. Raises ProbeError
    (a short code) when the file cannot be read."""
    try:
        v = _ffprobe_json(path, "-select_streams", "v:0", "-show_entries",
                          "stream=codec_name,width,height,pix_fmt,r_frame_rate")["streams"][0]
        a = _ffprobe_json(path, "-select_streams", "a:0", "-show_entries", "stream=codec_type").get("streams", [])
        d = float(_ffprobe_json(path, "-show_entries", "format=duration")["format"]["duration"])
        fmt = {"vcodec": v["codec_name"], "width": int(v["width"]), "height": int(v["height"]),
               "pix_fmt": v["pix_fmt"], "fps": v["r_frame_rate"]}
    except (KeyError, IndexError, TypeError, ValueError):
        raise ProbeError("probe_unreadable") from None
    try:
        lufs = mix.integrated_lufs(path)
    except subprocess.TimeoutExpired:
        raise ProbeError("probe_timeout") from None
    except OSError:
        raise ProbeError("probe_failed") from None
    return {**fmt, "has_audio": bool(a), "duration": d, "lufs": lufs}


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _name(line_id) -> str:
    return line_id if isinstance(line_id, str) and _ID.match(line_id) else "?"


def captions_match(tl: dict, lines: list[dict], coverages: dict) -> tuple[bool, str]:
    """Every captioned line (in lines.json order) shows exactly the tokens align gives its text, once and in order,
    and Whisper heard at least MIN_COVERAGE of every line, captioned or not. A line with no caption groups or no
    coverage fails. Evidence holds counts, coverages and line ids only."""
    bad, low, unheard, n_want, n_got, seen = [], [], [], 0, 0, []
    groups = tl.get("caption_groups", [])
    for ln in lines:
        lid = ln["id"]
        cov = coverages.get(lid)
        if not _num(cov):
            unheard.append(_name(lid))
        else:
            seen.append(cov)
            if cov < MIN_COVERAGE:
                low.append(f"{_name(lid)} {cov:.2f}")
        if lid in compose.NO_CAPTIONS:
            continue
        want = align.match_tokens(ln["text"], [])[0]
        got = [w for g in groups if g["line"] == lid for w in g["words"]]
        n_want, n_got = n_want + len(want), n_got + len(got)
        if got != want:
            bad.append(_name(lid))
    ev = f"{n_got} of {n_want} words captioned; min coverage {min(seen, default=0.0):.2f}"
    if bad:
        ev += f"; mismatched {','.join(bad)}"
    if low:
        ev += f"; low coverage {', '.join(low)}"
    if unheard:
        ev += f"; no coverage {','.join(unheard)}"
    return (not bad and not low and not unheard), ev


def render_checks(m: dict, total: float, cap: tuple[bool, str]) -> list[dict]:
    lo, hi = DURATION
    d, lufs = m.get("duration"), m.get("lufs")
    fmt = tuple(m.get(k) for k in ("vcodec", "width", "height", "pix_fmt", "fps"))
    dur_ok = _num(d) and _num(total) and lo <= d <= hi and abs(d - total) <= FRAME + 1e-6
    return [
        {"name": "duration", "pass": dur_ok,
         "evidence": f"{d} s (timeline {total} s; allowed {lo:.0f} to {hi:.0f} s)"},
        {"name": "format", "pass": fmt == FORMAT, "evidence": ",".join(str(x) for x in fmt)},
        {"name": "loudness", "pass": _num(lufs) and LUFS[0] <= lufs <= LUFS[1],
         "evidence": f"{lufs} LUFS integrated (allowed {LUFS[0]} to {LUFS[1]})"},
        {"name": "audio_present", "pass": m.get("has_audio") is True,
         "evidence": "audio stream present" if m.get("has_audio") is True else "no audio stream"},
        {"name": "captions_match", "pass": cap[0] is True, "evidence": cap[1]},
    ]


def write_verdict(path: Path, day: str, subject: Path, checks: list[dict], dry: bool, *, script_sha256: str,
                  ledger_sha256: str, voices_sha256: str | None = None, ref_sha256: str | None = None,
                  rejected: list[str] | None = None) -> dict:
    """The render verdict, written atomically. It vouches for the subject's bytes and records the hashes of the text
    verdict's script and ledger the episode was rendered from. Passing needs every name in RENDER_CHECKS exactly
    once, each passing. It also records the sha256 of the voices file the render used and the digest of the host
    reference the voice was synthesized from (null for a dry run, which has no reference), so a later edit to
    voices.json is visible and the clone's consent provenance travels with the episode."""
    names = [c["name"] for c in checks]
    v = {"schema": gate.VERDICT_SCHEMA, "day": day, "stage": "render",
         "pass": names == list(RENDER_CHECKS) and all(c["pass"] is True for c in checks),
         "subject_sha256": files.sha256_bytes(Path(subject).read_bytes()), "script_sha256": script_sha256,
         "ledger_sha256": ledger_sha256, "voices_sha256": voices_sha256, "ref_sha256": ref_sha256, "checks": checks, "dry": dry, "at": files.now_iso()}
    if rejected:
        v["rejected"] = list(rejected)  # the renamed files a failed render left for inspection
    files.write_json(path, v)
    return v


def hf_env() -> dict:
    """No display variables (a dead or unreachable display can hang headless Chromium, and the render needs none), and
    HyperFrames' telemetry, update check and skill install switched off."""
    env = dict(os.environ)
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    env.update(HF_QUIET_ENV)
    return env


def hf_argv(args: list[str], timeout: int, command=("hyperframes",), sandbox=()) -> list[str]:
    """The argv of one HyperFrames call: the sandbox prefix (each `{timeout}` replaced by the timeout in seconds), the
    HyperFrames command, then the arguments. A list, never a shell string: no value is ever parsed as syntax."""
    return [*(a.replace("{timeout}", str(int(timeout))) for a in sandbox), *command, *args]


def hf(args: list[str], comps: Path, timeout: int, log: Path, *, command=("hyperframes",), sandbox=()) -> int:
    """Run `<sandbox> <hyperframes> <args>` from comps, its output streaming into log, and return its exit code. It runs
    in a session of its own: when it outlasts timeout plus KILL_GRACE_S the whole process group is killed (a browser
    that left the group is what a PID-namespace sandbox is for) and TimeoutExpired is raised."""
    argv = hf_argv(args, timeout, command, sandbox)
    with open(log, "w", encoding="utf-8") as fh:
        proc = subprocess.Popen(argv, cwd=str(comps), stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=hf_env(), start_new_session=True)
        try:
            return proc.wait(timeout=timeout + KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
            raise
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
            raise


def remux(raw: Path, mix_wav: Path, out: Path) -> None:
    """Deliver the master mix: a renderer's own audio pass once cut an episode by 3.3 dB, so
    the video stream is copied untouched and the mix goes in as AAC 192k 48 kHz. ffmpeg writes a temp file that
    replaces `out` only when it has finished; a failure removes the temp and leaves `out` as it was."""
    out = Path(out)
    tmp = out.with_name(f".{out.name}.part.mp4")
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(raw), "-i", str(mix_wav), "-map", "0:v:0",
                        "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags",
                        "+faststart", str(tmp)], check=True, timeout=REMUX_TIMEOUT)
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)


def contact_sheet(video: Path, tl: dict, out: Path, font=None) -> Path:
    """A frame every 2 s plus frames around each scene boundary (daily.sheet), vertical tiles, six across. A long render
    makes a second sheet beside the first (contact-sheet-2.jpg)."""
    bounds = [v["start"] for k, v in tl["scenes"].items() if k != "cold_open"]
    times = sheet.strip_times(tl["total"], boundaries=bounds, cues=(), every=2.0)
    made = sheet.contact_sheets(video, times, out, cols=6, per_sheet=60, tile=sheet.TILE, font=font)
    made[0].replace(out)
    return out


def read_voice_manifest(work: Path) -> dict | None:
    """The voice stage's record of what it used (voice.MANIFEST), or None when it is missing or not a manifest."""
    try:
        m = files.read_json(work / "voice" / voice.MANIFEST)
    except (OSError, ValueError):
        return None
    ok = (isinstance(m, dict) and m.get("schema") == voice.MANIFEST_SCHEMA and isinstance(m.get("voices_sha256"), str)
          and (m.get("ref_sha256") is None or isinstance(m.get("ref_sha256"), str)) and isinstance(m.get("lines"), list))
    return m if ok else None


def _voice_matches(work: Path, script: dict, ledger: dict, manifest: dict, voices: dict) -> list[dict] | None:
    """lines.json when its ids, voices and texts are exactly voice.build_lines of the accepted script (under the voices
    config the manifest names) and its params are the ones the manifest recorded, else None."""
    try:
        lines = files.read_json(work / "voice" / "lines.json")
        want = voice.build_lines(script, ledger, voices)
        key = lambda rows: [(r["id"], r["voice"], r["text"]) for r in rows]  # noqa: E731
        recorded = [(r["id"], r["voice"], r["params"]) for r in manifest["lines"]]
        if key(lines) != key(want) or recorded != [(r["id"], r["voice"], r["params"]) for r in lines]:
            return None
        return lines
    except Exception:  # noqa: BLE001 - any failure to prove the match is a mismatch
        return None


def _problem_log(path: Path, problems: list[str]) -> None:
    files.write_text(path, "\n".join(problems) + "\n")  # the reason never holds log text; the log dir does


def run_stage(ctx) -> StageResult:
    """The render stage. Every failure reason is a short code: stale_cleanup, settings_missing, missing:render.kit,
    gate_not_accepted, no_voice_lines, no_voice_manifest, voice_config_changed, voice_mismatch, compose:<class>,
    mix:<class>, hf_check_failed, hf_check, hf_render_failed, hf_render, hf_timeout, hf:<class>,
    render_probe:<code or class>, render_checks:<check names>."""
    work = Path(ctx.work)
    sfx = "-dry" if ctx.dry else ""
    try:
        downstream.clear_stale(work, sfx)
    except OSError:
        return StageResult(ok=False, reason="stale_cleanup")
    settings = getattr(ctx, "settings", None)
    if settings is None:
        return StageResult(ok=False, reason="settings_missing")
    try:
        kit = Path(settings.require("render_kit"))
    except SettingsError as e:
        return StageResult(ok=False, reason=str(e))
    script_bytes, ledger_bytes, _why = gate.read_verified(work / "verdict.json", work / "script.json",
                                                          work / "ledger.json", gate.REQUIRED)
    if script_bytes is None:  # one read, every check made on the bytes used: any refusal is gate_not_accepted
        return StageResult(ok=False, reason="gate_not_accepted")
    try:
        script, ledger = json.loads(script_bytes.decode("utf-8")), json.loads(ledger_bytes.decode("utf-8"))
    except ValueError:
        return StageResult(ok=False, reason="gate_not_accepted")  # hash-bound bytes that are not JSON
    if not (work / "voice" / "lines.json").exists():
        return StageResult(ok=False, reason="no_voice_lines")
    manifest = read_voice_manifest(work)
    if manifest is None:
        return StageResult(ok=False, reason="no_voice_manifest")
    try:
        voices_bytes = paths.voices_path().read_bytes()  # the one read: hashed, checked, parsed and composed from
    except OSError:
        voices_bytes = None
    if voices_bytes is None or files.sha256_bytes(voices_bytes) != manifest["voices_sha256"]:
        return StageResult(ok=False, reason="voice_config_changed")  # voices.json is not the one the lines were voiced with
    try:
        voices = voiceconf.parse_voices_bytes(voices_bytes)
    except voiceconf.VoiceConfigError:
        return StageResult(ok=False, reason="voice_config_changed")
    lines = _voice_matches(work, script, ledger, manifest, voices)
    if lines is None:
        return StageResult(ok=False, reason="voice_mismatch")
    try:
        tl = compose.build(work, ctx.day, script, ledger, dry=ctx.dry, kit=kit, voices=voices, series=settings.series,
                           handle=settings.handle)
    except Exception as e:  # noqa: BLE001 - a short class-name code, never the message
        return StageResult(ok=False, reason=f"compose:{type(e).__name__}")
    comps = work / "build" / "comps"
    comp = comps / compose.COMP
    try:
        mix.mixdown(tl, work / "voice", kit, comp / "assets" / "mix.wav")
    except Exception as e:  # noqa: BLE001
        return StageResult(ok=False, reason=f"mix:{type(e).__name__}")
    logs = work / "build" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    raw = work / "build" / f"episode-raw{sfx}.mp4"
    run_hf = dict(command=settings.render_command, sandbox=settings.render_sandbox)
    try:
        if hf(["check", compose.COMP], comps, CHECK_TIMEOUT, logs / "check.log", **run_hf) != 0:
            return StageResult(ok=False, reason="hf_check_failed")
        probs = check_log_problems((logs / "check.log").read_text(encoding="utf-8", errors="replace"))
        if probs:
            _problem_log(logs / "check-problems.txt", probs)
            return StageResult(ok=False, reason="hf_check")
        if hf(["render", compose.COMP, "-o", str(raw), "--no-best-effort", "--no-browser-gpu", "-w", "2", "-q",
               "standard"], comps, RENDER_TIMEOUT, logs / "render.log", **run_hf) != 0:
            return StageResult(ok=False, reason="hf_render_failed")
    except subprocess.TimeoutExpired:
        return StageResult(ok=False, reason="hf_timeout")
    except OSError:
        return StageResult(ok=False, reason="hf_check_failed")
    except Exception as e:  # noqa: BLE001
        return StageResult(ok=False, reason=f"hf:{type(e).__name__}")
    if not raw.is_file():  # a zero exit that wrote nothing must never let an older raw file through
        return StageResult(ok=False, reason="hf_render_failed")
    probs = render_log_problems((logs / "render.log").read_text(encoding="utf-8", errors="replace"))
    if probs:
        _problem_log(logs / "render-problems.txt", probs)
        return StageResult(ok=False, reason="hf_render")
    final = work / f"episode{sfx}.mp4"
    try:
        remux(raw, comp / "assets" / "mix.wav", final)
        m = probe(final)
        covs = {ln["id"]: files.read_json(work / "voice" / f"{ln['id']}.words.json")["coverage"] for ln in lines}
        checks = render_checks(m, tl["total"], captions_match(tl, lines, covs))
        contact_sheet(final, tl, work / f"contact-sheet{sfx}.jpg", font=kit / "fonts" / compose.FONTS[1])
        keep = dict(script_sha256=files.sha256_bytes(script_bytes), ledger_sha256=files.sha256_bytes(ledger_bytes),
                    voices_sha256=files.sha256_bytes(voices_bytes), ref_sha256=manifest["ref_sha256"])
        if all(c["pass"] is True for c in checks):
            write_verdict(work / f"render{sfx}.json", ctx.day.isoformat(), final, checks, ctx.dry, **keep)
        else:
            moved = downstream.reject(work, sfx)
            write_verdict(work / f"render{sfx}.json", ctx.day.isoformat(), work / f"episode-rejected{sfx}.mp4",
                          checks, ctx.dry, rejected=moved, **keep)
    except Exception as e:  # noqa: BLE001
        try:
            downstream.reject(work, sfx)
        except OSError:
            pass
        return StageResult(ok=False, reason=probe_failure_code(e))
    bad = [c["name"] for c in checks if not c["pass"]]
    if bad:
        return StageResult(ok=False, reason="render_checks:" + ",".join(bad))
    return StageResult(ok=True, reason=f"{m['duration']:.2f} s, {m['lufs']} LUFS" + (" (dry)" if ctx.dry else ""))
