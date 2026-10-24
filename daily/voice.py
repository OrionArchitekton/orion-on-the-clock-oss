"""The voice stage: the host's voice says the host lines, the confessional plays in its agent's voice, and every line
gets word times for the captions.

Order: the stage first removes its own outputs from an earlier run (voice/ and hostlint.json), then the text gate's
verdict must accept the current script and ledger (gate.read_verified: one read, the bytes parsed below are the bytes
the verdict's hashes name); voices.json is read once and hashed; the lines are built (the confessional's words come
from the ledger by quote_id, never from the writer); the host lines are linted (advisory, hostlint.json); the host
reference must match its hash; Chatterbox voices every line in one run, which the runner's results file must prove
(its line ids, the reference digest and a wav per line); each line with an ffmpeg chain gets it, keeping the untouched
take as <id>.raw.wav; Whisper listens to that clean take, and its word times are mapped onto each line's own words,
timed by the final wav. A line Whisper heard less of than align.MIN_COVERAGE fails the stage. With ctx.dry, Chatterbox
and Whisper are skipped and each line is silence at 0.3 s a word, so compose and render run without a GPU.

Where voicing runs (settings.toml `tts.target`): "local" runs the runner on this machine (`tts.python`); "remote" runs
it on `tts.host` over ssh (daily.tts_remote) and fails when that cannot; "auto" (the default) tries the remote host
first when one is configured and falls back to the local runner exactly once.

A reason is a short code, never an exception's text: that text can hold a quote id, an agent string or a path.

This module takes no lock. The runner (tts_runner.py) owns the shared TTS lock and holds it for the whole synthesis,
so a second lock around it here would make the runner wait out its full timeout and fail."""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
from pathlib import Path

from daily import align, compose, downstream, files, gate, paths, tts_remote, tts_runner, voicelint, voiceconf
from daily.settings import SettingsError
from daily.stage import StageResult

TEXT_CHECKS = gate.REQUIRED  # single source: gate.py
TTS_RUNNER = Path(__file__).resolve().parent / "tts_runner.py"
# The runner may wait tts_runner.LOCK_WAIT_S (1800 s) for the shared lock, then loads the model (~3 min) and
# synthesizes. Measured on one CPU (a GPU that is too full sends Chatterbox there): ~29.7 s of compute per spoken word.
# At the budget maximum (gate.WORDS_MAX, 210 words) that is ~6300 s; with ~180 s of model load and up to 1800 s of lock
# wait, ~8300 s. 10800 s leaves margin over that (a test pins TTS_TIMEOUT_S >= LOCK_WAIT_S + 210 * 30 + 300).
TTS_TIMEOUT_S = 10800
FFMPEG_TIMEOUT_S = 120
# An agent chain pairs its pitch step (asetrate, which speeds the line up by its factor) with a tempo step (atempo), so
# its take is raw / chain_speed(chain) long, plus the echo tail: aecho lets its echo ring out after the line, so the take
# ends up longer by about the echo's delay (measured with ffmpeg: 14 ms, 60 ms and 120 ms echoes added 14, 60 and
# 118 ms). RAW_TOLERANCE_S is the margin on top of that. A chain that lost its atempo is off by far more and is refused.
RAW_TOLERANCE_S = 0.05
TTS_RESULTS = "tts-results.json"
TTS_TARGETS = ("remote", "local")  # the order "auto" tries them in
# what the stage used: voices.json's hash, the host reference digest, each line's params and post chain
MANIFEST = "voice-manifest.json"
MANIFEST_SCHEMA = "daily.voice-manifest/1"
SR = 24000
DRY_WORD_S = 0.3  # a cloned voice speaks ~3.3 words/s, so a dry episode is about as long as a real one
DRY_MIN_S = 0.6
QUOTE_KINDS = ("transcript", "pr")  # the render labels a quote by its kind; any other kind has no label
_AGENT = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


class VoiceError(RuntimeError):
    pass


# The only VoiceError messages the real path raises after lines are built: a fixed token set, so a reason can carry the
# code and never free text. Any other message (or any other exception) reports as its class name.
VOICE_CODES = frozenset({"tts_runner_failed", "tts_results_unreadable", "tts_results_ids", "tts_results_ref",
                         "tts_wav_missing", "raw_length", "chain_speed", "voice_config_changed",
                         "tts_remote_unconfigured", *[f"tts_remote_{c}" for c in tts_remote.CODES]})


