"""Not ported yet. The render stage (timeline, captions, mix, the 9:16 video and its render verdict).

This release carries the text half of the pipeline (collect, write, gate). The render stage arrives in a later release
(phase 2 of the port). Until then this placeholder is registered in the stage order so a run that reaches it fails
closed with `not_yet_ported`, never skips past it. `python3 -m daily.run --day yesterday --until gate --dry` stops
before it.
"""
from __future__ import annotations

from daily.stage import StageResult

REASON = "not_yet_ported"


def run_stage(ctx) -> StageResult:
    return StageResult(ok=False, reason=REASON)


def clear_downstream(work, dry: bool = False) -> None:
    """What a new ledger makes stale in the media, called by the collect stage. No stage writes media yet, so there is
    nothing to archive or remove; the ported render stage replaces this with its archive-and-clear step."""
    return None
