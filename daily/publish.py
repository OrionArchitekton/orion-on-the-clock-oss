"""Not ported yet. The publish stage (the private-by-default upload through YouTube Studio).

This release carries the text half of the pipeline (collect, write, gate). The publish stage arrives in a later release
(phase 3 of the port). Until then this placeholder is registered in the stage order so a run that reaches it fails
closed with `not_yet_ported`, never skips past it. `python3 -m daily.run --day yesterday --until gate --dry` stops
before it.
"""
from __future__ import annotations

from daily.stage import StageResult

REASON = "not_yet_ported"


def run_stage(ctx) -> StageResult:
    return StageResult(ok=False, reason=REASON)