def failure_code(e: BaseException) -> str:
    """`voice:<code>` for a VoiceError whose message is one of VOICE_CODES, else `voice:<class name>`."""
    if isinstance(e, VoiceError) and len(e.args) == 1 and e.args[0] in VOICE_CODES:
        return f"voice:{e.args[0]}"
    return f"voice:{type(e).__name__}"


def _confessional(ledger: dict, quote_id: str, voices: dict) -> dict:
    """The quote's own line: its words come from the ledger (the gate checked them against the source), its voice is
    its agent's, and it carries the provenance the render labels it with. Raises LookupError for an id that is not
    in the ledger and VoiceConfigError for an agent with no active voice."""
    q = compose.quote_by_id(ledger, quote_id)
    voice = voiceconf.voice_for(q["harness"], voices)
    if q.get("kind") not in QUOTE_KINDS:
        raise VoiceError("quote_kind")
    text = q["text"].strip()
    if not text:
        raise VoiceError("empty lines: confessional")
    return {"id": "confessional", "scene": "confessional", "voice": q["harness"], "text": text,
            "params": dict(voice["params"]), "quote_id": q["id"], "quote_ts": q["ts"], "quote_kind": q["kind"]}


def build_lines(script: dict, ledger: dict, voices: dict) -> list[dict]:
    """Every line in speaking order: [{"id", "scene", "voice", "text", "params"}]. The host lines are the lint's
    (voicelint.host_lines), so their ids and order are the lint's. The confessional line (after confessional_intro,
    carrying quote_id, quote_ts and quote_kind as well) exists only when the script has a quote; a quote without an
    introduction, or an introduction without a quote, is an error, never a skipped beat."""
    host = voices[voiceconf.HOST]
    conf = script["confessional"]
    has_quote, has_intro = bool(conf["quote_id"].strip()), bool(conf["host_intro"].strip())
    if has_quote != has_intro:
        raise VoiceError("confessional_mismatch")
    if not script["story"]["host"]:
        raise VoiceError("empty lines: story")
    spoken = voicelint.host_lines(script)  # leaves out an empty line, so a missing id below is an empty line
    expected = ["cold_open", "scoreboard", *[f"story_{i}" for i in range(len(script["story"]["host"]))],
                "steal_this", *(["confessional_intro"] if has_intro else []), "out"]
    empty = [i for i in expected if i not in {line_id for line_id, _ in spoken}]
    if empty:
        raise VoiceError(f"empty lines: {', '.join(empty)}")
    quote_line = _confessional(ledger, conf["quote_id"], voices) if has_quote else None
    lines = []
    for line_id, text in spoken:
        params = host["params_cold_open"] if line_id == "cold_open" else host["params"]
        scene = compose.scene_of(line_id)  # the one scene list: compose.SCENES
        lines.append({"id": line_id, "scene": scene, "voice": voiceconf.HOST, "text": text.strip(),
                      "params": dict(params)})
        if line_id == "confessional_intro":
            lines.append(quote_line)
    return lines


def unvoiced_agents(ledger: dict, voices: dict) -> list:
    """The agents in the ledger's coverage that voices.json has no entry for (the scoreboard could not label them),
    as safe ids ("?" for anything that is not an agent id)."""
    coverage = ledger.get("coverage") if isinstance(ledger, dict) else None
    if not isinstance(coverage, dict):
        return []
    return [h if isinstance(h, str) and _AGENT.match(h) else "?" for h in coverage
            if h == voiceconf.HOST or h not in voices]


def silent_wav(path: Path, seconds: float) -> None:
    """Mono silence at SR. ffmpeg writes a temp file beside the target and the target is replaced only when ffmpeg has
    finished, so a failed or killed run never leaves a truncated wav at the final path (and leaves an earlier one be)."""
    path = Path(path)
    tmp = path.with_name(f".{path.stem}.part.wav")
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"anullsrc=r={SR}:cl=mono",
                        "-t", f"{seconds:.3f}", str(tmp)], check=True, timeout=FFMPEG_TIMEOUT_S)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def wav_seconds(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         check=True, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S).stdout
    return float(out.strip())


def write_words(voice_dir: Path, line: dict, dur: float, coverage: float, source: str, words: list[dict]) -> None:
    rec = {"id": line["id"], "voice": line["voice"], "text": line["text"], "dur": dur, "coverage": coverage,
           "source": source, "words": words}
    files.write_json(Path(voice_dir) / f"{line['id']}.words.json", rec)


