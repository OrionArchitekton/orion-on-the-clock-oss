"""A text-only install: numpy and Pillow are the optional [media] extra, so collect, write and gate must import and run
without them. Collect clears what the media stages made from the old ledger through a module with no media imports,
bound when collect is imported, so it can never delete the old ledger and then fail to load the clear."""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from daily import paths
from test_cli import DAY, world

MEDIA = {"numpy", "PIL", "daily.render", "daily.mix", "daily.voice", "daily.compose", "daily.align", "daily.sheet",
         "daily.tts_runner", "daily.tts_remote", "daily.whisper_words"}

DRIVER = textwrap.dedent('''
    import json, runpy, sys
    sys.modules["numpy"] = None   # `import numpy` now raises ImportError, as in an install without the extra
    sys.modules["PIL"] = None
    sys.argv = ["daily.run", *sys.argv[1:]]
    code = None
    try:
        runpy.run_module("daily.run", run_name="__main__", alter_sys=True)
    except SystemExit as e:
        code = e.code
    print(json.dumps({"code": code, "loaded": sorted(k for k, v in sys.modules.items() if v is not None)}))
''')


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git for the repo lookup")
def test_a_dry_run_through_the_gate_needs_no_media_extra_and_replaces_the_old_text(tmp_path):
    w = world(tmp_path)
    dry = w["state"] / f"{DAY}-dry"
    dry.mkdir(parents=True)
    for name in ("ledger.json", "script.json", "verdict.json"):
        (dry / name).write_text('{"old": true}')   # an earlier rehearsal's text: replaced, never just deleted
    driver = tmp_path / "no_media.py"
    driver.write_text(DRIVER)
    done = subprocess.run([sys.executable, str(driver), "--day", DAY, "--until", "gate", "--dry"], env=w["env"],
                          cwd=tmp_path, capture_output=True, text=True, timeout=120)
    out = json.loads(done.stdout.strip().splitlines()[-1])
    status = json.loads((dry / "status.json").read_text())["stages"]
    assert out["code"] == 0, (status, done.stderr)
    assert {k: v["state"] for k, v in status.items()} == {"collect": "dry", "write": "dry", "gate": "dry"}
    assert sorted(MEDIA & set(out["loaded"])) == []   # not one media module was even imported
    assert json.loads((dry / "ledger.json").read_text())["day"] == DAY
    assert json.loads((dry / "verdict.json").read_text())["pass"] is True


def _imports(path: Path) -> tuple[set, set]:
    """(module-level imports, imports inside functions) of one daily source file, as dotted module names."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    top, inner = set(), set()
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    nested = {id(x) for f in funcs for x in ast.walk(f) if isinstance(x, (ast.Import, ast.ImportFrom))}
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:   # `from daily.x import y` may import module daily.x.y
            names = [node.module] + ([f"{node.module}.{a.name}" for a in node.names]
                                     if node.module.split(".")[0] == "daily" else [])
        (inner if id(node) in nested else top).update(names)
    return top, inner


def _closure(module: str) -> set:
    """Every daily module (and third-party top-level package) `module` imports at module level, transitively."""
    seen, todo = set(), [module]
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = paths.REPO / (mod.replace(".", "/") + ".py")
        if not path.is_file():
            path = paths.REPO / mod.replace(".", "/") / "__init__.py"   # a package
        if not path.is_file():
            continue   # an attribute, not a module
        top, _inner = _imports(path)
        for name in top:
            if name.startswith("daily."):
                todo.append(name)
            elif name != "daily" and name.split(".")[0] not in sys.stdlib_module_names:
                seen.add(name.split(".")[0])
    return seen


def test_collect_binds_the_media_free_clear_when_it_is_imported():
    top, inner = _imports(paths.PACKAGE / "collect.py")
    assert "daily.downstream" in top          # bound before run_stage can delete anything
    assert inner == set()                     # no import is left to fail after the old ledger is gone
    assert not (MEDIA & _closure("daily.collect"))


@pytest.mark.parametrize("module", ["daily.downstream", "daily.collect", "daily.writer", "daily.gate", "daily.run"])
def test_the_text_half_imports_nothing_from_the_media_half(module):
    assert not (MEDIA & _closure(module)), sorted(MEDIA & _closure(module))


def test_the_closure_check_sees_the_media_half_where_it_is():
    assert {"daily.mix", "numpy"} <= _closure("daily.render")   # positive control
