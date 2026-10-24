import datetime as dt
from pathlib import Path

import pytest

from daily import paths
from daily.files import now_iso, read_json, sha256_bytes, write_json, write_text
from daily.stage import Ctx, StageResult


def test_work_dir_uses_state_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ORION_CLOCK_STATE", str(tmp_path))
    assert paths.work_dir(dt.date(2026, 10, 16)) == tmp_path / "2026-10-16"


def test_dry_work_dir_is_a_sibling_named_for_the_day_with_dry(monkeypatch, tmp_path):
    monkeypatch.setenv("ORION_CLOCK_STATE", str(tmp_path))
    day = dt.date(2026, 10, 16)
    assert paths.work_dir(day, dry=True) == tmp_path / "2026-10-16-dry"
    assert paths.work_dir(day, dry=False) == paths.work_dir(day) == tmp_path / "2026-10-16"
    assert paths.work_dir(day, dry=True).parent == paths.work_dir(day).parent


def test_config_dir_uses_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(tmp_path))
    assert paths.config_dir() == tmp_path


def test_repo_root_holds_this_package():
    assert (paths.REPO / "daily/paths.py").is_file()
    assert paths.SCHEMA_PATH == paths.PACKAGE / "script_schema.json"
    assert (paths.CHECKS_DIR / "redactor.py").is_file()


def test_defaults_are_under_the_home_folder(monkeypatch):
    monkeypatch.delenv("ORION_CLOCK_CONFIG")
    monkeypatch.delenv("ORION_CLOCK_STATE")
    assert paths.config_dir() == Path.home() / ".config/orion-on-the-clock"
    assert paths.state_root() == Path.home() / ".local/state/orion-on-the-clock"


def test_stage_order_matches_contract():
    assert paths.STAGE_ORDER == ("collect", "write", "gate", "voice", "render", "publish", "notify")


def test_write_json_is_atomic_and_round_trips(tmp_path):
    p = tmp_path / "a" / "b.json"
    write_json(p, {"x": 1})
    assert read_json(p) == {"x": 1}
    assert [f.name for f in p.parent.iterdir()] == ["b.json"]
    write_text(tmp_path / "t.txt", "hi")
    assert (tmp_path / "t.txt").read_text() == "hi"


def test_sha_and_now():
    assert sha256_bytes(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert now_iso().endswith("Z") and "T" in now_iso()


def test_stage_types_defaults(tmp_path):
    r = StageResult(ok=True)
    assert (r.reason, r.quiet) == ("", False)
    c = Ctx(day=dt.date(2026, 10, 16), work=tmp_path, cfg=None, dry=False)
    assert c.work == tmp_path


def test_write_text_cleans_temp_when_replace_fails(monkeypatch, tmp_path):
    p = tmp_path / "s.json"
    write_text(p, "original")

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr("daily.files.os.replace", boom)
    with pytest.raises(OSError):
        write_text(p, "new")
    assert p.read_text() == "original"
    assert [f.name for f in tmp_path.iterdir()] == ["s.json"]


def test_write_json_lone_surrogate_leaves_no_temp(tmp_path):
    p = tmp_path / "s.json"
    write_json(p, {"x": 1})
    with pytest.raises(UnicodeEncodeError):
        write_json(p, {"bad": chr(0xD83D)})
    assert read_json(p) == {"x": 1}
    assert [f.name for f in tmp_path.iterdir()] == ["s.json"]


def test_env_overrides_expand_user(monkeypatch):
    monkeypatch.setenv("ORION_CLOCK_CONFIG", "~/x")
    monkeypatch.setenv("ORION_CLOCK_STATE", "~/y")
    assert paths.config_dir() == Path.home() / "x"
    assert paths.state_root() == Path.home() / "y"
    assert paths.work_dir(dt.date(2026, 10, 16)) == Path.home() / "y" / "2026-10-16"
