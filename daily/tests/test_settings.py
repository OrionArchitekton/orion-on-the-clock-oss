"""settings.toml: strict, fail-closed loading of the pluggable pieces (time zone, show, sources, checks, writer, and the
keys later stages read). Every refusal is a short code naming a key, never a value."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from daily import day, paths
from daily.settings import DEFAULT_PREFIXES, Settings, SettingsError, load_settings
from conftest import SETTINGS


def write(cfg_dir: Path, text: str) -> Path:
    (cfg_dir / "settings.toml").write_text(text)
    return cfg_dir


def refused(cfg_dir: Path, text: str) -> str:
    with pytest.raises(SettingsError) as e:
        load_settings(write(cfg_dir, text))
    return str(e.value)


def test_the_fixture_settings_load(settings):  # positive control for every refusal below
    assert isinstance(settings, Settings)
    assert (settings.timezone, settings.series, settings.host_name) == ("America/Los_Angeles",
                                                                        "Demo Agents on the Clock", "Sam")
    assert settings.github and settings.github_owner == "demo-owner"
    assert settings.claude_code and settings.codex and settings.adapters == () and settings.tracker is None
    assert settings.harness_prefixes == DEFAULT_PREFIXES
    assert settings.claude_projects == Path.home() / ".claude/projects"
    assert settings.codex_db == Path.home() / ".codex/state_5.sqlite"
    assert settings.public_hosts == ("youtube.com", "www.youtube.com")
    assert settings.writer_model == "sonnet" and settings.writer_budget_usd == 2.0
    assert settings.browser_endpoint == "http://127.0.0.1:9222" and settings.notify_command == ()
    assert settings.channel_id is None and settings.tts_host is None and settings.avoid_words is None


def test_the_shipped_example_loads():
    s = load_settings(paths.REPO / "config.example")
    assert s.timezone == "America/Chicago" and s.github_owner == "your-github-name"
    assert s.harness_display() == {"claude_code": "Claude Code", "codex": "Codex"}


def test_the_default_folder_is_the_config_dir(monkeypatch, cfg_dir):
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(cfg_dir))
    assert load_settings().series == "Demo Agents on the Clock"


def test_a_missing_file_fails_closed(tmp_path):
    with pytest.raises(SettingsError, match="^settings_missing$"):
        load_settings(tmp_path)


@pytest.mark.parametrize("text", ["timezone = ", 'timezone = "a"\ntimezone = "b"\n', "\xff\xfe"],
                         ids=["broken", "repeated-key", "not-utf8"])
def test_unreadable_toml_fails_closed(cfg_dir, text):
    (cfg_dir / "settings.toml").write_bytes(text.encode("latin-1"))
    with pytest.raises(SettingsError, match="^settings_unreadable$"):
        load_settings(cfg_dir)


@pytest.mark.parametrize("drop,code", [
    ('timezone = "America/Los_Angeles"\n', "missing:timezone"),
    ('series = "Demo Agents on the Clock"\n', "missing:show.series"),
    ('host_name = "Sam"\n', "missing:show.host_name"),
    ('disclosure = "Narrated with an AI voice; agent lines are verbatim from logs."\n', "missing:show.disclosure"),
    ('owner = "demo-owner"\n', "missing:github.owner"),
])
def test_a_required_key_left_out_is_named(cfg_dir, drop, code):
    assert drop in SETTINGS  # positive control: the fixture has it
    assert refused(cfg_dir, SETTINGS.replace(drop, "")) == code


def test_github_off_needs_no_owner(cfg_dir):
    text = SETTINGS.replace('owner = "demo-owner"\n', "") + "\n[sources]\ngithub = false\n"
    s = load_settings(write(cfg_dir, text))
    assert s.github is False and s.github_owner is None


@pytest.mark.parametrize("extra,code", [
    ("color = 'blue'\n", "unknown:color"),
    ("[show]\n", "settings_unreadable"),  # a repeated table is a TOML error
    ("[sources]\nclaude = true\n", "unknown:sources.claude"),
    ("[writer]\nmodle = 'opus'\n", "unknown:writer.modle"),
    ("[channel]\nvisiblity = 'public'\n", "unknown:channel.visiblity"),
])
def test_an_unknown_key_is_never_ignored(cfg_dir, extra, code):
    """A typo must stop the run: a misspelled key silently ignored could leave a guard off."""
    text = SETTINGS.replace("[show]", extra + "\n[show]", 1) if extra.startswith("color") else SETTINGS + extra
    assert refused(cfg_dir, text) == code


@pytest.mark.parametrize("zone", ["Mars/Olympus", "", "PST8PDT-oops"])
def test_a_bad_zone_is_refused_and_the_active_zone_is_untouched(cfg_dir, zone):
    before = day.zone_name()
    assert refused(cfg_dir, SETTINGS.replace("America/Los_Angeles", zone)) == "bad:timezone"
    assert day.zone_name() == before


def test_loading_good_settings_does_not_switch_the_zone(cfg_dir):
    day.use_timezone("UTC")
    load_settings(write(cfg_dir, SETTINGS.replace("America/Los_Angeles", "Asia/Tokyo")))
    assert day.zone_name() == "UTC"  # run.py switches it, explicitly, once per run


@pytest.mark.parametrize("line,code", [
    ('timezone = 5', "bad:timezone"),
    ('owner = "-oProxyCommand"', "bad:github.owner"),
    ('owner = "owner with spaces"', "bad:github.owner"),
    ('series = ""', "bad:show.series"),
    ('series = "two\\nlines"', "bad:show.series"),
    ('host_name = 7', "bad:show.host_name"),
])
def test_a_bad_value_is_refused_by_key(cfg_dir, line, code):
    key = line.split(" = ")[0]
    text = re.sub(rf"(?m)^{key} = .*$", lambda m: line, SETTINGS, count=1)
    assert line in text
    assert refused(cfg_dir, text) == code


def test_a_refusal_never_echoes_the_value(cfg_dir):
    secretish = "ghp_" + "x" * 30
    msg = refused(cfg_dir, SETTINGS.replace('owner = "demo-owner"', f'owner = "{secretish}"'))
    assert msg == "bad:github.owner" and secretish not in msg


@pytest.mark.parametrize("block,code", [
    ("[sources]\nclaude_code = false\ncodex = false\n", "bad:sources"),
    ("[sources]\nclaude_code = 'yes'\n", "bad:sources.claude_code"),
    ("[sources]\nclaude_projects = 'relative/path'\n", "bad:sources.claude_projects"),
    ("[sources]\ntracker = 'tracker.py'\n", "bad:sources.tracker"),
    ("[[sources.adapters]]\nharness = 'Bad-Id'\ndisplay = 'X'\nmodule = '/a/x.py'\n", "bad:sources.adapters[0].harness"),
    ("[[sources.adapters]]\nharness = 'codex'\ndisplay = 'X'\nmodule = '/a/x.py'\n",
     "duplicate:sources.adapters[0].harness"),
    ("[[sources.adapters]]\nharness = 'my_agent'\ndisplay = 'X'\n", "missing:sources.adapters[0].module"),
    ("[[sources.adapters]]\nharness = 'my_agent'\ndisplay = 'X'\nmodule = '/a/x.py'\nextra = 1\n",
     "unknown:sources.adapters[0].extra"),
    ("[checks]\npublic_hosts = ['https://evil.example']\n", "bad:checks.public_hosts"),
    ("[checks]\nredact_patterns = [{ name = 'x', regex = '(' }]\n", "bad:checks.redact_patterns[0].regex"),
    ("[checks]\nredact_patterns = [{ name = 'x' }]\n", "missing:checks.redact_patterns[0].regex"),
    ("[writer]\nbudget_usd = 0\n", "bad:writer.budget_usd"),
    ("[writer]\nbudget_usd = true\n", "bad:writer.budget_usd"),
    ("[writer]\nbudget_usd = 500\n", "bad:writer.budget_usd"),
    ("[writer]\nmodel = '--dangerously-skip-permissions'\n", "bad:writer.model"),
    ("[writer]\navoid_words = 'clever'\n", "bad:writer.avoid_words"),
    ("[channel]\nid = 'not-a-channel'\n", "bad:channel.id"),
    ("[channel]\nhome_id = 'UCshort'\n", "bad:channel.home_id"),
    ("[browser]\nendpoint = 'file:///etc/passwd'\n", "bad:browser.endpoint"),
    ("[notify]\ncommand = 'notify-send'\n", "bad:notify.command"),
    ("[tts]\nhost = '-oProxyCommand=evil'\n", "bad:tts.host"),
])
def test_optional_tables_are_validated_when_present(cfg_dir, block, code):
    text = SETTINGS.replace("[github]", block + "\n[github]", 1) if block.startswith("[sources]") \
        or block.startswith("[[sources") else SETTINGS + "\n" + block
    assert refused(cfg_dir, text) == code


def test_harness_prefixes_may_only_name_a_transcript_harness(cfg_dir):
    ok = load_settings(write(cfg_dir, SETTINGS + '[github.harness_prefixes]\n"bot/" = "codex"\n'))
    assert ok.harness_prefixes == (("bot/", "codex"),)
    assert refused(cfg_dir, SETTINGS + '[github.harness_prefixes]\n"bot/" = "my_agent"\n') == \
        "bad:github.harness_prefixes"


def test_every_optional_piece_loads_when_well_formed(cfg_dir, tmp_path):
    text = SETTINGS.replace("[github]", f"""[sources]
