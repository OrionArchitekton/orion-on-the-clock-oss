"""Contact sheets: a frame every few seconds plus frames around every scene boundary, tiled with timestamps, so a person
can review an episode at a glance before it goes anywhere. Needs Pillow and ffmpeg."""
from __future__ import annotations

import io
import subprocess
from pathlib import Path

TILE = (270, 480)  # each frame's size on the sheet: a vertical Short
GRAB_TIMEOUT_S = 60  # one frame grab; a stuck ffmpeg must not hang an unattended render stage
LABEL_H = 30


def strip_times(duration: float, boundaries=(), cues=(), every: float = 2.0) -> list[float]:
    """The times to grab: every `every` seconds, four frames around each boundary and two around each cue, all inside
    the video, sorted and without repeats."""
    if every <= 0:
        raise ValueError(f"every must be positive, got {every}")
    ts = {round(i * every, 2) for i in range(int(duration / every + 1e-9) + 1)}
    for b in boundaries:
        ts |= {round(b + d, 2) for d in (-0.3, -0.15, 0.0, 0.15)}
    for c in cues:
        ts |= {round(c + d, 2) for d in (-0.05, 0.45)}
    last = round(max(0.0, duration - 0.04), 2)
    return sorted({min(max(0.0, t), last) for t in ts})


def _grab(video: Path, t: float, tile=TILE) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                           "-vf", f"scale={tile[0]}:{tile[1]}", "-f", "image2pipe", "-vcodec", "png", "-"],
                          capture_output=True, stdin=subprocess.DEVNULL, timeout=GRAB_TIMEOUT_S)


def _frame(video: Path, t: float, tile=TILE):
    from PIL import Image
    r = _grab(video, t, tile)
    if r.returncode != 0 or not r.stdout:
        r = _grab(video, max(0.0, t - 0.1), tile)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(f"no frame at t={t}")
    with Image.open(io.BytesIO(r.stdout)) as im:
        return im.convert("RGB")


def _font(path, size: int = 20):
    from PIL import ImageFont
    if path is not None:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            pass
    return ImageFont.load_default()


def contact_sheets(video: Path, times: list[float], out: Path, cols: int = 6, per_sheet: int = 60,
                   tile: tuple[int, int] = TILE, font=None) -> list[Path]:
    """Write the frames at `times` as sheets of at most `per_sheet` tiles, `cols` across, each labelled with its time:
    <out stem>-1<suffix>, <out stem>-2<suffix>, ... Returns their paths. `font` is a TrueType file for the labels
    (the media kit's body font); without a usable one Pillow's default font is used."""
    from PIL import Image, ImageDraw
    if per_sheet <= 0:
        raise ValueError(f"per_sheet must be positive, got {per_sheet}")
    if not times:
        return []
    face = _font(font)
    paths = []
    for k in range(0, len(times), per_sheet):
        chunk = times[k:k + per_sheet]
        rows = (len(chunk) + cols - 1) // cols
        img = Image.new("RGB", (cols * tile[0], rows * (tile[1] + LABEL_H)), (48, 48, 48))
        d = ImageDraw.Draw(img)
        for i, t in enumerate(chunk):
            x, y = (i % cols) * tile[0], (i // cols) * (tile[1] + LABEL_H)
            img.paste(_frame(video, t, tile), (x, y))
            d.text((x + 8, y + tile[1] + 4), f"t={t:.2f}", font=face, fill=(240, 240, 240))
        path = Path(out).with_name(f"{Path(out).stem}-{k // per_sheet + 1}{Path(out).suffix}")
        img.save(path)
        paths.append(path)
    return paths
