"""Not ported yet. The voice stage (text to speech for every host and agent line).

This release carries the text half of the pipeline (collect, write, gate). The voice stage arrives in a later release
(phase 2 of the port). Until then this placeholder is registered in the stage order so a run that reaches it fails
closed with `not_yet_ported`, never skips past it. `python3 -m daily.run --day yesterday --until gate --dry` stops
before it.
"""
from __future__ import annotations

from daily.stage import StageResult

REASON = "not_yet_ported"


def run_stage(ctx) -> StageResult:
    return StageResult(ok=False, reason=REASON)
