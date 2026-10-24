"""What a new ledger makes stale, cleared without the media stack: the archive-and-clear of the render's outputs (a
passing render is archived under previous/, anything unverified is renamed -rejected or removed, the build dir goes)
and the voice stage's outputs. Collect imports this module when it is imported, so a text-only install (no numpy, no
Pillow: the [media] extra) can collect, and collect can never delete the old ledger and then fail to load the clear.
The render stage uses the same functions before it renders. Standard library and daily.gate only: a test pins that no
media module is imported here."""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from daily import gate

RENDER_CHECKS = gate.RENDER_REQUIRED  # single source: gate.py
VOICE_OUTPUTS = ("voice", "hostlint.json")


ARCHIVE_NAME = re.compile(r"\d{8}T\d{12}Z(-\d+)?(-dry)?")
KEEP_ARCHIVES = 3  # archived runs kept per kind (real, dry) under work/previous/


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _drop(path: Path) -> None:
    """Remove one exact path. A link is unlinked, never followed."""
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def _passing_render(work: Path, sfx: str) -> bool:
    """True when the top-level render{sfx}.json is a passing render verdict that binds the top-level episode (the
    gate's one judge, `gate.read_verified_render`, reads each file once and judges those bytes). The ledger is not
    bound here: a recollect has replaced it by the time the old run is archived. Anything unreadable or unusual is not
    proof."""
    verdict, _episode, _ledger, _why = gate.read_verified_render(
        work / f"render{sfx}.json", work / f"episode{sfx}.mp4", None, RENDER_CHECKS)
    return verdict is not None


def _archive_previous(work: Path, sfx: str) -> None:
    """Move the last run's episode, render.json and contact sheet(s) into work/previous/<UTC timestamp>/ (atomic moves
    on one filesystem; a symlink moves as the link and is never followed), then keep only the newest KEEP_ARCHIVES
    archives of this kind (dry runs archive as <timestamp>-dry and are pruned apart from real ones). Publish reads
    only the top-level files, so an archived copy can never be published."""
    names = [f"render{sfx}.json", f"episode{sfx}.mp4", f"contact-sheet{sfx}.jpg"]
    names += sorted(p.name for p in work.glob(f"contact-sheet{sfx}-[0-9]*.jpg"))  # the second sheet of a long render
    if not os.path.lexists(work / f"episode{sfx}.mp4"):
        return  # no episode: a lone failing render.json (or sheet) is not a render worth a slot; _clear_stale removes it
    if not _passing_render(work, sfx):
        reject(work, sfx)  # an episode nothing vouches for (killed before its verdict, or failing): inspect, never keep
        return
    present = [n for n in names if os.path.lexists(work / n)]
    root = work / "previous"
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        root.unlink()
    root.mkdir(exist_ok=True)
    stamp = _utc_stamp() + sfx
    dest = root / stamp
    for n in range(1, 1000):  # two reruns inside one microsecond never share a directory
        try:
            dest.mkdir()
            break
        except FileExistsError:
            dest = root / f"{stamp[:len(stamp) - len(sfx)]}-{n}{sfx}"
    for name in present:
        os.replace(work / name, dest / name)
    mine = sorted((p for p in root.iterdir() if ARCHIVE_NAME.fullmatch(p.name) and p.name.endswith("-dry") == bool(sfx)
                   and any(p.glob("episode*.mp4"))), key=lambda p: p.name)
    for old in mine[:-KEEP_ARCHIVES]:
        _drop(old)


def _drop_rejected(work: Path, sfx: str) -> None:
    for old in (work / f"episode-rejected{sfx}.mp4", work / f"contact-sheet-rejected{sfx}.jpg",
                *work.glob(f"contact-sheet-rejected{sfx}-[0-9]*.jpg")):
        old.unlink(missing_ok=True)


def clear_stale(work: Path, sfx: str) -> None:
    """Archive this run's outputs from an earlier run (never delete: a failed rerun must not lose the last known-good
    episode), so none is current while this one renders; remove the rejected copies of a failed run and the whole build
    dir (an old composition, mix or log must never reach this render). A link is unlinked, never followed."""
    _drop_rejected(work, sfx)  # the last failed run's inspection copies, replaced by this run's
    _archive_previous(work, sfx)  # may rename an unverified episode to the -rejected names
    for name in (f"render{sfx}.json", f"contact-sheet{sfx}.jpg"):  # whatever the archive step left is not a render
        (work / name).unlink(missing_ok=True)
    for extra in work.glob(f"contact-sheet{sfx}-[0-9]*.jpg"):
        extra.unlink(missing_ok=True)
    (work / f".episode{sfx}.mp4.part.mp4").unlink(missing_ok=True)  # remux's temp, left by a killed run
    build = work / "build"
    _drop(build)


def reject(work: Path, sfx: str) -> list[str]:
    """A failed render must leave no ready-looking episode: rename the episode and its sheet to the -rejected names
    (atomic, replacing older rejected files) so they can still be inspected. Returns the names that now exist."""
    out = []
    pairs = [(f"episode{sfx}.mp4", f"episode-rejected{sfx}.mp4"), (f"contact-sheet{sfx}.jpg", f"contact-sheet-rejected{sfx}.jpg")]
    pairs += [(p.name, p.name.replace("contact-sheet", "contact-sheet-rejected", 1))
              for p in sorted(work.glob(f"contact-sheet{sfx}-[0-9]*.jpg"))]
    for src, dst in pairs:
        if os.path.lexists(work / src):
            os.replace(work / src, work / dst)
            out.append(dst)
    return out


def clear_voice_outputs(work: Path) -> None:
    """Remove the voice stage's outputs (voice/ and hostlint.json). A link is unlinked, never followed."""
    for name in VOICE_OUTPUTS:
        _drop(Path(work) / name)


def clear_downstream(work: Path, dry: bool = False) -> None:
    """What a new ledger makes stale (collect calls this): for the real and the dry output alike, a passing render is
    archived with its verdict and anything unverified is rejected or removed, as a render rerun does; then the voice
    stage's outputs go. Nothing downstream of the old ledger stays at the top level. A dry collect (`dry`) clears only
    the dry media and their archives: the real episode, verdict, sheets and voice/ are the current deliverable and a
    dry run never touches them."""
    for sfx in (("-dry",) if dry else ("", "-dry")):
        clear_stale(work, sfx)
        _drop_rejected(work, sfx)  # an unverified episode clear_stale just renamed is kept for no one now
    if not dry:
        clear_voice_outputs(work)
