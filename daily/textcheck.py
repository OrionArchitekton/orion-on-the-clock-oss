"""The text gate's redactor, slop and secret checks, for use before the gate: the collect stage keeps only text they
all pass, so the writer (and the model provider behind it) only ever sees text the gate could ship: a credential-like
value in a PR title, a PR body line or a quote is dropped at collection, never sent in the prompt. The gate loads the
same modules the same way.

The checks are three modules, `redactor.py` (`decision`), `slop_gate.py` (`scan`) and `secret_scan.py` (`scan_output`,
given settings.toml `checks.public_hosts`), loaded lazily by file path from one folder: settings.toml `checks.dir` when the operator supplies their own, else the built-in `daily/checks/`. They load
under private module names, so sys.path and sys.modules end up as they were and no module of the same name elsewhere
(a shadow on sys.path, an earlier import) can stand in. A file that is missing, linked from outside that folder, fails
to load or lacks its function raises TextcheckUnavailable: the caller fails closed. The operator's own
`checks.redact_patterns`, and a pattern for every branch prefix in `github.harness_prefixes`, are added to the
redactor's verdict, never in place of it. Such a hit reports a fixed class (`operator_pattern`, `internal-ref`), never
the pattern's name or the matched text, so a private word cannot travel into a verdict through its pattern's name.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from daily import paths
from daily.settings import DEFAULT_PUBLIC_HOSTS


# Dropped from the normal form: combining marks (Mn, Me: accents, variation selectors, U+034F, U+0332) and format
# characters (Cf), these zero-width ones by name included.
INVISIBLE = frozenset({0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD})
DROPPED_CATEGORIES = frozenset({"Mn", "Me", "Cf"})
# Dash look-alikes (box-drawing and line-extension horizontals included), mapped to the long dash the slop gate blocks
# so they are blocked too.
DASH_LOOKALIKES = frozenset({*range(0x2010, 0x2016), 0xFE58, 0xFE31, 0xFE32, 0x2E3A, 0x2E3B, 0x2212,
                             0x2500, 0x2501, 0x2504, 0x2505, 0x2508, 0x2509, 0x23AF})
_TO_LONG_DASH = {c: "\u2014" for c in DASH_LOOKALIKES}


def _visible(text: str) -> str:
    return "".join(ch for ch in text
                   if ord(ch) not in INVISIBLE and unicodedata.category(ch) not in DROPPED_CATEGORIES)


def normal_form(text: str) -> str:
    """text decomposed (NFKD), with combining marks and zero-width and format characters dropped, recomposed (NFKC),
    and every dash look-alike turned into the long dash: an invisible split ("Her" ZWSP "mes"), a variation selector
    or combining mark inside a word, a fullwidth spelling or a look-alike dash cannot hide a hit in this form. Accents
    go too ("cafe"); the raw form is always checked as well."""
    return unicodedata.normalize("NFKC", _visible(unicodedata.normalize("NFKD", text))).translate(_TO_LONG_DASH)


def forms(text: str) -> tuple:
    """What every text check reads: the raw text, then its normal form when that differs. A hit on either fails."""
    normal = normal_form(text)
    return (text,) if normal == text else (text, normal)


class TextcheckUnavailable(Exception):
    """The redactor, the slop gate or the secret scanner cannot be loaded from the checks folder."""


@dataclass(frozen=True)
class TextChecks:
    redactor_decision: Callable  # text -> ("PUBLISH", []) or ("ABSTAIN", hits)
    slop_scan: Callable  # text -> [] when clean, else hits
    secrets_scan: Callable  # text -> [] when clean, else findings (the gate's secret scanner, with the public hosts)

    def ok(self, text: str) -> bool:
        """True only for a PUBLISH verdict with no hits, no slop hit and no secret finding, on the raw text and on its
        normal form. A secret scanner that answers anything but a list or tuple is not a clean answer."""
        for form in forms(text):
            verdict, hits = self.redactor_decision(form)
            if verdict != "PUBLISH" or hits or self.slop_scan(form):
                return False
            found = self.secrets_scan(form)
            if not isinstance(found, (list, tuple)) or found:
                return False
        return True


def load_module(path: Path, name: str):
    """The file at path, executed as the private module _daily_<name>, so no module of the same name elsewhere (a
    shadow on sys.path, an earlier import) can stand in. The private name sits in sys.modules only while the module
    runs (a dataclass with postponed annotations looks its own module up there as the class is built); afterwards it
    is removed, or whatever held it before is put back. Raises TextcheckUnavailable when path is not a regular file or
    resolves outside its own folder (a link to a file elsewhere); an error raised while the module runs propagates for
    the caller to judge."""
    path = Path(path)
    if not path.is_file() or path.resolve().parent != path.parent.resolve():
        raise TextcheckUnavailable("textcheck_unavailable")  # missing, or a link to a file outside the folder
    private = f"_daily_{name}"
    spec = importlib.util.spec_from_file_location(private, path)
    mod = importlib.util.module_from_spec(spec)
    earlier = sys.modules.get(private)
    sys.modules[private] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        if earlier is None:
            sys.modules.pop(private, None)
        else:
            sys.modules[private] = earlier
    return mod


def checks_dir(settings=None) -> Path:
    """The folder the checks load from: the operator's `checks.dir`, or the built-in one (paths.CHECKS_DIR)."""
    folder = getattr(settings, "checks_dir", None)
    return Path(folder) if folder is not None else Path(paths.CHECKS_DIR)


