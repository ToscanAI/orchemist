"""Acceptance tests for scripts/check_template_sync.py.

These tests are derived ONLY from behavioral contracts in behavioral.md
(template drift lint for #867 + #869). They exercise the CLI surface of the
script via subprocess — the contract surface is "what happens when you run
this script with these inputs".

DO NOT modify these tests during the IMPLEMENT phase. They are the immutable
acceptance constraint for the implementation.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# ─────────────────────────────────────────────────────────────────────────────
# Constants & helpers
# ─────────────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_template_sync.py"
# Template paths — this is the ONE legitimate place a test references
# templates/ by filesystem path, because the script under test IS the drift
# lint for those bundled production templates. Allowlisted in
# tests/test_lint_no_templates_hardcode.py (issue #632).
_STANDARD_PATH = _REPO_ROOT / "templates" / "coding-pipeline-standard.yaml"
_SKIP_SPEC_PATH = _REPO_ROOT / "templates" / "coding-pipeline-skip-spec.yaml"


def _run_script(
    *,
    standard: Path | None = None,
    skip_spec: Path | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke scripts/check_template_sync.py with optional path overrides."""
    cmd: list[str] = [sys.executable, str(_SCRIPT_PATH)]
    if standard is not None:
        cmd += ["--standard", str(standard)]
    if skip_spec is not None:
        cmd += ["--skip-spec", str(skip_spec)]
    if extra_args:
        cmd += extra_args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _dump_yaml(data: dict, path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


def _get_phase(template: dict, phase_id: str) -> dict:
    phases = template.get("phases") or (template.get("pipeline") or {}).get("phases") or []
    for phase in phases:
        if phase.get("id") == phase_id:
            return phase
    raise KeyError(f"phase {phase_id} not found")


@pytest.fixture
def tmp_templates(tmp_path: Path) -> tuple[Path, Path]:
    """Copy the real templates into tmp_path so tests can mutate them."""
    dst_standard = tmp_path / "coding-pipeline-standard.yaml"
    dst_skip_spec = tmp_path / "coding-pipeline-skip-spec.yaml"
    shutil.copy(_STANDARD_PATH, dst_standard)
    shutil.copy(_SKIP_SPEC_PATH, dst_skip_spec)
    return dst_standard, dst_skip_spec


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight sanity
# ─────────────────────────────────────────────────────────────────────────────


def test_script_file_exists() -> None:
    """The lint script must exist at the agreed path."""
    assert _SCRIPT_PATH.exists(), f"missing script: {_SCRIPT_PATH}"


def test_template_files_exist() -> None:
    """Both production template files must exist on the feature branch."""
    assert _STANDARD_PATH.exists()
    assert _SKIP_SPEC_PATH.exists()


# ─────────────────────────────────────────────────────────────────────────────
# B1, B2, B13 — happy path
# ─────────────────────────────────────────────────────────────────────────────


def test_b1_clean_run_against_main_templates_exits_zero() -> None:
    """B1+B13: against the real templates on the feature branch, exit 0."""
    result = _run_script()
    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )


def test_b1_clean_run_stdout_mentions_ok_and_phase_counts() -> None:
    """B1: stdout contains OK and mentions strict-locked and anchored counts."""
    result = _run_script()
    assert result.returncode == 0
    out = result.stdout
    assert "OK" in out, f"expected OK in stdout, got: {out!r}"
    # Must reference at least one strict-locked phase and at least one anchored phase
    assert "strict" in out.lower()
    assert "anchor" in out.lower()


# ─────────────────────────────────────────────────────────────────────────────
# B3 — strict-locked drift detection
# ─────────────────────────────────────────────────────────────────────────────