codex = false
claude_projects = "{tmp_path / 'claude'}"
tracker = "{tmp_path / 'tracker.py'}"

[[sources.adapters]]
harness = "my_agent"
display = "My Agent"
module = "{tmp_path / 'my_agent.py'}"

[github]""", 1) + f"""
[checks]
public_hosts = ["example.com"]
redact_patterns = [{{ name = "machine", regex = 'box-\\d+' }}]
dir = "{tmp_path / 'checks'}"

[writer]
model = "opus"
budget_usd = 1.5
voice_style = ["Talks fast."]
voice_samples = ["So that worked."]
voice_tags = ["Done."]
avoid_words = ["robust"]

[channel]
id = "UC{'a' * 22}"
name = "Demo Channel"

[browser]
endpoint = "http://127.0.0.1:9333"

[notify]
command = ["notify-send", "daily build"]

[tts]
host = "user@gpu-box"
remote_python = "/opt/tts/bin/python"
remote_dir = "/var/tmp/tts-runs"
"""
    s = load_settings(write(cfg_dir, text))
    assert s.codex is False and s.claude_projects == tmp_path / "claude" and s.tracker == tmp_path / "tracker.py"
    assert [(a.harness, a.display, a.module) for a in s.adapters] == [("my_agent", "My Agent", tmp_path / "my_agent.py")]
    assert s.harness_display() == {"claude_code": "Claude Code", "my_agent": "My Agent"}
    assert s.public_hosts == ("example.com",) and s.checks_dir == tmp_path / "checks"
    assert [n for n, _ in s.redact_patterns] == ["machine"] and s.redact_patterns[0][1].search("BOX-12")
    assert (s.writer_model, s.writer_budget_usd, s.voice_style, s.voice_samples, s.voice_tags, s.avoid_words) == \
        ("opus", 1.5, ("Talks fast.",), ("So that worked.",), ("Done.",), ("robust",))
    assert s.channel_id == "UC" + "a" * 22 and s.channel_name == "Demo Channel"
    assert s.browser_endpoint == "http://127.0.0.1:9333" and s.notify_command == ("notify-send", "daily build")
    assert s.tts_host == "user@gpu-box"


def test_require_fails_closed_for_a_key_a_stage_needs(settings):
    with pytest.raises(SettingsError, match="^missing:channel_id$"):
        settings.require("channel_id")
    with pytest.raises(SettingsError, match="^missing:tts_host$"):
        settings.require("tts_host")
    assert settings.require("github_owner") == "demo-owner"  # positive control


def test_a_home_relative_path_expands(cfg_dir):
    s = load_settings(write(cfg_dir, SETTINGS.replace("[github]", "[sources]\ncodex_db = '~/elsewhere/state.sqlite'\n\n[github]", 1)))
    assert s.codex_db == Path.home() / "elsewhere/state.sqlite"


# ---- the voice and render keys ----

def test_the_media_keys_default_to_nothing_that_runs(settings):
    assert settings.handle is None and settings.tts_python is None and settings.tts_lock is None
    assert settings.tts_target == "auto" and settings.tts_remote_gpu_allow == ()
    assert settings.whisper_python is None and settings.whisper_model == "small.en"
    assert settings.render_kit is None and settings.render_command == ("hyperframes",) and settings.render_sandbox == ()
    for key in ("tts_python", "whisper_python", "render_kit"):
        with pytest.raises(SettingsError, match=f"^missing:{key}$"):
            settings.require(key)


@pytest.mark.parametrize("block,code", [
    ("[show]\nhandle = 'NoAt'\n", "bad:show.handle"),
    ("[tts]\npython = 'venv/bin/python'\n", "bad:tts.python"),
    ("[tts]\nlock = 'tts.lock'\n", "bad:tts.lock"),
    ("[tts]\ntarget = 'cloud'\n", "bad:tts.target"),
    ("[tts]\ntarget = 'remote'\n", "missing:tts.host"),
    ("[tts]\nhost = 'gpu-box'\nremote_dir = '/var/tmp/runs'\n", "missing:tts.remote_python"),
    ("[tts]\nhost = 'gpu-box'\nremote_python = '/opt/bin/python'\n", "missing:tts.remote_dir"),
    ("[tts]\nhost = 'gpu-box'\nremote_python = '/opt/bin/python'\nremote_dir = 'runs'\n", "bad:tts.remote_dir"),
    ("[tts]\nhost = 'gpu-box'\nremote_python = '/opt/bin/python'\nremote_dir = '/var/tmp/../etc'\n",
     "bad:tts.remote_dir"),
    ("[tts]\nhost = 'gpu-box'\nremote_python = '/opt/bin/python'\nremote_dir = '/var/tmp/my runs'\n",
     "bad:tts.remote_dir"),
    ("[tts]\nhost = 'gpu-box'\nremote_python = '/opt/bin/python;id'\nremote_dir = '/var/tmp/runs'\n",
     "bad:tts.remote_python"),
    ("[tts]\nhost = 'gpu-box'\nremote_python = '~/bin/python'\nremote_dir = '/var/tmp/runs'\n",
     "bad:tts.remote_python"),
    ("[tts]\nhost = 'gpu-box'\nremote_python = '/opt/bin/python'\nremote_dir = '/'\n", "bad:tts.remote_dir"),
    ("[tts]\nremote_gpu_allow = ['ollama', '-x']\n", "bad:tts.remote_gpu_allow"),
    ("[tts]\nremote_gpu_allow = 'ollama'\n", "bad:tts.remote_gpu_allow"),
    ("[whisper]\npython = 'python3'\n", "bad:whisper.python"),
    ("[whisper]\nmodel = 'small en'\n", "bad:whisper.model"),
    ("[whisper]\nmodle = 'small.en'\n", "unknown:whisper.modle"),
    ("[render]\nkit = 'kit'\n", "bad:render.kit"),
    ("[render]\nhyperframes = []\n", "bad:render.hyperframes"),
    ("[render]\nhyperframes = 'npx hyperframes'\n", "bad:render.hyperframes"),
    ("[render]\nsandbox = 'unshare -n'\n", "bad:render.sandbox"),
    ("[render]\nkitt = '/a/kit'\n", "unknown:render.kitt"),
])
def test_the_media_keys_are_validated_when_present(cfg_dir, block, code):
    text = SETTINGS.replace("[show]\n", block + "\n", 1) if block.startswith("[show]") else SETTINGS + "\n" + block
    assert refused(cfg_dir, text) == code


def test_every_media_key_loads_when_well_formed(cfg_dir, tmp_path):
    text = SETTINGS.replace('disclosure = ', 'handle = "@Demo_Channel"\ndisclosure = ', 1) + f"""
