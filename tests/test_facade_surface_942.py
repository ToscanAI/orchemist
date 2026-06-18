"""Derived facade-completeness gate for the EPIC #942 god-module decomposition.

Each module that #942 converts from a single ``X.py`` file into an ``X/`` package
MUST keep re-exporting its full *public-to-the-test-suite* surface from the old
import path (the facade). The hand-maintained re-export lists in the package
``__init__`` files are the *starting point*; THIS test is the *gate*.

How it works (adversary amendment #4 — the durable safeguard):

1. Walk the entire ``tests/`` tree.
2. For every ``from <module> import <names>`` whose ``<module>`` is one of the
   decomposed facades in :data:`FACADE_MODULES` (accepting both the canonical
   ``orchestration_engine.X`` path and the worktree ``src.orchestration_engine.X``
   path), collect every imported *name* — robustly, via the ``ast`` module, so
   multi-line parenthesized imports and ``import foo as bar`` aliases are handled.
3. Assert every collected name actually resolves as an attribute of the live
   facade module.

A future extraction sub-issue (950b-e, etc.) that silently drops a re-export
fails here immediately, with the offending name + the test files that import it.

The facade list is a CONSTANT so sibling modules (db, sequencer, web/api) can be
added one line at a time as #942 decomposes them.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Dict, Set, Tuple

import pytest

# ---------------------------------------------------------------------------
# Facades under #942. Extend this tuple as each module is converted to a package
# (db -> #951, sequencer -> #953, web/api -> #952). The leading "src." alias is
# handled automatically; do NOT list it separately.
# ---------------------------------------------------------------------------
FACADE_MODULES: Tuple[str, ...] = (
    "orchestration_engine.cli",
    "orchestration_engine.web.api",
    "orchestration_engine.sequencer",
    "orchestration_engine.db",
    "orchestration_engine.daemon",
)

TESTS_DIR = Path(__file__).resolve().parent


def _canonical(module: str) -> str | None:
    """Map an imported module string to a facade in FACADE_MODULES, or None.

    Accepts both ``orchestration_engine.cli`` and the worktree-relative
    ``src.orchestration_engine.cli`` spelling (PYTHONPATH=src gotcha).
    """
    if module in FACADE_MODULES:
        return module
    if module.startswith("src."):
        stripped = module[len("src.") :]
        if stripped in FACADE_MODULES:
            return stripped
    return None


def _collect_imported_names() -> Dict[str, Dict[str, Set[str]]]:
    """Return {facade_module: {imported_name: {test_file, ...}}}.

    Parses every ``tests/*.py`` file with ``ast`` and records the *original*
    (pre-alias) name of every symbol imported from a facade module.
    """
    found: Dict[str, Dict[str, Set[str]]] = {m: {} for m in FACADE_MODULES}
    for py in sorted(TESTS_DIR.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            # ImportFrom with level>0 is a relative import (never a facade).
            if node.level:
                continue
            facade = _canonical(node.module)
            if facade is None:
                continue
            for alias in node.names:
                if alias.name == "*":  # star imports can't be name-checked
                    continue
                found[facade].setdefault(alias.name, set()).add(py.name)
    return found


@pytest.mark.parametrize("facade_module", FACADE_MODULES)
def test_facade_reexports_every_test_imported_symbol(facade_module: str) -> None:
    """Every name the test-suite imports from a facade resolves from that facade."""
    imported = _collect_imported_names()[facade_module]
    assert imported, (
        f"No test imports discovered for facade {facade_module!r}; the grep/parse "
        "step is broken (this test would silently pass and protect nothing)."
    )

    module = importlib.import_module(facade_module)
    missing = {
        name: sorted(files) for name, files in sorted(imported.items()) if not hasattr(module, name)
    }
    assert not missing, (
        f"{facade_module} no longer re-exports symbols imported by the test-suite "
        f"(facade regression — restore the re-export in the package __init__):\n"
        + "\n".join(
            f"  - {name}  (imported by: {', '.join(files)})" for name, files in missing.items()
        )
    )


def test_cli_facade_includes_known_private_surface() -> None:
    """Belt-and-suspenders: the documented cli private surface stays importable.

    This pins the 950a-era list explicitly so a regression is obvious in review
    even if a coupling test file were ever deleted.
    """
    cli = importlib.import_module("orchestration_engine.cli")
    required = {
        "main",
        "_validate_required_config",
        "_read_openclaw_token",
        "_watch_pipeline_run",
        "_print_watch_event",
        "_print_run_detail",
        "_safe_write_phase_output",
        "_is_github_shorthand",
        "_install_from_git",
        "_find_yaml_in_dir",
        "_apply_fixes",
        "_check_yaml_syntax",
        "_normalize_git_url",
        "_slugify_title",
    }
    missing = sorted(name for name in required if not hasattr(cli, name))
    assert not missing, f"cli facade dropped required symbols: {missing}"