def dry_voice(lines: list[dict], voice_dir: Path) -> None:
    """Silence of 0.3 s a word (at least 0.6 s) and evenly spaced word times, in the shape align.align_words returns."""
    voice_dir = Path(voice_dir)
    for ln in lines:
        toks = ln["text"].split()
        dur = round(max(DRY_MIN_S, DRY_WORD_S * len(toks)), 3)
        silent_wav(voice_dir / f"{ln['id']}.wav", dur)
        words = [{"word": w, "start": round(i * DRY_WORD_S, 3), "end": round((i + 1) * DRY_WORD_S, 3)}
                 for i, w in enumerate(toks)]
        write_words(voice_dir, ln, dur, 1.0, "dry", words)


def _runner_ok(log: Path) -> bool:
    return tts_remote.runner_ok(log)


def check_results(lines: list[dict], voice_dir: Path, ref_digest: str) -> dict:
    """The evidence checks, the same for either target: the results file must list exactly these line ids in order,
    carry `ref_digest`, and every <id>.wav must exist and hold bytes. Raises VoiceError with a short code."""
    try:
        results = files.read_json(Path(voice_dir) / TTS_RESULTS)
    except (OSError, ValueError):
        raise VoiceError("tts_results_unreadable") from None
    rows = results.get("lines") if isinstance(results, dict) else None
    ids = [row.get("id") if isinstance(row, dict) else None for row in rows] if isinstance(rows, list) else None
    if ids != [ln["id"] for ln in lines]:
        raise VoiceError("tts_results_ids")
    got = results.get("ref_sha256")
    if not (isinstance(ref_digest, str) and ref_digest and isinstance(got, str) and got == ref_digest):
        raise VoiceError("tts_results_ref")
    for ln in lines:
        wav = Path(voice_dir) / f"{ln['id']}.wav"
        if not wav.is_file() or wav.stat().st_size == 0:
            raise VoiceError("tts_wav_missing")
    return results