[tts]
python = "{tmp_path / 'tts' / 'python'}"
lock = "~/locks/tts.lock"
target = "remote"
host = "user@gpu-box"
remote_python = "/opt/tts-env/bin/python"
remote_dir = "/var/tmp/tts-runs/"
remote_gpu_allow = ["ollama", "llama-server"]

[whisper]
python = "{tmp_path / 'whisper' / 'python'}"
model = "distil-large-v3"

[render]
kit = "~/kit"
hyperframes = ["npx", "--no-install", "hyperframes"]
sandbox = ["~/bin/render-sandbox.sh", "{{timeout}}"]
"""
    s = load_settings(write(cfg_dir, text))
    assert s.handle == "@Demo_Channel"
    assert s.tts_python == tmp_path / "tts" / "python" and s.tts_lock == Path.home() / "locks" / "tts.lock"
    assert (s.tts_target, s.tts_host, s.tts_remote_python) == ("remote", "user@gpu-box", "/opt/tts-env/bin/python")
    assert s.tts_remote_dir == "/var/tmp/tts-runs" and s.tts_remote_gpu_allow == ("ollama", "llama-server")
    assert s.whisper_python == tmp_path / "whisper" / "python" and s.whisper_model == "distil-large-v3"
    assert s.render_kit == Path.home() / "kit"
    assert s.render_command == ("npx", "--no-install", "hyperframes")
    assert s.render_sandbox == (str(Path.home() / "bin" / "render-sandbox.sh"), "{timeout}")
