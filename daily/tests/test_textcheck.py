"""textcheck: the gate's redactor and slop checks as a prefilter, loaded lazily from the checks folder (fail closed)."""
import re
import sys
import types
from types import SimpleNamespace

import pytest

from daily import paths, textcheck
from daily.textcheck import TextChecks, TextcheckUnavailable

CLEAN = "The new browser pool shipped today and every single test passed."


def checks(redactor_hits=(), slop_hits=(), verdict=None):
    return TextChecks(lambda t: (verdict or ("ABSTAIN" if redactor_hits else "PUBLISH"), list(redactor_hits)),
                      lambda t: list(slop_hits))


def test_clean_text_passes():  # positive control
    assert checks().ok(CLEAN)


def test_a_redactor_abstain_fails():
    assert not checks(redactor_hits=[("host", "ni**us")]).ok(CLEAN)


def test_a_publish_verdict_with_hits_still_fails():
    assert not checks(redactor_hits=[("host", "ni**us")], verdict="PUBLISH").ok(CLEAN)


def test_any_other_verdict_fails():
    assert not checks(verdict="MAYBE").ok(CLEAN)


def test_a_slop_hit_fails():
    assert not checks(slop_hits=[("ai_slop_lexeme", "delve")]).ok(CLEAN)


def test_the_checks_see_the_exact_text():
    seen = []
    TextChecks(lambda t: (seen.append(("r", t)), ("PUBLISH", []))[1],
               lambda t: (seen.append(("s", t)), [])[1]).ok(CLEAN)
    assert seen == [("r", CLEAN), ("s", CLEAN)]


SHADOWED = ("redactor", "slop_gate")


@pytest.fixture
def clean_sys_path(monkeypatch):
    """sys.path and the two plain module names come back exactly as they were, whatever a test imported."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    saved = {name: sys.modules.get(name) for name in SHADOWED}
    yield
    for name, mod in saved.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


def fake_modules(folder, verdict="PUBLISH", slop="['x'] if 'delve' in t else []"):
    folder.mkdir(exist_ok=True)
    (folder / "redactor.py").write_text(f"def decision(t):\n    return ({verdict!r}, [])\n")
    (folder / "slop_gate.py").write_text(f"def scan(t):\n    return {slop}\n")


def test_a_missing_checks_folder_is_unavailable(clean_sys_path, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "CHECKS_DIR", tmp_path / "nope")
    with pytest.raises(TextcheckUnavailable, match="^textcheck_unavailable$"):
        textcheck.load()


@pytest.mark.parametrize("broken", [{"redactor.py": "def other(t):\n    return 1\n"},
                                    {"slop_gate.py": "raise ImportError('half written')\n"}],
                         ids=["no-decision", "raises-on-load"])
def test_a_broken_module_is_unavailable(clean_sys_path, monkeypatch, tmp_path, broken):
    fake_modules(tmp_path / "bp")
    for name, text in broken.items():
        (tmp_path / "bp" / name).write_text(text)
    monkeypatch.setattr(paths, "CHECKS_DIR", tmp_path / "bp")
    with pytest.raises(TextcheckUnavailable, match="^textcheck_unavailable$"):
        textcheck.load()


def test_modules_from_the_checks_folder_load(clean_sys_path, monkeypatch, tmp_path):  # positive control
    fake_modules(tmp_path / "bp")
    monkeypatch.setattr(paths, "CHECKS_DIR", tmp_path / "bp")
    got = textcheck.load()
    assert got.ok(CLEAN) and not got.ok("We delve into it.")


def test_loading_leaves_sys_path_and_the_plain_module_names_alone(clean_sys_path, monkeypatch, tmp_path):
    fake_modules(tmp_path / "bp")
    monkeypatch.setattr(paths, "CHECKS_DIR", tmp_path / "bp")
    for name in SHADOWED:
        sys.modules.pop(name, None)
    before = list(sys.path)
    textcheck.load()
    assert sys.path == before and not set(SHADOWED) & set(sys.modules)


def test_a_shadow_module_is_never_used(clean_sys_path, monkeypatch, tmp_path):
    """A redactor shadowing the real one on sys.path, or already imported under the plain name, is not the gate's
    check: only the checks folder's files are loaded, and without them there is no check at all."""
    fake_modules(tmp_path / "shadow", slop="[]")  # passes everything, slop included
    monkeypatch.syspath_prepend(str(tmp_path / "shadow"))
    for name in SHADOWED:
        sys.modules.pop(name, None)
    import redactor  # noqa: F401  (now imported under the plain name, from the shadow)
    import slop_gate  # noqa: F401
    monkeypatch.setattr(paths, "CHECKS_DIR", tmp_path / "elsewhere")
    with pytest.raises(TextcheckUnavailable, match="^textcheck_unavailable$"):
        textcheck.load()  # the folder is empty: the shadow is not a fallback
    fake_modules(tmp_path / "elsewhere")
    assert not textcheck.load().ok("We delve into it.")  # positive control: the folder's slop gate, not the shadow's


