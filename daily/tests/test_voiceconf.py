"""voices.json: a closed, validated file of who speaks each line and how. Open agent ids, notes ignored, an ffmpeg chain
allowlist, a host reference checked by hash, and error codes that name a key and never a value."""
import copy
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import types

import pytest

from daily import paths, run, voiceconf
from daily.stage import Ctx
from media_helpers import AGENTS, VOICES, needs_ffmpeg

EXAMPLE = paths.REPO / "config.example" / "voices.json"
LONG_DASHES = "".join(chr(c) for c in (0x2013, 0x2014, 0x2015))  # built from code points: no dash in this file


def _data():
    return copy.deepcopy(VOICES)


def _write(tmp_path, data, name="voices.json"):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def _load(tmp_path, data):
    return voiceconf.load_voices(_write(tmp_path, data))


def _code(tmp_path, data) -> str:
    with pytest.raises(voiceconf.VoiceConfigError) as e:
        _load(tmp_path, data)
    return e.value.code


_DEL = object()


def _mutated(data, path, value):
    node = data
    for key in path[:-1]:
        node = node[key]
    if value is _DEL:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return data


# ---- the shipped example and the fixture voices ----

def test_the_shipped_example_loads_as_it_is():
    v = voiceconf.load_voices(EXAMPLE)
    assert voiceconf.harnesses(v) == ("claude_code", "codex")
    assert re.fullmatch(r"[0-9a-f]{16,64}", v["host"]["sha256_prefix"])  # a valid placeholder
    assert v["host"]["ref"].startswith("~/") and v["host"]["post"] == ""
    assert "_comment" not in json.dumps(v)  # the notes are dropped from what loads


def test_the_shipped_example_has_no_long_dash():
    assert not [c for c in EXAMPLE.read_text(encoding="utf-8") if c in LONG_DASHES]


def test_the_fixture_voices_load_in_file_order(tmp_path):
    v = _load(tmp_path, _data())
    assert voiceconf.harnesses(v) == AGENTS
    for h in AGENTS:
        assert v[h]["active"] is True and v[h].get("ref") is None


def test_the_hash_names_the_very_bytes_read(tmp_path):
    p = _write(tmp_path, _data())
    voices, sha = voiceconf.load_voices_hashed(p)
    assert sha == hashlib.sha256(p.read_bytes()).hexdigest()
    assert voices == voiceconf.parse_voices_bytes(p.read_bytes())


@pytest.mark.parametrize("source", ["example", "fixture"])
def test_every_agent_card_chain_and_pitch_is_its_own(tmp_path, source):
    v = voiceconf.load_voices(EXAMPLE) if source == "example" else _load(tmp_path, _data())
    hs = voiceconf.harnesses(v)
    for key in ("display", "glyph", "color", "post"):
        assert len({v[h][key] for h in hs}) == len(hs), key
    assert all(v[h]["post"] for h in hs) and v["host"]["post"] == ""
    pitch = sorted(float(re.match(r"asetrate=24000\*([\d.]+),", v[h]["post"]).group(1)) for h in hs)
    assert min(b - a for a, b in zip(pitch, pitch[1:])) >= 0.03


def _chains():
    out = [("example", h, c["post"]) for h, c in json.loads(EXAMPLE.read_text())["voices"].items() if c["post"]]
    out += [("fixture", h, c["post"]) for h, c in VOICES["voices"].items() if c["post"]]
    return out


@needs_ffmpeg
@pytest.mark.parametrize("where,harness,chain", _chains(), ids=lambda x: x if isinstance(x, str) and "=" not in x else "")
def test_every_shipped_and_fixture_chain_runs_in_ffmpeg(tmp_path, where, harness, chain):
    voiceconf.check_post_chain(chain)
    src, out = tmp_path / "in.wav", tmp_path / "out.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=220:d=1:r=24000", str(src)],
                   check=True, timeout=60)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src), "-af", chain, "-ar", "24000", "-ac", "1",
                    str(out)], check=True, timeout=60)
    assert out.stat().st_size > 1000


# ---- open agent ids, notes ----