def _run_local(lines: list[dict], voice_dir: Path, voices_path: Path, ref_digest: str, settings) -> dict:
    """Run the Chatterbox runner here, once, for every line, with settings.toml `tts.python`. It takes no lock (the
    runner holds the shared one itself; `tts.lock` names its file). The runner's exit code is not the proof: see
    check_results."""
    voice_dir = Path(voice_dir)
    python = settings.require("tts_python")
    env = dict(os.environ, PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True")
    if settings.tts_lock is not None:
        env[tts_runner.LOCK_ENV] = str(settings.tts_lock)
    log = voice_dir / "tts.log"
    # The runner's stdout and stderr go straight to tts.log as it runs, so a timeout or a kill still leaves what it
    # printed (one progress line per voiced line) and nothing is held in memory until it exits.
    with open(log, "w", encoding="utf-8", errors="replace") as fh:
        r = subprocess.run([str(python), str(TTS_RUNNER), str(voice_dir / "lines.json"), str(voice_dir),
                            str(voices_path)], stdout=fh, stderr=subprocess.STDOUT, env=env, timeout=TTS_TIMEOUT_S,
                           stdin=subprocess.DEVNULL)
    if r.returncode != 0 or not _runner_ok(log):
        raise VoiceError("tts_runner_failed")
    return check_results(lines, voice_dir, ref_digest)


def _targets(settings) -> tuple[str, ...]:
    pick = getattr(settings, "tts_target", "auto")
    if pick in TTS_TARGETS:
        return (pick,)
    return TTS_TARGETS if getattr(settings, "tts_host", None) else ("local",)


def run_tts(lines: list[dict], voice_dir: Path, voices_path: Path, ref_digest: str, *, settings,
            voices_sha: str | None = None, day=None) -> dict:
    """Voice every line once and return the results file's dict, which also records `tts_target` ("remote" or
    "local"), `device` (as the runner reported it) and, when the remote was tried and the local runner took over,
    `remote_skip` (a short code from tts_remote.CODES). Targets follow settings.toml `tts.target`: in "auto" any remote
    failure falls back to the local runner exactly once and is never retried; a forced "remote" raises VoiceError
    `tts_remote_<code>` instead. Either way the results must pass check_results (the line ids in order, `ref_digest`,
    a non-empty wav per line), else VoiceError with a short code (the runner's output is in tts.log, in the work
    dir)."""
    voice_dir = Path(voice_dir)
    voices_bytes = Path(voices_path).read_bytes()  # read once: the remote gets these bytes, and they must be the hashed ones
    if voices_sha is not None and files.sha256_bytes(voices_bytes) != voices_sha:
        raise VoiceError("voice_config_changed")
    targets = _targets(settings)
    forced = len(targets) == 1
    remote = tts_remote.Remote.from_settings(settings)
    skip = None
    for target in targets:
        if target == "remote":
            if remote is None:
                raise VoiceError("tts_remote_unconfigured")
            try:
                results = tts_remote.run(remote, lines, voice_dir, voices_bytes, day if day is not None else "00000000",
                                         lambda ls, vd: check_results(ls, vd, ref_digest))
            except tts_remote.RemoteSkip as e:
                skip = e.args[0]
                log = voice_dir / "tts.log"
                try:
                    if e.log_opened and log.is_file():
                        os.replace(log, voice_dir / "tts-remote.log")
                except OSError:
                    pass
                if skip != "unreachable":  # ssh works otherwise, so a stale run folder can be pruned
                    tts_remote.prune_after(remote, voice_dir, "tts-remote.log")
                if forced:
                    raise VoiceError(f"tts_remote_{skip}") from None
                continue
            tts_remote.prune_after(remote, voice_dir, "tts.log")
        else:
            results = _run_local(lines, voice_dir, voices_path, ref_digest, settings)
        results.pop("tts_target", None)  # the recorded values are this function's own, never a results file's
        results.pop("remote_skip", None)
        results["tts_target"] = target
        if target == "local" and skip:
            results["remote_skip"] = skip
        files.write_json(voice_dir / TTS_RESULTS, results)
        return results
    raise VoiceError("tts_runner_failed")


def post_process(wav: Path, chain: str) -> None:
    """Apply a voice's ffmpeg chain to <id>.wav; the untouched take stays as <id>.raw.wav. An empty chain does nothing.
    The chain is checked against the allowlist first (it is also checked when voices.json loads) and goes to ffmpeg as
    one argv element. ffmpeg writes a temp file beside the wav and the wav is replaced only once it has finished, so a
    failed or killed run leaves the take as it was, with no raw copy and no temp."""
    voiceconf.check_post_chain(chain, allow_empty=True)
    if not chain:
        return
    wav = Path(wav)
    raw = wav.with_name(wav.stem + ".raw.wav")
    part = wav.with_name(f".{wav.stem}.part.wav")
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(wav), "-af", chain, "-ar", str(SR),
                        "-ac", "1", str(part)], check=True, timeout=FFMPEG_TIMEOUT_S)
        os.replace(wav, raw)
        try:
            os.replace(part, wav)
        except BaseException:
            os.replace(raw, wav)  # put the untouched take back
            raise
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def echo_tail_s(chain: str) -> float:
    """How much longer than the raw take a chain's take may end up: the sum of its aecho delays (aecho=in:out:delay:decay,
    delay in ms; the chain allowlist has no "|", so one delay each). A delay that cannot be read counts as none."""
    tail = 0.0
    for part in chain.split(","):
        name, _, args = part.partition("=")
        if name == "aecho":
            try:
                tail += float(args.split(":")[2]) / 1000
            except (IndexError, ValueError):
                pass
    return tail


def chain_speed(chain: str) -> float:
    """The speed a chain plays a line at: the asetrate factor (asetrate=24000*F) times every atempo value. A step whose
    number cannot be read counts as 1, so a damaged chain shows up as a length that does not match."""
    speed = 1.0
    for part in chain.split(","):
        name, _, args = part.partition("=")
        try:
            if name == "asetrate":
                speed *= float(args.split("*")[1])
            elif name == "atempo":
                speed *= float(args)
        except (IndexError, ValueError):
            pass
    return speed


def raw_to_final(timed, speed: float):
    """Whisper's word times on a raw take, moved onto the final take a chain made from it: every numeric start and end
    divided by the chain's speed (a 0.5 chain doubles them, a 0.93 chain stretches them by about 7.5%). Anything that
    is not a list of word records is returned as it is, for align_words to judge; a value that is not a number is
    left alone."""
    if not isinstance(timed, list):
        return timed
    out = []
    for t in timed:
        if isinstance(t, dict):
            t = {k: (v / speed if k in ("start", "end") and isinstance(v, (int, float)) and not isinstance(v, bool)
                     else v) for k, v in t.items()}
        out.append(t)
    return out