def test_b3_strict_locked_drift_detected(tmp_templates: tuple[Path, Path]) -> None:
    """B3: a single-character change in postmortem_review on one side → non-zero."""
    standard, skip_spec = tmp_templates
    data = _load_yaml(skip_spec)
    phase = _get_phase(data, "postmortem_review")
    phase["prompt_template"] = phase["prompt_template"].replace(
        "You are a technical analyst",
        "You are a technical_analyst",  # one-char drift (space -> underscore)
        1,
    )
    _dump_yaml(data, skip_spec)

    result = _run_script(standard=standard, skip_spec=skip_spec)
    assert result.returncode != 0, (
        f"expected non-zero exit, got 0. stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "postmortem_review" in result.stderr
    assert "drift" in result.stderr.lower()


# ─────────────────────────────────────────────────────────────────────────────
# B4 — trailing-whitespace tolerance on strict-locked phase
# ─────────────────────────────────────────────────────────────────────────────


def test_b4_trailing_newline_only_diff_in_strict_exits_zero(
    tmp_templates: tuple[Path, Path],
) -> None:
    """B4: trailing newline / outer whitespace alone in strict phase → exit 0."""
    standard, skip_spec = tmp_templates
    data = _load_yaml(skip_spec)
    phase = _get_phase(data, "postmortem_review")
    # Add multiple trailing newlines — strip() should tolerate this.
    phase["prompt_template"] = phase["prompt_template"].rstrip("\n") + "\n\n\n"
    _dump_yaml(data, skip_spec)

    result = _run_script(standard=standard, skip_spec=skip_spec)
    assert result.returncode == 0, (
        f"expected exit 0 after only-outer-whitespace change in strict phase, "
        f"got {result.returncode}. stderr={result.stderr!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# B5 — anchored drift OUTSIDE the anchor → non-zero
# ─────────────────────────────────────────────────────────────────────────────


def test_b5_anchored_phase_drift_outside_anchor_detected(
    tmp_templates: tuple[Path, Path],
) -> None:
    """B5: a character change in acceptance_test outside any anchor → non-zero."""
    standard, skip_spec = tmp_templates
    data = _load_yaml(skip_spec)
    phase = _get_phase(data, "acceptance_test")
    # Mutate something that is in the shared portion (NOT inside an anchor).
    # The phrase "You are a QA engineer" is in the shared portion of both prompts.
    assert "You are a QA engineer" in phase["prompt_template"]
    phase["prompt_template"] = phase["prompt_template"].replace(
        "You are a QA engineer",
        "You are a QA-engineer",  # drift in shared region
        1,
    )
    _dump_yaml(data, skip_spec)

    result = _run_script(standard=standard, skip_spec=skip_spec)
    assert result.returncode != 0, (
        f"expected non-zero, got 0. stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "acceptance_test" in result.stderr
    assert "drift" in result.stderr.lower()


# ─────────────────────────────────────────────────────────────────────────────
# B6 — drift INSIDE anchor region → exit 0
# ─────────────────────────────────────────────────────────────────────────────


def test_b6_anchored_phase_drift_inside_anchor_tolerated(
    tmp_templates: tuple[Path, Path],
) -> None:
    """B6: arbitrary changes inside an anchor on one side are tolerated."""
    standard, skip_spec = tmp_templates
    data = _load_yaml(skip_spec)
    phase = _get_phase(data, "acceptance_test")
    # The skip-spec adversary-integration block lives between
    # <!-- ADVERSARY_FEEDBACK_INTEGRATION_BEGIN --> and END markers.
    # Mutate the block content (sandwiched between the markers).
    pt = phase["prompt_template"]
    begin = "<!-- ADVERSARY_FEEDBACK_INTEGRATION_BEGIN -->"
    end = "<!-- ADVERSARY_FEEDBACK_INTEGRATION_END -->"
    assert begin in pt, (
        "skip-spec acceptance_test must contain ADVERSARY_FEEDBACK_INTEGRATION_BEGIN "
        "anchor (inserted by the implementation)"
    )
    assert end in pt, (
        "skip-spec acceptance_test must contain ADVERSARY_FEEDBACK_INTEGRATION_END "
        "anchor (inserted by the implementation)"
    )
    # Inject arbitrary new content INSIDE the anchor block.
    pt2 = pt.replace(
        begin,
        begin + "\n      ARBITRARY GARBAGE INSIDE ANCHOR — should be elided",
        1,
    )
    assert pt2 != pt
    phase["prompt_template"] = pt2
    _dump_yaml(data, skip_spec)

    result = _run_script(standard=standard, skip_spec=skip_spec)
    assert result.returncode == 0, (
        f"expected exit 0 (content inside anchor should be elided), got "
        f"{result.returncode}. stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# B7, B7a — unbalanced & cross-named anchors
# ─────────────────────────────────────────────────────────────────────────────


def test_b7_unbalanced_anchor_begin_without_end_exits_two(
    tmp_templates: tuple[Path, Path],
) -> None:
    """B7: BEGIN without matching END → exit 2 (config error)."""
    standard, skip_spec = tmp_templates
    data = _load_yaml(skip_spec)
    phase = _get_phase(data, "acceptance_test")
    end = "<!-- ADVERSARY_FEEDBACK_INTEGRATION_END -->"
    assert end in phase["prompt_template"]
    # Delete the END marker — leaving a dangling BEGIN.
    phase["prompt_template"] = phase["prompt_template"].replace(end, "", 1)
    _dump_yaml(data, skip_spec)

    result = _run_script(standard=standard, skip_spec=skip_spec)
    assert result.returncode == 2, (
        f"expected exit 2 for unbalanced anchor, got {result.returncode}. "
        f"stderr={result.stderr!r}"
    )
    assert "ADVERSARY_FEEDBACK_INTEGRATION" in result.stderr or "unbalanced" in result.stderr.lower()


def test_b7a_cross_named_anchor_begin_foo_end_bar_exits_two(
    tmp_templates: tuple[Path, Path],
) -> None:
    """B7a: BEGIN_FOO followed by BAR_END (different names) → exit 2."""
    standard, skip_spec = tmp_templates
    data = _load_yaml(skip_spec)
    phase = _get_phase(data, "acceptance_test")
    end = "<!-- ADVERSARY_FEEDBACK_INTEGRATION_END -->"
    # Replace END marker with a differently-named END marker.
    phase["prompt_template"] = phase["prompt_template"].replace(
        end, "<!-- WRONG_NAME_END -->", 1
    )
    _dump_yaml(data, skip_spec)

    result = _run_script(standard=standard, skip_spec=skip_spec)
    assert result.returncode == 2, (
        f"expected exit 2 for cross-named anchor, got {result.returncode}. "
        f"stderr={result.stderr!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# B8 — anchor extensibility (novel names work without script edit)
# ─────────────────────────────────────────────────────────────────────────────


def test_b8_novel_anchor_name_elides_without_script_edit(
    tmp_templates: tuple[Path, Path],
) -> None:
    """B8: a brand-new anchor name on one side is elided by pattern alone."""
    standard, skip_spec = tmp_templates
    data = _load_yaml(skip_spec)
    phase = _get_phase(data, "acceptance_test")
    # Insert a never-before-seen anchor name with content INSIDE; the script
    # must elide it without any knowledge of the name.
    novel_block = (
        "<!-- BRAND_NEW_NAME_BEGIN -->\n"
        "      novel content that does not exist in standard.yaml\n"
        "<!-- BRAND_NEW_NAME_END -->\n"
    )
    pt = phase["prompt_template"]
    # Inject at the top of the prompt (above the shared content).
    phase["prompt_template"] = novel_block + pt
    _dump_yaml(data, skip_spec)

    result = _run_script(standard=standard, skip_spec=skip_spec)
    assert result.returncode == 0, (
        f"expected exit 0 (novel anchor should elide), got {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# B9 — missing configured phase → exit 2
# ─────────────────────────────────────────────────────────────────────────────


def test_b9_missing_configured_phase_exits_two(
    tmp_templates: tuple[Path, Path],
) -> None:
    """B9: if a configured drift-locked phase is absent from one template → exit 2."""
    standard, skip_spec = tmp_templates
    data = _load_yaml(skip_spec)
    # Remove the postmortem_review phase entirely from skip-spec.
    if "phases" in data:
        data["phases"] = [p for p in data["phases"] if p.get("id") != "postmortem_review"]
    else:
        data["pipeline"]["phases"] = [
            p for p in data["pipeline"]["phases"] if p.get("id") != "postmortem_review"
        ]
    _dump_yaml(data, skip_spec)

    result = _run_script(standard=standard, skip_spec=skip_spec)
    assert result.returncode == 2, (
        f"expected exit 2 for missing configured phase, got {result.returncode}. "
        f"stderr={result.stderr!r}"
    )
    assert "postmortem_review" in result.stderr


# ─────────────────────────────────────────────────────────────────────────────
# B11 — CLI path overrides honored
# ─────────────────────────────────────────────────────────────────────────────


def test_b11_explicit_paths_used_instead_of_defaults(
    tmp_templates: tuple[Path, Path],
) -> None:
    """B11: --standard and --skip-spec override default paths."""
    standard, skip_spec = tmp_templates
    # Copies of the real templates passed via flags should still pass.
    result = _run_script(standard=standard, skip_spec=skip_spec)
    assert result.returncode == 0, (
        f"expected exit 0 with explicit paths, got {result.returncode}. "
        f"stderr={result.stderr!r}"
    )


def test_b12_missing_template_file_exits_two(tmp_path: Path) -> None:
    """B12: nonexistent template path → exit 2 with explanatory message."""
    nonexistent = tmp_path / "does_not_exist.yaml"
    real = _STANDARD_PATH
    result = _run_script(standard=real, skip_spec=nonexistent)
    assert result.returncode == 2, (
        f"expected exit 2 for missing file, got {result.returncode}. "
        f"stderr={result.stderr!r}"
    )
    assert (
        "does_not_exist" in result.stderr
        or "not found" in result.stderr.lower()
        or "no such" in result.stderr.lower()
    )
