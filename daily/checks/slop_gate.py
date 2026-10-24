"""The built-in slop gate: the hard AI-tell tier every shipped line must pass.

Contract (any replacement in settings.toml `checks.dir` must keep it): `scan(text)` returns [] for clean text, or
(rule, match) hits, one per distinct rule: ("banned_dash", "U+2014") for a long dash, ("ai_slop_lexeme", term) for a
listed phrase. The writer prompt names the same phrases (daily.writer rule 12), and a test keeps the two in step.

Dash code points are built from ordinals so this source stays dash-clean. A URL is not prose anyone wrote, so a phrase
inside a link (".../seamless-rollbacks") does not count.
"""
from __future__ import annotations

import re

# em (U+2014), en (U+2013), horizontal bar (U+2015)
BANNED_DASHES: tuple = (chr(0x2014), chr(0x2013), chr(0x2015))

HARD_LEXEMES: tuple = (
    "delve",
    "tapestry",
    "multifaceted",
    "seamless",
    "a myriad of",
    "it's worth noting",
    "in today's digital age",
    "testament to",
    "game-changer",
    "paradigm shift",
    "navigating the landscape",
)


def _term_pattern(term: str) -> re.Pattern:
    # word boundaries, internal whitespace collapsed to \s+ so a wrapped line cannot hide a multi-word phrase
    body = r"\s+".join(re.escape(t) for t in term.split())
    return re.compile(r"\b" + body + r"\b", re.IGNORECASE)


_TERM_PATTERNS = tuple((term, _term_pattern(term)) for term in HARD_LEXEMES)
_URL_RX = re.compile(r"https?://\S+")


def scan(text: str) -> list:
    if not text:
        return []
    text = _URL_RX.sub(" ", text)
    hits: list = []
    for dash in BANNED_DASHES:
        if dash in text:
            hits.append(("banned_dash", f"U+{ord(dash):04X}"))
    for term, pattern in _TERM_PATTERNS:
        if pattern.search(text):
            hits.append(("ai_slop_lexeme", term))
    return hits
