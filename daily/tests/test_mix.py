"""The episode's sound (daily.mix): the music and cue plans, the units (ducking, looping, peak guard, loudness), and
real mixdowns of tone kits, read back by frequency so a test can tell which sound landed where. Needs numpy; the real
mixes need ffmpeg."""
import math
import shutil
import subprocess
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from daily import compose, mix  # noqa: E402
from media_helpers import STINGS, VOICE_HZ, kit_freq, kit_sounds, make_kit, make_sound_kit, needs_ffmpeg, tone  # noqa: E402

LINES = (("cold_open", 3.0), ("scoreboard", 2.0), ("story_0", 3.0), ("story_1", 3.0), ("story_2", 3.0),
         ("steal_this", 4.0), ("confessional_intro", 2.0), ("confessional", 3.0), ("out", 3.0))
NO_CONF = tuple(x for x in LINES if not x[0].startswith("confessional"))
ONE_S = tuple((i, 1.0) for i, _d in NO_CONF)
REQUIRED = sorted({r for r, _g in mix.BEDS.values()} | {r for r, _g in mix.CUES.values()})


def timeline(lines=LINES, min_total=0.0, n_items=3, harness="codex"):
    tl = compose.build_timeline([{"id": i, "dur": d, "words": [{"word": "w", "start": 0.0, "end": d}]}
                                 for i, d in lines], min_total=min_total)
    tl["cues"] = compose.cues(tl, n_items)
    tl["confessional_harness"] = harness if "confessional" in tl["scenes"] else None
    return tl


def stub_kit(root: Path, stings=STINGS) -> Path:
    """A kit of empty files: every required sound plus a sting for each of `stings` (enough for the plans)."""
    kit = make_kit(root)
    for rel in REQUIRED + [mix.entrant_rel(h) for h in stings]:
        (kit / rel).parent.mkdir(parents=True, exist_ok=True)
        (kit / rel).write_bytes(b"x")
    return kit


def power(x, f, t0, t1):
    """Mean power of frequency f in [t0, t1) of a decoded mix (single-bin DFT on the left channel)."""
    seg = x[int(t0 * mix.SR):int(t1 * mix.SR), 0]
    n = np.arange(len(seg))
    return float(abs(np.sum(seg * np.exp(-2j * np.pi * f * n / mix.SR))) / max(len(seg), 1))


def db(a, b):
    return 20 * math.log10(a / b)


def _length(path):
    return int(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                               "stream=duration_ts", "-of", "csv=p=0", str(path)],
                              capture_output=True, text=True, check=True).stdout.strip())


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """One tone kit (stings for STINGS only), one voice dir and one real mixdown per case, shared by the read-only
    assertions: `transcript` (a Codex confessional, which has a sting), `none` (no confessional) and `unstung` (a
    confessional by night_bot, which has no sting in the kit)."""
    root = tmp_path_factory.mktemp("mix")
    kit = make_sound_kit(root)
    tl_t = timeline()
    vd = root / "voice"
    for ln in tl_t["lines"]:
        tone(vd / ln["file"], ln["dur"], VOICE_HZ, sr=24000, ch=1)
    cases = {}
    for name, tl in (("transcript", tl_t), ("none", timeline(NO_CONF)), ("unstung", timeline(harness="night_bot"))):
        out = root / name / "mix.wav"
        info = mix.mixdown(tl, vd, kit, out)
        cases[name] = {"tl": tl, "info": info, "out": out, "x": mix.load(out), "lufs": mix.integrated_lufs(out),
                       "length": _length(out), "files": sorted(p.name for p in out.parent.iterdir())}
    return {"root": root, "kit": kit, "vd": vd, "cases": cases}


def case(env, name):
    c = env["cases"][name]
    return c["tl"], c["x"]


# plans ---------------------------------------------------------------------------------------------------------