def align_lines(lines: list[dict], voice_dir: Path, chains: dict | None = None, *, python=None,
                model: str | None = None) -> list[tuple[str, float]]:
    """Word times for every line, written as <id>.words.json; returns (line id, coverage) in line order. `chains` maps
    the id of each post-processed line to its chain. An agent chain (acrusher, flanger, echo) can hurt recognition, so
    such a line is heard on its clean <id>.raw.wav while its length comes from the final <id>.wav. The two must agree,
    else VoiceError: the final take may be at most RAW_TOLERANCE_S shorter than the raw one's length at the chain's
    speed (raw / chain_speed) and at most the chain's echo tail (echo_tail_s) plus RAW_TOLERANCE_S longer; any other
    change means the chain moved the words the captions are timed by. Whisper times such a line on the raw take, so its
    word times are divided by the chain's speed (raw_to_final) before they are mapped onto the line: the captions follow
    the final take. A chain whose speed is not a positive number is VoiceError("chain_speed"). Every other line is heard
    on its own wav. `python` and `model` are settings.toml `whisper.python` and `whisper.model`."""
    voice_dir = Path(voice_dir)
    jobs, durs, speeds = [], {}, {}
    for ln in lines:
        final = voice_dir / f"{ln['id']}.wav"
        seconds = wav_seconds(final)
        durs[ln["id"]] = round(seconds, 3)
        heard = final
        chain = (chains or {}).get(ln["id"])
        if chain:
            heard = voice_dir / f"{ln['id']}.raw.wav"
            speeds[ln["id"]] = speed = chain_speed(chain)
            if not (math.isfinite(speed) and speed > 0):
                raise VoiceError("chain_speed")
            skew = seconds - wav_seconds(heard) / speed  # off from the length the chain should give
            if not -RAW_TOLERANCE_S <= skew <= echo_tail_s(chain) + RAW_TOLERANCE_S:
                raise VoiceError("raw_length")
        jobs.append({"audio": str(heard), "text": ln["text"], "out": str(voice_dir / f"{ln['id']}.heard.json")})
    align.run_whisper(jobs, voice_dir / "whisper-manifest.json", python, model)
    covs = []
    for ln in lines:
        timed = files.read_json(voice_dir / f"{ln['id']}.heard.json")
        if ln["id"] in speeds:  # Whisper timed the raw take; the chain plays it at its speed, so a raw second is 1/speed
            timed = raw_to_final(timed, speeds[ln["id"]])
        words, cov = align.align_words(ln["text"], timed, durs[ln["id"]])
        write_words(voice_dir, ln, durs[ln["id"]], cov, "whisper", words)
        covs.append((ln["id"], cov))
    return covs


def clear_outputs(work: Path) -> None:
    """Remove the stage's outputs (voice/ and hostlint.json); collect does the same through daily.downstream when a
    new ledger replaces the old."""
    _clear_stale(work)


def _clear_stale(work: Path) -> None:
    """Remove the stage's outputs from an earlier run. A link is unlinked, never followed."""
    downstream.clear_voice_outputs(work)


def _summary(head: str, lint: dict) -> str:
    n = voicelint.open_count(lint)
    return f"{head}; hostlint " + (f"open {n}" if n is not None else "unavailable")


def write_manifest(voice_dir: Path, lines: list[dict], chains: dict, voices_sha256: str, ref_sha256: str | None,
                   tts: dict | None = None) -> None:
    """Record, atomically, what this run used: the sha256 of the voices.json bytes the lines were built from, the host
    reference digest (None in a dry run) and, per line, the voice, params and post chain applied. The render reads these
    values back instead of looking at the config again. `tts` (a real run, after voicing) adds where voicing ran:
    tts_target, device and, after a remote fallback, remote_skip."""
    body = {"schema": MANIFEST_SCHEMA, "voices_sha256": voices_sha256, "ref_sha256": ref_sha256,
            "lines": [{"id": ln["id"], "voice": ln["voice"], "params": ln["params"], "post": chains[ln["id"]]}
                      for ln in lines]}
    for key in ("tts_target", "device", "remote_skip"):
        if tts and key in tts:
            body[key] = tts[key]
    files.write_json(Path(voice_dir) / MANIFEST, body)


