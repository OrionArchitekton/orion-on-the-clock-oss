"""Small builders shared by the daily tests (named daily_helpers so it never collides with another suite)."""
from __future__ import annotations

import json
from pathlib import Path


def write_jsonl(path: Path, records: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def words(n: int) -> str:
    """n plain words that trip no gate: used to hit word budgets exactly."""
    pool = "we built the demo tool so a video makes itself from a short plain script".split()
    return " ".join(pool[i % len(pool)] for i in range(n))


DISCLOSURE = "Narrated with an AI voice; agent lines are verbatim from logs."


def good_script(quote_id: str = "q:claude_code:0a1b2c3d", **over) -> dict:
    """A script that passes every text check: 195 host words (15 + 12 + 3 x 45 + 10 + 8 + 15).

    Keyword overrides replace a top-level field, or a section field with a dotted name like `out.teaser`
    (pass it as **{"out.teaser": "..."}).
    """
    s = {
        "schema": "daily-build/script/v1", "day": "2026-10-16", "quiet": False, "quiet_reason": "",
        "story_item_id": "pr:demo-tool#7", "title": "The demo tool makes its own videos now",
        "description": "A short day in the build. " + DISCLOSURE,
        "cold_open": {"host": words(15), "on_screen": "It made its own video"},
        "scoreboard": {"host": words(12)},
        "story": {"host": [words(45), words(45), words(45)], "on_screen": ["Script in", "Video out"]},
        "steal_this": {"host": words(10), "on_screen": "Steal this: one log per run"},
        "confessional": {"host_intro": words(8), "quote_id": quote_id},
        "out": {"host": words(15), "teaser": "Tomorrow: the recap"},
    }
    for key, value in over.items():
        if "." in key:
            section, field = key.split(".", 1)
            s[section] = {**s[section], field: value}
        else:
            s[key] = value
    return s


def no_confessional_script(**over) -> dict:
    """The valid variant with no confessional beat: quote_id and host_intro are both exactly empty (187 host words)."""
    return good_script(**{"confessional.quote_id": "", "confessional.host_intro": "", **over})


def quiet_script(reason: str = "no_items") -> dict:
    return {"schema": "daily-build/script/v1", "day": "2026-10-16", "quiet": True, "quiet_reason": reason,
            "story_item_id": "", "title": "", "description": "",
            "cold_open": {"host": "", "on_screen": ""}, "scoreboard": {"host": ""},
            "story": {"host": [], "on_screen": []}, "steal_this": {"host": "", "on_screen": ""},
            "confessional": {"host_intro": "", "quote_id": ""},
            "out": {"host": "", "teaser": ""}}


QUOTE = "The new browser pool shipped today and every single test passed."


def make_world(tmp_path: Path, quote: str = QUOTE) -> tuple:
    """A Claude transcript holding the quote on line 2, its quote id, and a ledger with one item, one session note and
    one quote that points at that line (the shapes the collect stage writes)."""
    from daily.ledger import quote_id
    src = write_jsonl(tmp_path / "s.jsonl", [
        {"type": "user", "timestamp": "2026-10-16T20:00:00Z", "uuid": "u0",
         "message": {"content": [{"type": "text", "text": "Is the pool done?"}]}},
        {"type": "assistant", "timestamp": "2026-10-16T20:00:05Z", "uuid": "u1",
         "message": {"content": [{"type": "text", "text": "Done. " + quote + " Next up."}]}},
    ])
    qid = quote_id("claude_code", quote, str(src), 2)
    ledger = {"schema": "daily-build/ledger/v1", "day": "2026-10-16",
              "items": [{"id": "pr:demo-tool#7", "kind": "pr"}],
              "sessions": [{"harness": "claude_code", "ref": "0a1b2c3d", "title": "Browser pool",
                            "last_ts": "2026-10-16T20:00:05.000Z"}],
              "quotes": [{"id": qid, "kind": "transcript", "harness": "claude_code", "lane": "demo-tool",
                          "disclosure": "public", "text": quote, "ts": "2026-10-16T20:00:05Z",
                          "source": {"path": str(src), "line": 2, "record": "u1"}}]}
    return src, qid, ledger