def test_a_module_linked_from_outside_the_folder_is_refused(clean_sys_path, monkeypatch, tmp_path):
    fake_modules(tmp_path / "outside")
    (tmp_path / "bp").mkdir()
    (tmp_path / "bp" / "redactor.py").symlink_to(tmp_path / "outside" / "redactor.py")
    (tmp_path / "bp" / "slop_gate.py").write_text((tmp_path / "outside" / "slop_gate.py").read_text())
    monkeypatch.setattr(paths, "CHECKS_DIR", tmp_path / "bp")
    with pytest.raises(TextcheckUnavailable, match="^textcheck_unavailable$"):
        textcheck.load()


def test_the_built_in_checks_load_and_judge(clean_sys_path):
    real = textcheck.load()
    assert real.ok(CLEAN)  # positive control
    assert not real.ok("We delve into the browser pool and every single test passed.")  # slop lexeme
    assert not real.ok("The token " + "A" * 44 + " leaked into the log today.")  # secret-shaped


def test_load_module_loads_a_file_under_a_private_name(clean_sys_path, tmp_path):  # positive control
    fake_modules(tmp_path / "bp")
    mod = textcheck.load_module(tmp_path / "bp" / "slop_gate.py", "slop_gate")
    assert mod.scan("We delve.") == ["x"] and mod.__name__ == "_daily_slop_gate"
    assert "slop_gate" not in sys.modules and "_daily_slop_gate" not in sys.modules


DATACLASS_MODULE = """from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class Line:
    text: str


def first(text):
    return Line(text).text
"""


def test_load_module_runs_a_module_whose_dataclass_looks_itself_up(clean_sys_path, tmp_path):
    """A dataclass with postponed annotations reads its own module from sys.modules while the class is built (the
    adapters may have one): the private name is there while the module runs, and gone, or put back, afterwards."""
    (tmp_path / "lint.py").write_text(DATACLASS_MODULE)
    mod = textcheck.load_module(tmp_path / "lint.py", "lint")
    assert mod.first("ok") == "ok" and "_daily_lint" not in sys.modules
    earlier = types.ModuleType("_daily_lint")
    sys.modules["_daily_lint"] = earlier
    try:
        assert textcheck.load_module(tmp_path / "lint.py", "lint") is not earlier
        assert sys.modules["_daily_lint"] is earlier
    finally:
        sys.modules.pop("_daily_lint", None)


def test_load_module_unregisters_a_module_that_fails_while_it_runs(clean_sys_path, tmp_path):
    (tmp_path / "broken.py").write_text("raise RuntimeError('broken on load')\n")
    with pytest.raises(RuntimeError):
        textcheck.load_module(tmp_path / "broken.py", "broken")
    assert "_daily_broken" not in sys.modules


@pytest.mark.parametrize("how", ["missing", "linked", "folder"])
def test_load_module_refuses_a_missing_linked_or_non_file(clean_sys_path, tmp_path, how):
    fake_modules(tmp_path / "outside")
    target = tmp_path / "bp" / "redactor.py"
    target.parent.mkdir()
    if how == "linked":
        target.symlink_to(tmp_path / "outside" / "redactor.py")
    elif how == "folder":
        target.mkdir()
    with pytest.raises(TextcheckUnavailable, match="^textcheck_unavailable$"):
        textcheck.load_module(target, "redactor")


# ---- the normal form: what the checks also read ----

@pytest.mark.parametrize("raw,normal", [
    ("Nim\u200bbus", "Nimbus"), ("Nim\u2060bus\ufeff", "Nimbus"), ("Zeph\u00adyr", "Zephyr"),
    ("\uff2e\uff49\uff4d\uff42\uff55\uff53", "Nimbus"), ("a \u2012 b", "a \u2014 b"), ("a \ufe58 b", "a \u2014 b"),
    ("a \u2212 b", "a \u2014 b"), ("a \u2e3b b", "a \u2014 b"), ("log-first", "log-first"), (CLEAN, CLEAN)])
