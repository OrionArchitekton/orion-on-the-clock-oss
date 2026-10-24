"""Voice settings (voices.json in your config folder): who speaks each line and how it is processed.

The host is the narrator: a voice clone made from a reference recording you supply (`host.ref`, a path on the machine
that runs the text to speech). Clone only a voice whose owner has agreed to it. The recording is checked against
`host.sha256_prefix` before every real voicing, so a swapped or damaged file stops the run instead of putting another
voice on the air. An agent (harness) voice never has a reference recording: it is Chatterbox's built-in voice plus
its own ffmpeg chain, so no agent ever speaks in a person's voice (a harness entry with a `ref` is refused).

The file is closed and validated on load: "host" plus one entry per agent id (the ids your settings use, such as
claude_code and codex: lower case letters, digits, underscores), every value of its type, and every ffmpeg chain built
from an allowlist of audio filters (a chain is handed to `ffmpeg -af`, so a filter that can read or write files must
not get through). Keys starting with an underscore are notes and are ignored.

A VoiceConfigError carries `code`, a short code that names a key and never a value (`missing:host.ref`,
`bad:codex.color`, `voices_file_unreadable`), which is what a stage may report. Its message can say more and is for a
person reading a terminal, never for a stage reason.

This module is standard library only and imports nothing from the package: the TTS runner loads it in the Chatterbox
environment, on this machine or on a remote one."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

SCHEMA = "daily-build/voices/v1"
HOST = "host"
HARNESS_ID = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")  # the same shape as settings.toml's agent ids
HARNESS_KEYS = ("display", "glyph", "color", "active", "params", "post")
HOST_KEYS = ("display", "ref", "sha256_prefix", "params", "params_cold_open", "post")
PARAM_KEYS = ("exaggeration", "cfg_weight", "temperature")
DISPLAY_MAX, GLYPH_MAX = 40, 4

POST_START = "asetrate=24000*"
POST_FILTERS = frozenset({"asetrate", "aresample", "atempo", "highpass", "lowpass", "equalizer", "bass", "treble",
                          "aecho", "chorus", "acompressor", "volume", "acrusher", "flanger"})
POST_CHARS = re.compile(r"[A-Za-z0-9_=:.,*-]+")  # no ; [ ] ' " \ whitespace, so no graph syntax and no escapes
SHA_PREFIX = re.compile(r"[0-9a-f]{16,64}")
COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
_KEY = re.compile(r"[A-Za-z0-9_]{1,40}\Z")  # a key name safe to put in a code
# ref_ok's failure codes, and the plain words the runner prints for each (never the path)
REF_MESSAGES = {
    "bad_prefix": "voice reference: host.sha256_prefix must be 16 to 64 lowercase hex characters",
    "missing": "voice reference missing: host.ref names no readable file",
    "mismatch": "voice reference sha256 does not start with host.sha256_prefix",
}


class VoiceConfigError(ValueError):
    """A voices.json problem. `code` is a short machine code a stage can use as its failure reason."""

    def __init__(self, message: str, code: str = "voices_invalid"):
        super().__init__(message)
        self.code = code


def _bad(message: str, code: str = "voices_invalid"):
    raise VoiceConfigError(message, code)


def _name(key) -> str:
    return key if isinstance(key, str) and _KEY.match(key) else "?"


def check_post_chain(chain, *, allow_empty: bool = False) -> None:
    """Raise VoiceConfigError unless `chain` is an ffmpeg audio chain made only of allowed filters, starting with the
    24 kHz pitch step."""
    if not isinstance(chain, str):
        _bad("post chain must be a string", "bad_post_chain")
    if chain == "" and allow_empty:
        return
    if not chain.strip():
        _bad("post chain is empty", "bad_post_chain")
    if "file=" in chain.lower():
        _bad("post chain names a file", "bad_post_chain")
    if not POST_CHARS.fullmatch(chain):
        _bad("post chain has a character outside letters, digits and = : . , * - _", "bad_post_chain")
    if not chain.startswith(POST_START):
        _bad(f"post chain must start with {POST_START}", "bad_post_chain")
    for part in chain.split(","):
        name = part.split("=", 1)[0]
        if name not in POST_FILTERS:
            _bad(f"post chain uses a filter that is not allowed: {name!r}", "bad_post_chain")


def _text(owner: str, key: str, value, cap: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > cap or "\n" in value:
        _bad(f"{owner}: {key} must be a one-line string of 1 to {cap} characters", f"bad:{owner}.{key}")


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _params(owner: str, key: str, value) -> None:
    if not isinstance(value, dict):
        _bad(f"{owner}: {key} must be an object", f"bad:{owner}.{key}")
    missing = [k for k in PARAM_KEYS if k not in value]
    if missing:
        _bad(f"{owner}: {key} is missing {', '.join(missing)}", f"missing:{owner}.{key}.{missing[0]}")
    extra = sorted(str(k) for k in value if k not in PARAM_KEYS)
    if extra:
        _bad(f"{owner}: {key} has unknown settings: {', '.join(extra)}", f"unknown:{owner}.{key}.{_name(extra[0])}")
    for k in PARAM_KEYS:
        if not _number(value[k]):
            _bad(f"{owner}: {key}.{k} must be a finite number", f"bad:{owner}.{key}.{k}")


def _need(owner: str, voice, keys: tuple, optional: tuple = ()) -> None:
    if not isinstance(voice, dict):
        _bad(f"{owner}: voice must be an object", f"bad:{owner}")
    missing = [k for k in keys if k not in voice]
    if missing:
        _bad(f"{owner}: missing {', '.join(missing)}", f"missing:{owner}.{missing[0]}")
    extra = sorted(str(k) for k in voice if k not in keys and k not in optional and not str(k).startswith("_"))
    if extra:
        _bad(f"{owner}: unknown keys: {', '.join(extra)}", f"unknown:{owner}.{_name(extra[0])}")


def _check_host(host) -> None:
    _need(HOST, host, HOST_KEYS)
    _text(HOST, "display", host["display"], DISPLAY_MAX)
    _text(HOST, "ref", host["ref"], 4096)
    if not isinstance(host["sha256_prefix"], str) or not SHA_PREFIX.fullmatch(host["sha256_prefix"]):
        _bad("host: sha256_prefix must be 16 to 64 lowercase hex characters", "bad:host.sha256_prefix")
    _params(HOST, "params", host["params"])
    _params(HOST, "params_cold_open", host["params_cold_open"])
    try:
        check_post_chain(host["post"], allow_empty=True)
    except VoiceConfigError as e:
        _bad(f"host: {e}", "bad:host.post")


def _check_harness(h: str, v) -> None:
    _need(h, v, HARNESS_KEYS, optional=("ref",))
    if v.get("ref") is not None:
        _bad(f"{h}: a harness voice never uses a reference recording", f"bad:{h}.ref")
    _text(h, "display", v["display"], DISPLAY_MAX)
    _text(h, "glyph", v["glyph"], GLYPH_MAX)
    if not isinstance(v["color"], str) or not COLOR.fullmatch(v["color"]):
        _bad(f"{h}: color must be #RRGGBB", f"bad:{h}.color")
    if not isinstance(v["active"], bool):
        _bad(f"{h}: active must be true or false", f"bad:{h}.active")
    _params(h, "params", v["params"])
    try:
        check_post_chain(v["post"])
    except VoiceConfigError as e:
        _bad(f"{h}: {e}", f"bad:{h}.post")


def load_voices(path) -> dict:
    return load_voices_hashed(path)[0]


def load_voices_hashed(path) -> tuple[dict, str]:
    """(the voices, the sha256 of the very bytes they were parsed from): one read, so the hash names what was used."""
    voices, sha, _raw = load_voices_raw(path)
    return voices, sha


def load_voices_raw(path) -> tuple[dict, str, bytes]:
    """(the voices, the sha256 of the bytes, the bytes themselves): one read of the file, for a caller that hands the
    very same bytes on (the voice stage writes them into the run's folder for the runner)."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise VoiceConfigError("voices_file_unreadable", "voices_file_unreadable") from None
    return parse_voices_bytes(raw), hashlib.sha256(raw).hexdigest(), raw


def parse_voices_bytes(raw: bytes) -> dict:
    """The voices parsed from bytes the caller already read (and hashed): no second open of the file. The notes
    (keys starting with an underscore) are left out of what is returned."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise VoiceConfigError("voices_file_unreadable", "voices_file_unreadable") from None
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise VoiceConfigError("voices_bad_json", "voices_bad_json") from None
    if not isinstance(data, dict):
        _bad("voices.json must be an object")
    extra = sorted(str(k) for k in data if k not in ("schema", "voices") and not str(k).startswith("_"))
    if extra:
        _bad(f"voices.json has unknown keys: {', '.join(extra)}", f"unknown:{_name(extra[0])}")
    if data.get("schema") != SCHEMA:
        _bad(f"voices.json schema must be {SCHEMA!r}", "bad:schema")
    voices = data.get("voices")
    if not isinstance(voices, dict):
        _bad("voices.json needs a voices object", "bad:voices")
    voices = {k: v for k, v in voices.items() if not str(k).startswith("_")}
    if any(k != HOST and not HARNESS_ID.match(str(k)) for k in voices):
        _bad("a voice id must be host or an agent id (lower case letters, digits, underscores)", "bad_voice_id")
    if HOST not in voices:
        _bad("voices.json has no host voice", "missing:host")
    _check_host(voices[HOST])
    for h in voices:
        if h != HOST:
            _check_harness(h, voices[h])
    return {k: {kk: vv for kk, vv in v.items() if not str(kk).startswith("_")} for k, v in voices.items()}


def harnesses(voices: dict) -> tuple:
    """The agent voice ids, in file order."""
    return tuple(k for k in voices if k != HOST)


def ref_path(voice: dict) -> Path:
    return Path(voice["ref"]).expanduser()


def ref_ok(voice: dict) -> tuple[bool, str]:
    """(True, the first 16 hex characters of the reference's sha256) when the reference file exists and its digest
    starts with the voice's sha256_prefix, else (False, a code from REF_MESSAGES). The code never holds the path."""
    prefix = voice.get("sha256_prefix")
    if not isinstance(prefix, str) or not SHA_PREFIX.fullmatch(prefix):
        return False, "bad_prefix"
    p = ref_path(voice)
    try:
        if not p.is_file():
            return False, "missing"
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return False, "missing"
    if not digest.startswith(prefix):
        return False, "mismatch"
    return True, digest[:16]


def voice_for(harness: str, voices: dict) -> dict:
    """The active voice of an agent. The host is never an agent: a quote is never said in the narrator's voice."""
    v = voices.get(harness) if isinstance(harness, str) and harness != HOST else None
    if v is None or not v.get("active", False):
        raise VoiceConfigError(f"no active voice for harness {harness!r}", "no_active_voice")
    return v
