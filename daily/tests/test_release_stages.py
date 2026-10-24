"""run.py registers the release stages last and each resolves to a real module with run_stage (no placeholder is
left); a dry run over a gated, rendered dry folder records publish and notify as dry without a Studio child, a
notify command or a write outside the dry folder."""
import datetime as dt
import importlib
import json
import subprocess

from daily import paths, run
from test_publish import build

SETTINGS_TOML = '''timezone = "America/Los_Angeles"

[show]
series = "Demo Agents on the Clock"
host_name = "Sam"
disclosure = "Narrated with an AI voice; agent lines are verbatim from logs."

[github]
owner = "demo-owner"

[notify]
command = ["notify-hook"]
'''


def _module(mod):
    return importlib.import_module(mod)


def test_publish_follows_render_and_notify_follows_publish():
    order = list(paths.STAGE_ORDER)
    assert order.index("publish") == order.index("render") + 1
    assert order.index("notify") == order.index("publish") + 1
    assert [name for name, _ in run.STAGES] == order
    assert [name for name, _ in run.STAGES][-2:] == ["publish", "notify"]


def test_modules_map_the_release_stages():
    assert run.MODULES["publish"] == "daily.publish" and run.MODULES["notify"] == "daily.notify"
    assert dict(run.STAGES)["publish"] == "daily.publish" and dict(run.STAGES)["notify"] == "daily.notify"


def test_every_stage_is_a_real_module_and_no_placeholder_is_left():
    for name, mod in run.STAGES:
        m = _module(mod)
        assert callable(m.run_stage), name
        assert getattr(m, "REASON", None) != "not_yet_ported", name
        assert "not_yet_ported" not in (m.__doc__ or ""), name


def test_dry_run_records_publish_and_notify_dry_and_touches_no_child(monkeypatch, tmp_path, cfg_dir):
    state = tmp_path / "state"
    monkeypatch.setenv(paths.ENV_STATE, str(state))
    (cfg_dir / "settings.toml").write_text(SETTINGS_TOML)
    (cfg_dir / "publish.json").write_text(json.dumps({"visibility": "public"}))   # public mode on: a rehearsal still never uploads
    monkeypatch.setenv(paths.ENV_CONFIG, str(cfg_dir))
    work = state / "2026-10-16-dry"  # a dry run's own directory (never the real day's)
    work.mkdir(parents=True)
    build(work, dry=True)
    (work / "status.json").write_text(json.dumps({"day": "2026-10-16", "stages": {
        n: {"state": "done", "reason": "", "at": "2026-10-16T00:00:00Z"}
        for n in ("collect", "write", "gate", "voice", "render")}}))

    spawned = []

    def no_child(*a, **k):   # the Studio child, the notify command and every other process: none may start
        spawned.append(a)
        raise AssertionError("a child process started")
    monkeypatch.setattr(subprocess, "Popen", no_child)
    monkeypatch.setattr(subprocess, "run", no_child)

    rc = run.main(["--day", "2026-10-16", "--dry"], today=dt.date(2026, 10, 17))
    assert rc == 0 and spawned == []
    assert not (state / "2026-10-16").exists()  # the real day's directory was never created
    assert not (state / "notify").exists()      # nor the default sink: a rehearsal reports nothing
    stages = json.loads((work / "status.json").read_text())["stages"]
    assert stages["publish"]["state"] == "dry" and stages["publish"]["reason"] == "dry_run"
    assert stages["notify"]["state"] == "dry" and stages["notify"]["reason"] == "dry run: nothing sent"
    assert all(stages[n]["state"] == "done" for n in ("collect", "write", "gate", "voice", "render"))
    item = json.loads((work / "upload-item.json").read_text())
    assert item["dry"] is True and item["id"] == "daily-2026-10-16" and item["visibility"] == "public"
    for absent in ("upload.json", "notify.json", "upload-episode.mp4", "publish.log"):
        assert not (work / absent).exists(), absent