def test_any_agent_id_of_the_right_shape_is_a_voice(tmp_path):
    data = _data()
    data["voices"]["release_bot_2"] = dict(data["voices"]["codex"], display="Release Bot", glyph="RB",
                                          color="#010203", post="asetrate=24000*1.5,aresample=24000,atempo=0.6667")
    v = _load(tmp_path, data)
    assert voiceconf.harnesses(v)[-1] == "release_bot_2"
    assert voiceconf.voice_for("release_bot_2", v)["display"] == "Release Bot"


@pytest.mark.parametrize("bad", ["Rogue", "2bot", "with-dash", "a b", "x" * 33, "", "../up", "_"])
def test_a_voice_id_of_the_wrong_shape_is_refused_without_naming_it(tmp_path, bad):
    data = _data()
    data["voices"][bad] = data["voices"]["codex"]
    if bad.startswith("_"):  # an underscore key is a note, never a voice
        assert "_" not in _load(tmp_path, data)
        return
    code = _code(tmp_path, data)
    assert code == "bad_voice_id" and (not bad or bad not in code)


def test_notes_are_ignored_everywhere_they_may_appear(tmp_path):
    data = _data()
    data["_comment"] = ["a note at the top"]
    data["voices"]["_note"] = {"anything": "goes"}
    data["voices"]["host"]["_why"] = "a note inside the host"
    data["voices"]["codex"]["_why"] = "a note inside an agent"
    v = _load(tmp_path, data)
    assert "_note" not in v and "_why" not in v["host"] and "_why" not in v["codex"]
    assert v == _load(tmp_path, _data())  # positive control: the notes change nothing that loads


def test_only_agent_ids_are_harnesses():
    assert voiceconf.harnesses({"host": {}, "a": {}, "b": {}}) == ("a", "b")


# ---- the host reference ----

def test_harness_with_reference_is_rejected(tmp_path):
    data = _data()
    data["voices"]["codex"]["ref"] = "/tmp/someone.wav"
    with pytest.raises(voiceconf.VoiceConfigError, match="never uses a reference") as e:
        _load(tmp_path, data)
    assert e.value.code == "bad:codex.ref" and "someone" not in e.value.code


def test_a_harness_ref_of_null_is_allowed(tmp_path):
    data = _data()
    data["voices"]["codex"]["ref"] = None
    assert _load(tmp_path, data)["codex"]["ref"] is None


def test_ref_ok_checks_the_hash_prefix(tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"voice")
    digest = hashlib.sha256(b"voice").hexdigest()
    assert voiceconf.ref_ok({"ref": str(ref), "sha256_prefix": digest[:16]}) == (True, digest[:16])
    assert voiceconf.ref_ok({"ref": str(ref), "sha256_prefix": "0" * 16}) == (False, "mismatch")
    assert voiceconf.ref_ok({"ref": str(tmp_path / "missing.wav"), "sha256_prefix": "0" * 16}) == (False, "missing")
    assert voiceconf.ref_ok({"ref": str(tmp_path), "sha256_prefix": "0" * 16}) == (False, "missing")  # a folder


def test_ref_ok_accepts_the_full_digest(tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"voice")
    digest = hashlib.sha256(b"voice").hexdigest()
    assert voiceconf.ref_ok({"ref": str(ref), "sha256_prefix": digest}) == (True, digest[:16])


def test_ref_ok_expands_a_home_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "v").mkdir()
    (tmp_path / "v" / "ref.wav").write_bytes(b"voice")
    digest = hashlib.sha256(b"voice").hexdigest()
    assert voiceconf.ref_ok({"ref": "~/v/ref.wav", "sha256_prefix": digest[:16]}) == (True, digest[:16])


def test_ref_ok_codes_never_hold_the_path(tmp_path):
    secret = tmp_path / "secret-folder-name"
    for voice in ({"ref": str(secret / "x.wav"), "sha256_prefix": "0" * 16},
                  {"ref": str(secret / "x.wav"), "sha256_prefix": "bad"}):
        ok, code = voiceconf.ref_ok(voice)
        assert not ok and code in voiceconf.REF_MESSAGES and "secret" not in code
    assert all("secret" not in m and "/" not in m.replace("host.ref", "") for m in voiceconf.REF_MESSAGES.values())