def test_music_and_cue_plans_follow_the_timeline(tmp_path):
    tl = timeline()
    plan = mix.music_plan(tl)
    sc = tl["scenes"]
    assert [p["bed"] for p in plan] == ["theme", "bed", "theme"]
    assert plan[1]["start"] == sc["story"]["start"] and plan[2]["end"] == tl["total"]
    assert plan[1]["end"] == round(sc["confessional"]["start"] + sc["confessional"]["dur"] + mix.XF_S, 3)
    sfx = mix.sfx_plan(tl, stub_kit(tmp_path))
    assert [c["cue"] for c in sfx].count("pop") == 4  # three story cards and the Steal-this card
    assert {"t": tl["cues"]["steal_in"], "cue": "pop"} in sfx
    assert {"t": tl["cues"]["harness_in"], "cue": "entrant_codex"} in sfx
    assert all(c["cue"] in mix.CUES or c["cue"] == "entrant_codex" for c in sfx)


def test_plans_without_a_confessional_place_no_sting_and_end_the_bed_after_steal_this(tmp_path):
    tl = timeline(NO_CONF)
    assert "confessional" not in tl["scenes"] and tl["cues"]["harness_in"] is None
    plan = mix.music_plan(tl)
    st = tl["scenes"]["steal_this"]
    assert plan[1]["start"] == tl["scenes"]["story"]["start"]
    assert plan[1]["end"] == round(st["start"] + st["dur"] + mix.XF_S, 3)
    sfx = mix.sfx_plan(tl, stub_kit(tmp_path))
    assert not [c for c in sfx if c["cue"].startswith("entrant_")]
    assert [c["cue"] for c in sfx].count("pop") == 4
    assert {"t": tl["cues"]["steal_in"], "cue": "pop"} in sfx


def test_a_stale_agent_name_without_a_confessional_scene_still_places_no_sting(tmp_path):
    kit = stub_kit(tmp_path)
    tl = timeline(NO_CONF)
    tl["confessional_harness"] = "codex"  # inconsistent input: the scene decides, not the name
    assert not [c for c in mix.sfx_plan(tl, kit) if c["cue"].startswith("entrant_")]
    tl["cues"]["harness_in"] = 3.0  # a stale cue time with no scene: still no sting
    assert not [c for c in mix.sfx_plan(tl, kit) if c["cue"].startswith("entrant_")]


def test_a_confessional_scene_without_a_cue_time_places_no_sting(tmp_path):
    tl = timeline()
    tl["cues"]["harness_in"] = None
    assert not [c for c in mix.sfx_plan(tl, stub_kit(tmp_path)) if c["cue"].startswith("entrant_")]


def test_a_sting_is_planned_only_when_the_kit_holds_one_for_that_agent(tmp_path):
    kit = stub_kit(tmp_path, stings=("codex",))
    stings = lambda tl, k: [c for c in mix.sfx_plan(tl, k) if c["cue"].startswith("entrant_")]  # noqa: E731
    assert stings(timeline(harness="codex"), kit) == [{"t": timeline()["cues"]["harness_in"], "cue": "entrant_codex"}]
    assert stings(timeline(harness="claude_code"), kit) == []  # no file for it in this kit
    assert stings(timeline(harness="night_bot"), kit) == []
    assert stings(timeline(harness="codex"), None) == []  # without a kit no sting is planned
    (kit / "sound" / "entrant_codex.wav").unlink()
    assert stings(timeline(harness="codex"), kit) == []  # positive control above: the file is what decides


@pytest.mark.parametrize("harness", [None, "", "Codex", "../codex", "co dex", "x" * 33, 7])
def test_no_sting_for_anything_that_is_not_an_agent_id(tmp_path, harness):
    kit = stub_kit(tmp_path)
    tl = timeline()
    tl["confessional_harness"] = harness
    assert not [c for c in mix.sfx_plan(tl, kit) if c["cue"].startswith("entrant_")]
    assert mix.entrant_rel(harness) is None


def test_sting_paths_and_cue_files():
    assert mix.entrant_rel("codex") == "sound/entrant_codex.wav"
    assert mix.entrant_rel("my_agent") == "sound/entrant_my_agent.wav"
    for cue, (rel, gain) in mix.CUES.items():
        assert mix.cue_file(cue) == (rel, gain)
    assert mix.cue_file("entrant_night_bot") == ("sound/entrant_night_bot.wav", mix.ENTRANT_GAIN)
    for bad in ("bogus", "entrant_", "entrant_../evil", "entrant_Codex", "entrant_a b"):
        with pytest.raises(KeyError):
            mix.cue_file(bad)