def with_patterns(decision: Callable, patterns) -> Callable:
    """The redactor's decision with extra patterns (class, compiled regex) added: a hit on either side is ABSTAIN, and
    an extra hit reports its class only, never the matched text."""
    patterns = tuple(patterns or ())
    if not patterns:
        return decision

    def combined(text: str):
        verdict, hits = decision(text)
        extra = [(cls, "withheld") for cls, rx in patterns if rx.search(text)]
        if not extra:
            return verdict, hits
        return "ABSTAIN", [*(hits if isinstance(hits, (list, tuple)) else [("redactor", "withheld")]), *extra]
    return combined


def load_pair(settings=None) -> tuple:
    """(redactor decision with the operator's extra patterns, slop scan), or TextcheckUnavailable."""
    folder = checks_dir(settings)
    try:
        decision = load_module(folder / "redactor.py", "redactor").decision
        scan = load_module(folder / "slop_gate.py", "slop_gate").scan
    except Exception as e:  # missing or outside the folder, broken on load, no such function
        raise TextcheckUnavailable("textcheck_unavailable") from e
    if not (callable(decision) and callable(scan)):
        raise TextcheckUnavailable("textcheck_unavailable")
    return with_patterns(decision, extra_patterns(settings)), scan


def load_secrets(settings=None) -> Callable:
    """The secret scanner (`secret_scan.scan_output`) bound to settings.toml `checks.public_hosts` (absent: the default
    YouTube hosts; an empty list: no host at all), or TextcheckUnavailable."""
    hosts = getattr(settings, "public_hosts", None)
    hosts = DEFAULT_PUBLIC_HOSTS if hosts is None else tuple(hosts)
    try:
        scan = load_module(checks_dir(settings) / "secret_scan.py", "secret_scan").scan_output
    except Exception as e:  # missing, linked from outside the folder, broken on load, no scan_output
        raise TextcheckUnavailable("textcheck_unavailable") from e
    if not callable(scan):
        raise TextcheckUnavailable("textcheck_unavailable")

    def secrets(text):
        return scan(text, public_hosts=hosts)
    return secrets


def load(settings=None) -> TextChecks:
    """All three checks, or TextcheckUnavailable when any one cannot be loaded (collect fails closed)."""
    decision, scan = load_pair(settings)
    return TextChecks(decision, scan, load_secrets(settings))


BUILT_IN_PREFIXES = ("claude/", "codex/")  # branch names the built-in redactor already refuses


def extra_patterns(settings) -> tuple:
    """(class, regex) for every operator pattern and every configured branch prefix beyond the built-in ones."""
    out = [("operator_pattern", rx) for _name, rx in getattr(settings, "redact_patterns", ()) or ()]
    for prefix, _harness in getattr(settings, "harness_prefixes", ()) or ():
        if prefix not in BUILT_IN_PREFIXES:
            out.append(("internal-ref", re.compile(r"(?<![\w/-])" + re.escape(prefix) + r"[A-Za-z0-9._/-]+", re.I)))
    return tuple(out)
