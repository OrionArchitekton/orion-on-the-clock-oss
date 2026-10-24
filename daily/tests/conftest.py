"""Puts the repo root on sys.path so tests import `daily.*`, builds a synthetic private-config fixture, and keeps every
test away from the real config and state folders and on one fixed time zone."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

LANES = {
    "lanes": {
        "demo-tool": {"disclosure": "public", "label": "the demo tool", "why": "so every demo runs itself"},
        "backup-box": {"disclosure": "headline", "label": "the offsite backup system",
                       "why": "so one dead disk never costs a week"},
        "diary-app": {"disclosure": "public", "label": "a diary", "why": "listed by mistake"},
    },
    "hard_exclude": ["diary-app", "secret-garden"],
    "blocked_names": ["Zephyr"],
}
EMBARGO = {"active": True, "terms": ["tree talk"], "note": "test"}
TZ = "America/Los_Angeles"  # the synthetic data below is laid out in a US zone with daylight saving
SETTINGS = f'''timezone = "{TZ}"

[show]
series = "Demo Agents on the Clock"
host_name = "Sam"
disclosure = "Narrated with an AI voice; agent lines are verbatim from logs."

[github]
owner = "demo-owner"
'''


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "lanes.json").write_text(json.dumps(LANES))
    (d / "embargo.json").write_text(json.dumps(EMBARGO))
    (d / "settings.toml").write_text(SETTINGS)
    return d


@pytest.fixture
def cfg(cfg_dir: Path):
    from daily.config import load_config
    return load_config(cfg_dir)


@pytest.fixture
def settings(cfg_dir: Path):
    from daily.settings import load_settings
    return load_settings(cfg_dir)


@pytest.fixture(autouse=True)
def _fixed_zone():
    """Every test cuts days in the same zone, and leaves the module as it found it."""
    from daily import day
    saved = day.zone_name()
    day.use_timezone(TZ)
    yield
    day.use_timezone(saved)


@pytest.fixture(autouse=True)
def _no_real_dirs(monkeypatch, tmp_path_factory):
    """No test may read or write the operator's real config or state: both point at empty temp folders unless a test
    sets them itself."""
    base = tmp_path_factory.mktemp("isolated")
    monkeypatch.setenv("ORION_CLOCK_CONFIG", str(base / "no-config"))
    monkeypatch.setenv("ORION_CLOCK_STATE", str(base / "no-state"))