def test_every_kit_sound_has_its_own_test_tone():
    rels = kit_sounds()
    assert len({kit_freq(r) for r in rels}) == len(rels) and min(kit_freq(r) for r in rels) > VOICE_HZ
    assert set(REQUIRED) <= set(rels) and {mix.entrant_rel(h) for h in STINGS} <= set(rels)


def test_parse_lufs_takes_the_summary_line():
    text = "t: 1.0 M: -20 S: -21 I: -30.0 LUFS\nSummary:\n  Integrated loudness:\n    I:         -14.2 LUFS\n"
    assert mix.parse_lufs(text) == -14.2
    assert mix.parse_lufs("no summary") is None
    assert mix.parse_lufs("    I:         -inf LUFS\n") == float("-inf")


# units ---------------------------------------------------------------------------------------------------------

def test_duck_gain_drops_to_about_35_percent_under_a_tone_and_recovers_slowly():
    sr = mix.SR
    v = np.zeros((3 * sr, 2))
    t = np.arange(sr) / sr
    v[sr // 2: sr // 2 + sr] = (0.2 * np.sin(2 * np.pi * 100 * t))[:, None]  # speech from 0.5 s to 1.5 s
    g = mix.duck_gain(v)
    assert len(g) == len(v)
    assert g[int(0.2 * sr)] == pytest.approx(1.0, abs=0.01)  # silence before the voice
    assert g[int(1.2 * sr)] == pytest.approx(0.35, abs=0.02)  # ducked under it
    assert 0.3 < g[int(1.6 * sr)] < 0.5  # 0.1 s after the voice: slow release, still ducked
    assert g[int(2.9 * sr)] > 0.9  # 1.4 s after: recovered


def test_looped_bed_fades_in_and_out_over_the_crossfade_at_its_gain():
    sr = mix.SR
    x = mix.looped(np.ones((sr, 2)), 5 * sr, 0.5)
    assert x.shape == (5 * sr, 2) and mix.XF_S == 0.6
    assert x[0, 0] == 0.0 and x[-1, 0] == pytest.approx(0.0, abs=1e-4)
    assert x[int(0.3 * sr), 0] == pytest.approx(0.25, abs=0.01)  # halfway up the 0.6 s fade
    assert x[int(2.5 * sr), 0] == 0.5


def test_bed_cue_and_sting_gains_are_the_designed_ones():
    assert {k: g for k, (_r, g) in mix.BEDS.items()} == {"theme": 0.40, "bed": 0.26}
    assert {k: g for k, (_r, g) in mix.CUES.items()} == {"jingle": -6.0, "section": -8.0, "pop": -6.0, "cta": -3.0}
    assert mix.ENTRANT_GAIN == -6.0
    assert {k: r for k, (r, _g) in mix.BEDS.items()} == {"theme": "sound/theme.wav", "bed": "music/bed.wav"}


def test_guard_peak_scales_only_above_the_ceiling():
    quiet = np.full((10, 2), 0.5)
    assert np.array_equal(mix.guard_peak(quiet), quiet)
    hot = np.full((10, 2), 2.0)
    assert np.abs(mix.guard_peak(hot)).max() == pytest.approx(0.98)
    assert np.array_equal(mix.guard_peak(hot) / 0.98 * 2.0, hot)  # a pure gain, no clipping


def _pre(path, samples):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:r=48000:d=3",
                    "-af", f"atrim=end_sample={samples}", "-ac", "2", str(path)], check=True, timeout=60)
    return path


@needs_ffmpeg
@pytest.mark.parametrize("delta", [-37, 0, 37])
def test_loudnorm_lands_on_exactly_n_samples(tmp_path, delta):
    n = 48000
    out = tmp_path / "o" / "m.wav"
    out.parent.mkdir()
    mix.loudnorm(_pre(tmp_path / "pre.wav", n + delta), out, n)
    assert _length(out) == n
    assert sorted(p.name for p in out.parent.iterdir()) == ["m.wav"]