@pytest.mark.parametrize("prefix", ["", "0123456789ab", "0123456789ABCDEF", "zz23456789abcdef", "7" * 65,
                                    " 0123456789abcdef", None, 758])
def test_malformed_sha256_prefix_is_rejected_by_load_and_ref_ok(tmp_path, prefix):
    data = _mutated(_data(), ("voices", "host", "sha256_prefix"), prefix)
    assert _code(tmp_path, data) == "bad:host.sha256_prefix"
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"voice")
    assert voiceconf.ref_ok({"ref": str(ref), "sha256_prefix": prefix}) == (False, "bad_prefix")


# ---- voice_for ----

def test_inactive_unknown_or_host_is_not_an_agent_voice(tmp_path):
    v = _load(tmp_path, _data())
    assert voiceconf.voice_for("my_agent", v)["display"] == "My Agent"
    v["my_agent"]["active"] = False
    for name in ("my_agent", "unknown", "host", "", None, 7):
        with pytest.raises(voiceconf.VoiceConfigError) as e:
            voiceconf.voice_for(name, v)
        assert e.value.code == "no_active_voice"


# ---- the ffmpeg chain allowlist ----

GOOD_CHAIN = "asetrate=24000*1.0,aresample=24000,atempo=1.0"


@pytest.mark.parametrize("chain", [
    GOOD_CHAIN,
    "asetrate=24000*0.9,aresample=24000,atempo=1.1111,highpass=f=160,lowpass=f=4200,equalizer=f=3200:t=q:w=1:g=3",
    "asetrate=24000*0.9,bass=g=5,treble=g=-3,chorus=0.5:0.9:50:0.4:0.25:2,acompressor=ratio=4,volume=1.5",
])
def test_post_chain_with_only_allowed_filters_is_accepted(tmp_path, chain):
    data = _mutated(_data(), ("voices", "codex", "post"), chain)
    assert _load(tmp_path, data)["codex"]["post"] == chain


@pytest.mark.parametrize("chain", [
    "asetrate=24000*1.0,ametadata=mode=print:file=/tmp/leak.txt",
    "asetrate=24000*1.0,volume=2:FILE=/tmp/leak.txt",
    "asetrate=24000*1.0,ametadata=mode=print",
    "asetrate=24000*1.0,amovie=/etc/hostname",
    "asetrate=24000*1.0,volume=2;ametadata=mode=print",
    "asetrate=24000*1.0,volume=2[out]",
    "asetrate=24000*1.0,[in]volume=2",
    "asetrate=24000*1.0,volume='2'",
    "asetrate=24000*1.0,volume=1\\,ametadata=mode=print",
    "asetrate=24000*1.0,\nvolume=2",
    "volume=2",
    "aresample=24000,asetrate=24000*1.0",
    "asetrate=48000*1.0,volume=2",
    "asetrate=24000*1.0,,volume=2",
    "asetrate=24000*1.0,",
    "",
    "   ",
    5,
    None,
    [GOOD_CHAIN],
])
def test_post_chain_outside_the_allowlist_is_rejected_by_its_key(tmp_path, chain):
    assert _load(tmp_path, _mutated(_data(), ("voices", "codex", "post"), GOOD_CHAIN))  # control: field is writable
    code = _code(tmp_path, _mutated(_data(), ("voices", "codex", "post"), chain))
    assert code == "bad:codex.post"
    assert "leak" not in code and "/" not in code