def test_normal_form(raw, normal):
    assert textcheck.normal_form(raw) == normal
    assert textcheck.forms(raw) == ((raw,) if raw == normal else (raw, normal))


def test_ok_fails_a_hit_found_only_in_the_normal_form():
    flags = TextChecks(lambda t: ("ABSTAIN", [("host", "xx")]) if "Nimbus" in t else ("PUBLISH", []),
                       lambda t: [("banned_dash", "U+2014")] if "\u2014" in t else [])
    assert flags.ok(CLEAN)  # positive control
    assert not flags.ok("We thank Nim\u200bbus today.") and not flags.ok("A long day \u2012 done.")



@pytest.mark.parametrize("raw,normal", [
    ("Nim" + chr(0xFE0F) + "bus", "Nimbus"), ("Nim" + chr(0x034F) + "bus", "Nimbus"),
    ("Nim" + chr(0x0332) + "bus", "Nimbus"), ("ghp_" + chr(0xE0101) + "x", "ghp_x"), ("caf" + chr(0xE9), "cafe"),
    ("a " + chr(0x2500) + " b", "a " + chr(0x2014) + " b"), ("a " + chr(0x23AF) + " b", "a " + chr(0x2014) + " b"),
    ("a " + chr(0x2509) + " b", "a " + chr(0x2014) + " b")])
def test_normal_form_drops_combining_marks_and_maps_box_dashes(raw, normal):
    assert textcheck.normal_form(raw) == normal


# ---- the operator's checks folder and extra patterns (settings.toml [checks]) ----

def test_settings_can_point_at_their_own_checks_folder(clean_sys_path, tmp_path):
    fake_modules(tmp_path / "mine", verdict="ABSTAIN")
    mine = textcheck.load(SimpleNamespace(checks_dir=tmp_path / "mine", redact_patterns=()))
    assert not mine.ok(CLEAN)  # their redactor, which refuses everything, not the built-in one
    assert textcheck.load(SimpleNamespace(checks_dir=None, redact_patterns=())).ok(CLEAN)  # positive control


def test_a_missing_operator_folder_is_unavailable_never_the_built_in_fallback(clean_sys_path, tmp_path):
    with pytest.raises(TextcheckUnavailable, match="^textcheck_unavailable$"):
        textcheck.load(SimpleNamespace(checks_dir=tmp_path / "nope", redact_patterns=()))


def test_extra_patterns_add_to_the_redactor_and_never_echo_the_match(clean_sys_path):
    pats = (("machine", re.compile(r"\bbox-\d+\b", re.I)),)
    got = textcheck.load(SimpleNamespace(checks_dir=None, redact_patterns=pats))
    assert got.ok(CLEAN)  # positive control
    assert not got.ok("The agent restarted BOX-7 before lunch and every test passed.")
    verdict, hits = got.redactor_decision("The agent restarted box-7 today.")
    assert verdict == "ABSTAIN" and hits == [("operator_pattern", "withheld")]  # never the pattern's own name


def test_a_custom_branch_prefix_is_refused_like_the_built_in_ones(clean_sys_path):
    mine = SimpleNamespace(checks_dir=None, redact_patterns=(), harness_prefixes=(("bot/", "codex"),))
    got = textcheck.load(mine)
    assert got.ok(CLEAN) and not got.ok("The bot/fix-login branch merged cleanly and every test passed.")
    assert textcheck.load(SimpleNamespace(checks_dir=None, redact_patterns=())).ok(
        "The bot/fix-login branch merged cleanly and every test passed.")  # positive control: the prefix is what blocks


def test_extra_patterns_never_soften_a_redactor_hit():
    decision = textcheck.with_patterns(lambda t: ("ABSTAIN", [("secret", "gh**xx")]), ())
    assert decision("anything") == ("ABSTAIN", [("secret", "gh**xx")])
    both = textcheck.with_patterns(lambda t: ("ABSTAIN", [("secret", "gh**xx")]),
                                   (("operator_pattern", re.compile("box")),))
    assert both("a box") == ("ABSTAIN", [("secret", "gh**xx"), ("operator_pattern", "withheld")])