@needs_ffmpeg
def test_a_failed_second_pass_leaves_no_part_file_and_no_output(tmp_path, monkeypatch):
    pre, out = _pre(tmp_path / "pre.wav", 48000), tmp_path / "o" / "m.wav"
    out.parent.mkdir()
    real = subprocess.run

    def fake(argv, *a, **kw):
        if any("linear=true" in x for x in argv):
            open(argv[-1], "wb").write(b"half")  # ffmpeg got as far as opening its output
            raise subprocess.CalledProcessError(1, argv)
        return real(argv, *a, **kw)

    monkeypatch.setattr(mix.subprocess, "run", fake)
    with pytest.raises(subprocess.CalledProcessError):
        mix.loudnorm(pre, out, 48000)
    assert list(out.parent.iterdir()) == []


@needs_ffmpeg
def test_a_crashed_measuring_pass_is_an_error_not_a_silent_mix(tmp_path):
    out = tmp_path / "m.wav"
    with pytest.raises(subprocess.CalledProcessError):
        mix.loudnorm(tmp_path / "no-such-file.wav", out, 48000)
    assert not out.exists()


# the real mixes ------------------------------------------------------------------------------------------------

@needs_ffmpeg
@pytest.mark.parametrize("name", ["transcript", "none", "unstung"])
def test_mixdown_is_exact_length_at_target_loudness(env, name):
    c = env["cases"][name]
    assert c["length"] == c["info"]["samples"] == int(round(c["tl"]["total"] * mix.SR))
    assert -15.0 <= c["lufs"] <= -13.0
    assert c["files"] == ["mix.wav"]  # no temp or pre-mix file left beside it


@needs_ffmpeg
def test_voice_lines_start_when_the_timeline_says(env):
    tl, x = case(env, "transcript")
    for ln in tl["lines"]:
        before = power(x, VOICE_HZ, ln["start"] - 0.1, ln["start"] - 0.02)
        after = power(x, VOICE_HZ, ln["start"] + 0.05, ln["start"] + 0.25)
        assert after > 20 * before, ln["id"]


@needs_ffmpeg
def test_beds_sit_under_the_voice_at_their_gains(env):
    tl, x = case(env, "transcript")
    sc = tl["scenes"]
    f = {"theme": kit_freq("sound/theme.wav"), "bed": kit_freq("music/bed.wav")}
    s1 = next(ln["start"] for ln in tl["lines"] if ln["id"] == "story_1")
    voice = power(x, VOICE_HZ, s1 + 0.5, s1 + 2.5)
    assert db(power(x, f["bed"], s1 + 0.5, s1 + 2.5), voice) == pytest.approx(
        20 * math.log10(0.26 * 0.35), abs=3.0)
    co = tl["lines"][0]["start"]
    voice = power(x, VOICE_HZ, co + 0.8, co + 2.2)
    assert db(power(x, f["theme"], co + 0.8, co + 2.2), voice) == pytest.approx(
        20 * math.log10(0.40 * 0.35), abs=3.0)
    # the open carries the theme and not the bed; the talk the bed and not the theme; the out the theme again
    assert power(x, f["theme"], 0.8, 2.2) > 10 * power(x, f["bed"], 0.8, 2.2)
    for s in ("story", "steal_this", "confessional"):
        a, b = sc[s]["start"] + 0.3, sc[s]["start"] + sc[s]["dur"] - 0.3
        assert power(x, f["bed"], a, b) > 10 * power(x, f["theme"], a, b), s
    o = sc["out"]
    assert power(x, f["theme"], o["start"] + 0.8, o["start"] + o["dur"]) > 10 * power(
        x, f["bed"], o["start"] + 0.8, o["start"] + o["dur"])


def cue_level(x, rel, t, ref):
    """dB of the cue's tone over its first 0.14 s against the voice tone in the same mix."""
    return db(power(x, kit_freq(rel), t, t + 0.14), ref)