@pytest.mark.parametrize("chain,reason", [
    ("asetrate=24000*1.0,volume=2:file=x", "names a file"),
    ("asetrate=24000*1.0,volume=2:FiLe=x", "names a file"),
    ("asetrate=24000*1.0,volume=2;", "character"),
    ("asetrate=24000*1.0,volume=2[a]", "character"),
    ("asetrate=24000*1.0,volume='2'", "character"),
    ("asetrate=24000*1.0,ametadata=mode=print", "not allowed"),
    ("volume=2", "must start with asetrate=24000"),
    ("", "empty"),
])
def test_post_chain_is_rejected_for_the_right_reason(chain, reason):
    voiceconf.check_post_chain(GOOD_CHAIN)  # control: the checker accepts a good chain
    with pytest.raises(voiceconf.VoiceConfigError, match=reason) as e:
        voiceconf.check_post_chain(chain)
    assert e.value.code == "bad_post_chain"


def test_an_empty_chain_is_allowed_only_when_asked():
    voiceconf.check_post_chain("", allow_empty=True)
    with pytest.raises(voiceconf.VoiceConfigError):
        voiceconf.check_post_chain("")
    with pytest.raises(voiceconf.VoiceConfigError):
        voiceconf.check_post_chain(None, allow_empty=True)


def test_host_post_chain_is_empty_or_valid(tmp_path):
    assert _load(tmp_path, _mutated(_data(), ("voices", "host", "post"), GOOD_CHAIN))["host"]["post"] == GOOD_CHAIN
    assert _code(tmp_path, _mutated(_data(), ("voices", "host", "post"), "ametadata=mode=print:file=/tmp/x")) == \
        "bad:host.post"


# ---- file errors, schema, keys and values: one short code that names a key ----

@pytest.mark.parametrize("content,code", [
    (None, "voices_file_unreadable"),
    ("{not json", "voices_bad_json"),
    ("", "voices_bad_json"),
    (b"\xff\xfe\x00", "voices_file_unreadable"),
])
def test_missing_or_broken_file_is_one_short_coded_error(tmp_path, content, code):
    p = tmp_path / "secret-dir-name" / "voices.json"
    if content is not None:
        p.parent.mkdir()
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
    with pytest.raises(voiceconf.VoiceConfigError) as e:
        voiceconf.load_voices(p)
    assert e.value.code == code and str(e.value) == code and "secret-dir-name" not in str(e.value)


def test_a_directory_in_place_of_the_file_is_the_same_error(tmp_path):
    with pytest.raises(voiceconf.VoiceConfigError) as e:
        voiceconf.load_voices(tmp_path)
    assert e.value.code == "voices_file_unreadable"


def test_top_level_json_that_is_not_an_object_is_rejected(tmp_path):
    p = tmp_path / "voices.json"
    p.write_text("[]")
    with pytest.raises(voiceconf.VoiceConfigError) as e:
        voiceconf.load_voices(p)
    assert e.value.code == "voices_invalid"


def test_a_voice_config_error_in_a_stage_is_a_failed_stage(tmp_path, monkeypatch):
    """The orchestrator turns any exception a stage raises into a failed stage that names only its class."""
    stub = types.ModuleType("daily_stub_voice_stage")
    stub.run_stage = lambda ctx: voiceconf.load_voices(ctx.work / "nope.json")
    monkeypatch.setitem(sys.modules, "daily_stub_voice_stage", stub)
    res = run._invoke("daily_stub_voice_stage", Ctx(dt.date(2026, 10, 16), tmp_path, None, True))
    assert res.ok is False and res.reason == "exception:VoiceConfigError"


@pytest.mark.parametrize("path,code", [
    *[(("voices", "host", k), f"missing:host.{k}") for k in voiceconf.HOST_KEYS],
    *[(("voices", "codex", k), f"missing:codex.{k}") for k in voiceconf.HARNESS_KEYS],
    *[(("voices", "my_agent", "params", k), f"missing:my_agent.params.{k}") for k in voiceconf.PARAM_KEYS],
    (("voices", "host", "params", "temperature"), "missing:host.params.temperature"),
    (("voices", "host", "params_cold_open", "cfg_weight"), "missing:host.params_cold_open.cfg_weight"),
    (("voices", "host"), "missing:host"),
    (("schema",), "bad:schema"),
    (("voices",), "bad:voices"),
], ids=lambda x: ".".join(x) if isinstance(x, tuple) else x)
def test_a_missing_key_is_named_by_its_code(tmp_path, path, code):
    assert _code(tmp_path, _mutated(_data(), path, _DEL)) == code


