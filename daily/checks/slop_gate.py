"""The built-in slop gate: the hard AI-tell tier every shipped line must pass.

Contract (any replacement in settings.toml `checks.dir` must keep it): `scan(text)` returns [] for clean text, or
(rule, match) hits, one per distinct rule: ("banned_dash", "U+2014") for a long dash, ("ai_slop_lexeme", term) for a
listed phrase. The writer prompt names the same phrases (daily.writer rule 12), and a test keeps the two in step.

The lists and the matcher come from `_orion_creative_adapter.py` next to this file: a generated, standard-library
only, self-verifying copy of the shared creative kit's hard tier. It is loaded by file location under a private name
(nothing is added to sys.path or sys.modules), and a missing or hand-edited adapter raises ImportError, so the checks
are unavailable and the caller fails closed. Never edit the adapter; regenerate it.

Dash code points are written as integers so this source stays dash-clean. A URL is not prose anyone wrote, so a
phrase or a dash inside a link (".../seamless-rollbacks") does not count.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_ADAPTER_FILE = "_orion_creative_adapter.py"


def _load_adapter():
    path = Path(__file__).resolve().with_name(_ADAPTER_FILE)
    if not path.is_file():
        raise ImportError("shared creative adapter missing")
    spec = importlib.util.spec_from_file_location("_daily_orion_creative_adapter", path)
    if spec is None or spec.loader is None:
        raise ImportError("shared creative adapter cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not module.verify_self():
        raise ImportError("shared creative adapter failed its integrity check")
    return module


_ADAPTER = _load_adapter()

# em (U+2014), en (U+2013), horizontal bar (U+2015)
BANNED_DASHES: tuple = tuple(chr(cp) for cp in (0x2014, 0x2013, 0x2015))

HARD_LEXEMES: tuple = tuple(_ADAPTER.HARD_LEXEMES)


def scan(text: str) -> list:
    return _ADAPTER.scan_hard_tier(text, dashes_on_raw_text=False)