def run_stage(ctx) -> StageResult:
    """The voice stage. Every failure reason is a short code: settings_missing, stale_cleanup, gate_not_accepted (the
    verdict, script and ledger do not pass gate.read_verified as one read), quiet_script_at_voice,
    voice_config:<code> (voices.json missing or invalid; the code names the key), voice_missing:<agent ids> (an agent
    in the ledger has no voice), voice_lines:<class>, missing:<setting> (a real run needs tts_python and
    whisper_python), voice_ref:<code> (the host reference is missing or is not the recording its hash names),
    voice_config_changed (voices.json changed while the runner ran), voice:<code or class>,
    align_coverage:<id>=<coverage>,..."""
    work = Path(ctx.work)
    try:
        _clear_stale(work)
    except OSError:
        return StageResult(ok=False, reason="stale_cleanup")
    settings = getattr(ctx, "settings", None)
    if settings is None:
        return StageResult(ok=False, reason="settings_missing")
    # One read of verdict, script and ledger (gate.read_verified): every check the gate's `accepts` makes is made on the
    # same bytes that are parsed below, so a file swapped at any moment cannot put unchecked text on the air.
    script_bytes, ledger_bytes, _why = gate.read_verified(work / "verdict.json", work / "script.json",
                                                          work / "ledger.json", TEXT_CHECKS)
    if script_bytes is None:  # why is a gate code (possibly with names); the reason is the bare code
        return StageResult(ok=False, reason="gate_not_accepted")
    voices_path = paths.voices_path()
    try:
        script = json.loads(script_bytes.decode("utf-8"))
        ledger = json.loads(ledger_bytes.decode("utf-8"))
        if isinstance(script, dict) and script.get("quiet"):
            return StageResult(ok=False, reason="quiet_script_at_voice")
        try:
            voices, voices_sha = voiceconf.load_voices_hashed(voices_path)
        except voiceconf.VoiceConfigError as e:
            return StageResult(ok=False, reason=f"voice_config:{e.code}")
        missing = unvoiced_agents(ledger, voices)
        if missing:
            return StageResult(ok=False, reason="voice_missing:" + ",".join(missing))
        lines = build_lines(script, ledger, voices)
    except (voiceconf.VoiceConfigError, VoiceError, LookupError, TypeError, OSError, ValueError) as e:
        return StageResult(ok=False, reason=f"voice_lines:{type(e).__name__}")
    voice_dir = work / "voice"
    try:
        voice_dir.mkdir(parents=True)
        files.write_json(voice_dir / "lines.json", lines)
        lint = voicelint.write_record(script, work / "hostlint.json", settings.avoid_words)
        chains = {ln["id"]: voices[ln["voice"]]["post"] for ln in lines}
        if ctx.dry:
            write_manifest(voice_dir, lines, chains, voices_sha, None)
            dry_voice(lines, voice_dir)
            return StageResult(ok=True, reason=_summary("dry", lint))
        try:
            whisper_python = settings.require("whisper_python")
            if "local" in _targets(settings):
                settings.require("tts_python")
        except SettingsError as e:
            return StageResult(ok=False, reason=str(e))
        ok, ref = voiceconf.ref_ok(voices[voiceconf.HOST])
        if not ok:
            code = ref if ref in voiceconf.REF_MESSAGES else "refused"  # a known code only: never a path
            return StageResult(ok=False, reason=f"voice_ref:{code}")
        write_manifest(voice_dir, lines, chains, voices_sha, ref)
        results = run_tts(lines, voice_dir, voices_path, ref, settings=settings, voices_sha=voices_sha, day=ctx.day)
        if isinstance(results, dict):  # the manifest says where voicing ran
            write_manifest(voice_dir, lines, chains, voices_sha, ref, results)
        if files.sha256_bytes(voices_path.read_bytes()) != voices_sha:
            return StageResult(ok=False, reason="voice_config_changed")  # the runner read a voices.json this stage did not
        for ln in lines:
            if chains[ln["id"]]:
                post_process(voice_dir / f"{ln['id']}.wav", chains[ln["id"]])
        covs = align_lines(lines, voice_dir, {line_id: chain for line_id, chain in chains.items() if chain},
                           python=whisper_python, model=settings.whisper_model)
    except (RuntimeError, subprocess.SubprocessError, OSError, ValueError, LookupError, TypeError) as e:
        if isinstance(e, VoiceError) and e.args == ("voice_config_changed",):
            return StageResult(ok=False, reason="voice_config_changed")  # voices.json changed before voicing began
        return StageResult(ok=False, reason=failure_code(e))
    low = [f"{line_id}={cov}" for line_id, cov in covs if not cov >= align.MIN_COVERAGE]
    if low:
        return StageResult(ok=False, reason="align_coverage:" + ",".join(low))
    return StageResult(ok=True, reason=_summary(f"{len(lines)} lines", lint))
