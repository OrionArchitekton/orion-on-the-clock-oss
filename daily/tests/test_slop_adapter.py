"""The slop gate now reads its hard tier from the vendored shared creative adapter.

The adapter is a generated, stdlib-only, self-verifying file next to slop_gate.py. These tests pin the gate's
contract to the exact behaviour it had before the move (same hits, same order, same URL rule) and prove the gate
fails closed when the adapter is missing or edited by hand. Long dashes are built from code points.
"""
from __future__ import annotations

import re
import shutil

import pytest

from daily import paths, textcheck

EM, EN, BAR = chr(0x2014), chr(0x2013), chr(0x2015)
FROZEN_HARD_LEXEMES = (
    "delve", "tapestry", "multifaceted", "seamless", "a myriad of", "it's worth noting", "in today's digital age",
    "testament to", "game-changer", "paradigm shift", "navigating the landscape",
)
CORPUS = (
    "",
    "The new browser pool shipped today and every single test passed.",
    f"A long day {EM} done.",
    f"Two {EN} three {BAR} four {EM} five.",
    "Let us delve into it.",
    "We saw a\nmyriad   of bugs.",
    "See https://example.com/seamless-rollbacks for more.",
    f"Read https://example.com/x{EM}y now.",
    "In today's digital age the paradigm shift is a testament to a game-changer.",
    "Seamless DELVE Tapestry",
)


def _legacy_scan(text):
    """The gate exactly as it shipped in 0.1.0, kept here as the parity reference."""
    if not text:
        return []
    text = re.sub(r"https?://\S+", " ", text)
    hits = []
    for dash in (EM, EN, BAR):
        if dash in text:
            hits.append(("banned_dash", f"U+{ord(dash):04X}"))
    for term in FROZEN_HARD_LEXEMES:
        body = r"\s+".join(re.escape(t) for t in term.split())
        if re.search(r"\b" + body + r"\b", text, re.IGNORECASE):
            hits.append(("ai_slop_lexeme", term))
    return hits


@pytest.fixture(scope="module")
def slop():
    return textcheck.load_module(paths.CHECKS_DIR / "slop_gate.py", "slop_gate")


def test_the_vendored_adapter_verifies_itself():
    adapter = textcheck.load_module(paths.CHECKS_DIR / "_orion_creative_adapter.py", "orion_creative_adapter")
    assert adapter.verify_self() is True
    assert adapter.ADAPTER_SCHEMA == "orion-creative-adapter/v1"


def test_the_hard_tier_is_unchanged(slop):
    assert tuple(slop.HARD_LEXEMES) == FROZEN_HARD_LEXEMES
    assert slop.BANNED_DASHES == (EM, EN, BAR)
    for text in CORPUS:
        assert slop.scan(text) == _legacy_scan(text), text


def test_a_missing_adapter_fails_closed(tmp_path):
    shutil.copy(paths.CHECKS_DIR / "slop_gate.py", tmp_path / "slop_gate.py")
    with pytest.raises(ImportError):
        textcheck.load_module(tmp_path / "slop_gate.py", "slop_gate")


def test_an_edited_adapter_fails_closed(tmp_path):
    shutil.copy(paths.CHECKS_DIR / "slop_gate.py", tmp_path / "slop_gate.py")
    source = (paths.CHECKS_DIR / "_orion_creative_adapter.py").read_text(encoding="utf-8")
    edited = source.replace("'delve',", "'delvex',", 1)
    assert edited != source
    sentinel = tmp_path / "tampered_executed.txt"
    payload = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n" + edited
    (tmp_path / "_orion_creative_adapter.py").write_text(payload, encoding="utf-8")
    with pytest.raises(ImportError):
        textcheck.load_module(tmp_path / "slop_gate.py", "slop_gate")
    assert not sentinel.exists()  # digest check stops tampered code before exec_module
