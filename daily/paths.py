"""Constants and directories. Env vars override the private config and state roots; neither lives in the repo."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
REPO = PACKAGE.parent
SCHEMA_PATH = PACKAGE / "script_schema.json"
CHECKS_DIR = PACKAGE / "checks"  # the built-in text checks, loaded by file location (textcheck.load_module)
ENGINE = PACKAGE / "engine"  # the HyperFrames composition template the render stage fills
VOICES_FILE = "voices.json"  # in the config folder: who speaks each line and how (daily.voiceconf)
ENV_CONFIG = "ORION_CLOCK_CONFIG"
ENV_STATE = "ORION_CLOCK_STATE"
DEFAULT_CONFIG = Path("~/.config/orion-on-the-clock")
DEFAULT_STATE = Path("~/.local/state/orion-on-the-clock")
# Where the agent CLIs keep their logs by default; settings.toml can point elsewhere.
DEFAULT_CLAUDE_PROJECTS = Path("~/.claude/projects")
DEFAULT_CODEX_DB = Path("~/.codex/state_5.sqlite")
# The notify stage reason prefixes that mean the outcome reached the operator (run.py exits 5 for a failure so
# reported): sent now, sent by an earlier run, or already closed outside the stage (daily.notify).
NOTIFY_REPORTED = ("notified", "already notified", "closed outside")
STAGE_ORDER = ("collect", "write", "gate", "voice", "render", "publish", "notify")


def config_dir() -> Path:
    return Path(os.environ.get(ENV_CONFIG) or DEFAULT_CONFIG).expanduser()


def voices_path() -> Path:
    """The operator's voices.json (daily.voiceconf), beside settings.toml in the config folder."""
    return config_dir() / VOICES_FILE


def state_root() -> Path:
    return Path(os.environ.get(ENV_STATE) or DEFAULT_STATE).expanduser()


def work_dir(day: dt.date, dry: bool = False) -> Path:
    """The day's work directory. A dry run has its own, a sibling named `<day>-dry`, so a hand-run rehearsal can never
    create, change or delete a file of the real day."""
    return state_root() / (day.isoformat() + ("-dry" if dry else ""))


def last_run_path() -> Path:
    """Where run.py records the day each real run selected (read by an OnFailure handler)."""
    return state_root() / "last-run.json"
