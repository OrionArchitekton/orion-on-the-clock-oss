"""The stage contract. Defined here so stage modules never import the orchestrator."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Ctx:
    day: dt.date
    work: Path
    cfg: Any  # daily.config.Config (lanes and embargo), or None when it failed to load
    dry: bool
    settings: Any = None  # daily.settings.Settings; a stage that needs it fails closed without it


@dataclass(frozen=True)
class StageResult:
    ok: bool
    reason: str = ""
    quiet: bool = False