@needs_ffmpeg
def test_transcript_mix_puts_each_cue_where_the_plan_says_at_its_gain(env):
    tl, x = case(env, "transcript")
    sc, c = tl["scenes"], tl["cues"]
    co = tl["lines"][0]["start"]
    ref = power(x, VOICE_HZ, co + 0.8, co + 2.2)
    for rel, t, gain in (("sound/scoreboard_jingle.wav", sc["scoreboard"]["start"], -6.0),
                         ("sound/section_break.wav", sc["story"]["start"], -8.0),
                         ("sound/entrant_codex.wav", c["harness_in"], mix.ENTRANT_GAIN),
                         ("sound/cta_pop.wav", c["subscribe"], -3.0),
                         ("sound/cta_pop.wav", c["like"], -3.0),
                         *(("sound/word_pop.wav", t, -6.0) for t in c["story_items"] + [c["steal_in"]])):
        assert cue_level(x, rel, t, ref) == pytest.approx(gain, abs=3.0), (rel, t)
        assert power(x, kit_freq(rel), t - 0.25, t - 0.05) < 0.2 * power(x, kit_freq(rel), t, t + 0.14), (rel, t)
    for h in STINGS:  # no other agent's sting anywhere
        if h != "codex":
            assert power(x, kit_freq(mix.entrant_rel(h)), 0, tl["total"]) < 0.001, h


@needs_ffmpeg
def test_an_agent_with_no_sting_in_the_kit_gets_none_and_the_mix_still_succeeds(env):
    tl, x = case(env, "unstung")
    assert not (env["kit"] / "sound" / "entrant_night_bot.wav").exists()  # the premise: the kit has none for it
    assert "confessional" in tl["scenes"] and tl["confessional_harness"] == "night_bot"
    assert env["cases"]["unstung"]["info"]["sfx"] == len(mix.sfx_plan(timeline(NO_CONF), env["kit"]))
    for h in STINGS:  # no sting at all: not another agent's in its place
        assert power(x, kit_freq(mix.entrant_rel(h)), 0, tl["total"]) < 0.001, h
    conf = tl["scenes"]["confessional"]  # the confessional still has its voice and its bed
    a, b = conf["start"] + 0.3, conf["start"] + conf["dur"] - 0.3
    assert power(x, VOICE_HZ, a, b) > 0.001 and power(x, kit_freq("music/bed.wav"), a, b) > 0.0005


@needs_ffmpeg
def test_none_mix_covers_steal_this_and_places_no_sting_or_confessional_bed(env):
    tl, x = case(env, "none")
    sc, c = tl["scenes"], tl["cues"]
    theme, bed = kit_freq("sound/theme.wav"), kit_freq("music/bed.wav")
    st = sc["steal_this"]
    a, b = st["start"] + 0.3, st["start"] + st["dur"] - 0.3
    assert power(x, bed, a, b) > 10 * power(x, theme, a, b)
    ref = power(x, VOICE_HZ, a, b)
    assert cue_level(x, "sound/word_pop.wav", c["steal_in"], ref) == pytest.approx(-6.0, abs=3.0)
    for h in STINGS:
        assert power(x, kit_freq(mix.entrant_rel(h)), 0, tl["total"]) < 0.001, h
    o = sc["out"]  # the bed stops with the Steal-this scene (plus its fade), the out is theme only
    assert power(x, bed, o["start"] + 0.8, o["start"] + o["dur"]) < 0.1 * power(
        x, theme, o["start"] + 0.8, o["start"] + o["dur"])
    for s in sc.values():  # the voice is in the mix in every scene
        assert power(x, VOICE_HZ, s["start"] + 0.3, s["start"] + s["dur"] - 0.3) > 0.001


# safety and failure --------------------------------------------------------------------------------------------

def _fake_ffmpeg(calls):
    def run(argv, *a, **kw):
        calls.append((argv, kw))
        out = subprocess.CompletedProcess(argv, 0, stdout=b"", stderr="")
        if argv[-1] == "-" and "f32le" in argv:
            out.stdout = bytes(mix.SR * 8)  # one second of stereo float32 silence
        elif any("print_format=json" in x for x in argv):
            out.stderr = ('x\n{"input_i":"-20.5","input_tp":"-3.1","input_lra":"4.0","input_thresh":"-31.0",'
                          '"target_offset":"0.3"}\n')
        elif any("linear=true" in x for x in argv):
            open(argv[-1], "wb").write(b"RIFF")
        elif "ebur128" in argv:
            out.stderr = "Summary:\n  Integrated loudness:\n    I:         -14.0 LUFS\n"
        return out
    return run


