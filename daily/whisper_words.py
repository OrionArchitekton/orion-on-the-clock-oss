#!/usr/bin/env python3
"""Word timestamps for the daily build's voiced lines. Run it with the Python of the environment faster-whisper is
installed in (settings.toml `whisper.python`):

    /path/to/whisper-venv/bin/python whisper_words.py MANIFEST_JSON

MANIFEST_JSON: [{"audio", "text", "out"}]. Writes each out as [{"word", "start", "end"}] and prints WHISPER_OK.
CPU, int8, word timestamps, one batch so the model loads once. The model is $ORION_CLOCK_WHISPER_MODEL (default
small.en; settings.toml `whisper.model` sets it) and the thread count $ORION_CLOCK_WHISPER_THREADS (default 4). Only
align.glossary(text) is the prompt, never the line itself: Whisper skips audio that repeats its prompt."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daily.align import glossary  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        sys.exit(__doc__)
    jobs = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    model = WhisperModel(os.environ.get("ORION_CLOCK_WHISPER_MODEL") or "small.en", device="cpu", compute_type="int8",
                         cpu_threads=int(os.environ.get("ORION_CLOCK_WHISPER_THREADS") or "4"))
    for job in jobs:
        segments, _ = model.transcribe(job["audio"], word_timestamps=True, initial_prompt=glossary(job["text"]))
        words = [{"word": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3)}
                 for s in segments for w in (s.words or [])
                 if w.word.strip() and w.start is not None and w.end is not None]
        Path(job["out"]).write_text(json.dumps(words), encoding="utf-8")
        print(f"{Path(job['audio']).name}: {len(words)} words", flush=True)
    print("WHISPER_OK", flush=True)


if __name__ == "__main__":
    main(sys.argv)