@pytest.mark.parametrize("path,value,code", [
    (("voices", "codex", "display"), 5, "bad:codex.display"),
    (("voices", "codex", "display"), "", "bad:codex.display"),
    (("voices", "codex", "display"), "x" * 41, "bad:codex.display"),
    (("voices", "codex", "display"), "two\nlines", "bad:codex.display"),
    (("voices", "codex", "glyph"), None, "bad:codex.glyph"),
    (("voices", "codex", "glyph"), "", "bad:codex.glyph"),
    (("voices", "codex", "glyph"), "WIDE5", "bad:codex.glyph"),
    (("voices", "codex", "color"), "orange", "bad:codex.color"),
    (("voices", "codex", "color"), "#FFF", "bad:codex.color"),
    (("voices", "codex", "color"), 16711680, "bad:codex.color"),
    (("voices", "codex", "active"), "yes", "bad:codex.active"),
    (("voices", "codex", "active"), 1, "bad:codex.active"),
    (("voices", "codex", "params"), [], "bad:codex.params"),
    (("voices", "codex", "params"), "fast", "bad:codex.params"),
    (("voices", "codex", "params", "exaggeration"), "0.5", "bad:codex.params.exaggeration"),
    (("voices", "codex", "params", "exaggeration"), True, "bad:codex.params.exaggeration"),
    (("voices", "codex", "params", "exaggeration"), None, "bad:codex.params.exaggeration"),
    (("voices", "codex", "params", "cfg_weight"), float("nan"), "bad:codex.params.cfg_weight"),
    (("voices", "codex", "params", "speed"), 1.0, "unknown:codex.params.speed"),
    (("voices", "codex", "extra"), 1, "unknown:codex.extra"),
    (("voices", "host", "extra"), 1, "unknown:host.extra"),
    (("voices", "host", "display"), 5, "bad:host.display"),
    (("voices", "host", "ref"), 5, "bad:host.ref"),
    (("voices", "host", "ref"), "", "bad:host.ref"),
    (("voices", "host", "params"), {}, "missing:host.params.exaggeration"),
    (("voices", "host", "params_cold_open"), "x", "bad:host.params_cold_open"),
    (("voices", "host", "params_cold_open", "temperature"), "hot", "bad:host.params_cold_open.temperature"),
    (("voices", "host", "post"), 5, "bad:host.post"),
    (("schema",), "daily-build/voices/v2", "bad:schema"),
    (("voices",), [], "bad:voices"),
    (("voices",), None, "bad:voices"),
    (("voices", "codex"), "not an object", "bad:codex"),
    (("voices", "host"), None, "bad:host"),
    (("extra_top",), 1, "unknown:extra_top"),
])
def test_a_wrong_type_or_value_is_named_by_its_key_never_its_value(tmp_path, path, value, code):
    got = _code(tmp_path, _mutated(_data(), path, value))
    assert got == code
    if isinstance(value, str) and len(value) > 3:
        assert value not in got  # the code names the key, never the value


def test_an_unknown_key_with_an_odd_name_is_not_echoed(tmp_path):
    data = _data()
    data["voices"]["codex"]["key with spaces/and slashes"] = 1
    assert _code(tmp_path, data) == "unknown:codex.?"
    data = _data()
    data["voices"]["codex"]["params"]["/etc/secret"] = 1
    assert _code(tmp_path, data) == "unknown:codex.params.?"


def test_the_message_may_say_more_but_the_code_stays_short(tmp_path):
    data = _mutated(_data(), ("voices", "codex", "post"), "asetrate=24000*1.0,amovie=x")
    with pytest.raises(voiceconf.VoiceConfigError) as e:
        _load(tmp_path, data)
    assert "amovie" in str(e.value) and e.value.code == "bad:codex.post"


def test_this_file_has_no_long_dash():
    from pathlib import Path
    assert not [c for c in Path(__file__).read_text(encoding="utf-8") if c in LONG_DASHES]