def test_every_ffmpeg_call_is_an_argv_list_with_nostdin_and_a_timeout(tmp_path, monkeypatch):
    tl = timeline()
    kit, vd = stub_kit(tmp_path), tmp_path / "voice"
    for p in (vd / ln["file"] for ln in tl["lines"]):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    calls = []
    monkeypatch.setattr(mix.subprocess, "run", _fake_ffmpeg(calls))
    out = tmp_path / "out" / "mix.wav"
    info = mix.mixdown(tl, vd, kit, out)
    assert mix.integrated_lufs(out) == -14.0
    assert out.read_bytes() == b"RIFF" and info["input_i"] == "-20.5"
    assert info["sfx"] == len(mix.sfx_plan(tl, kit)) and any(c["cue"] == "entrant_codex" for c in mix.sfx_plan(tl, kit))
    assert len(calls) >= 6
    for argv, kw in calls:
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        assert not kw.get("shell") and isinstance(kw.get("timeout"), (int, float))
        assert argv[0] == "ffmpeg" and "-nostdin" in argv
    # the sting is read from the kit like every other sound
    assert any(str(kit / "sound" / "entrant_codex.wav") in argv for argv, _kw in calls)
    # the second pass: measured values fed back, linear, exact length, target loudness and true peak
    n = int(round(tl["total"] * mix.SR))
    af = next(next(x for x in argv if "linear=true" in x) for argv, _kw in calls if any("linear=true" in x for x in argv))
    assert af.startswith("loudnorm=I=-14:TP=-1.5:LRA=11:measured_I=-20.5:measured_TP=-3.1:measured_LRA=4.0:")
    assert "offset=0.3:linear=true" in af and f"apad=whole_len={n},atrim=end_sample={n}" in af
    assert mix.TARGET == "I=-14:TP=-1.5:LRA=11"


def test_a_failed_mix_leaves_no_output_and_no_older_mix(tmp_path):
    tl = timeline(NO_CONF)
    out = tmp_path / "out" / "mix.wav"
    out.parent.mkdir()
    out.write_bytes(b"older mix")
    with pytest.raises(FileNotFoundError):
        mix.mixdown(tl, tmp_path / "empty-voice", tmp_path / "empty-kit", out)
    assert list(out.parent.iterdir()) == []


@needs_ffmpeg
@pytest.mark.parametrize("rel", REQUIRED)
def test_a_missing_required_kit_sound_fails_before_any_output(env, tmp_path, rel):
    tl, _x = case(env, "transcript")
    kit = tmp_path / "kit"
    shutil.copytree(env["kit"], kit, symlinks=True)
    (kit / rel).unlink()
    out = tmp_path / "out" / "mix.wav"
    calls = []
    real = subprocess.run
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mix.subprocess, "run", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
        with pytest.raises(FileNotFoundError) as e:
            mix.mixdown(tl, env["vd"], kit, out)
    assert not out.exists() and calls == []  # refused before any ffmpeg ran
    assert str(e.value) == f"mix input missing: {Path(rel).name}"  # the file's name, never its folder


def test_a_missing_voice_line_fails_before_any_output(tmp_path, monkeypatch):
    tl = timeline(NO_CONF)
    kit, vd = stub_kit(tmp_path), tmp_path / "voice"
    vd.mkdir()
    calls = []
    monkeypatch.setattr(mix.subprocess, "run", lambda *a, **k: calls.append(a))
    out = tmp_path / "out" / "mix.wav"
    with pytest.raises(FileNotFoundError, match="cold_open"):
        mix.mixdown(tl, vd, kit, out)
    assert calls == [] and not out.exists()


@needs_ffmpeg
def test_a_silent_mix_is_refused_not_normalised(tmp_path):
    tl = timeline(ONE_S)
    silent = tmp_path / "silent.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-t", "1", str(silent)], check=True, timeout=60)
    kit, vd = tmp_path / "kit", tmp_path / "voice"
    for p in [*(kit / r for r in REQUIRED), *(vd / ln["file"] for ln in tl["lines"])]:
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(silent, p)
    out = tmp_path / "out" / "mix.wav"
    with pytest.raises(ValueError, match="silent"):
        mix.mixdown(tl, vd, kit, out)
    assert list(out.parent.iterdir()) == []


def test_no_long_dashes_in_this_test_file():
    dashes = {chr(c) for c in (0x2013, 0x2014, 0x2015)}
    assert not dashes & set(Path(__file__).read_text(encoding="utf-8"))
