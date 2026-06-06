"""Tests for the cross-repo parity mode of scripts/check_template_sync.py (#917).

The engine is canonical for the shared coding-pipeline prompts; the standalone
copies in ToscanAI/orchemist-skills (``pipelines/*.yaml``) must not silently
drift. These tests exercise the ``--skills-dir`` CLI surface of the drift lint:

* graceful-skip when the skills directory is absent (always runnable),
* a clean cross-repo run against the real sibling skills checkout
  (skipif-guarded — only runs when skills is present as a sibling),
* drift detection on a STRICT cross-repo phase (temp copies),
* anchored-region tolerance (temp copies),
* the ``spec_adversary.model_tier == "opus"`` metadata lock (temp copies).

The temp-copy tests mutate copies of BOTH repos' files and point the engine at
the temp engine dir via ``ORCH_TEMPLATES_PATH`` so the skip-spec ``extends:``
resolves locally (same mechanism as tests/test_check_template_sync.py).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_template_sync.py"
_ENGINE_TEMPLATES = _REPO_ROOT / "templates"

_PIPELINES = (
    "coding-pipeline-standard.yaml",
    "coding-pipeline-skip-spec.yaml",
    "coding-pipeline-maintenance.yaml",
)


def _resolve_skills_pipelines() -> Path | None:
    """Resolve the skills ``pipelines/`` dir: $ORCHEMIST_SKILLS_DIR > sibling."""
    env = os.environ.get("ORCHEMIST_SKILLS_DIR")
    if env:
        return Path(env)
    sibling = _REPO_ROOT.parent / "orchemist-skills" / "pipelines"
    return sibling


_SKILLS_PIPELINES = _resolve_skills_pipelines()
_SKILLS_PRESENT = _SKILLS_PIPELINES is not None and _SKILLS_PIPELINES.is_dir()
_skipif_no_skills = pytest.mark.skipif(
    not _SKILLS_PRESENT,
    reason="orchemist-skills not checked out as sibling (or via $ORCHEMIST_SKILLS_DIR)",
)


def _run_script(
    *,
    standard: Path | None = None,
    skip_spec: Path | None = None,
    skills_dir: Path | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke check_template_sync.py with optional engine/skills path overrides.

    When ``standard``/``skip_spec`` point at mutated temp copies, the temp dir
    is prepended to ``ORCH_TEMPLATES_PATH`` so the engine resolves the child's
    ``extends:`` to the temp copy (mirrors tests/test_check_template_sync.py).
    """
    cmd: list[str] = [sys.executable, str(_SCRIPT_PATH)]
    if standard is not None:
        cmd += ["--standard", str(standard)]
    if skip_spec is not None:
        cmd += ["--skip-spec", str(skip_spec)]
    if skills_dir is not None:
        cmd += ["--skills-dir", str(skills_dir)]
    if extra_args:
        cmd += extra_args
    env = os.environ.copy()
    # Do not let an ambient env var override an explicit --skills-dir under test.
    env.pop("ORCHEMIST_SKILLS_DIR", None)
    if standard is not None or skip_spec is not None:
        tmp_dirs: list[str] = []
        if standard is not None:
            tmp_dirs.append(str(standard.parent))
        if skip_spec is not None and (
            standard is None or skip_spec.parent != standard.parent
        ):
            tmp_dirs.append(str(skip_spec.parent))
        if tmp_dirs:
            existing = env.get("ORCH_TEMPLATES_PATH", "")
            env["ORCH_TEMPLATES_PATH"] = ":".join(
                tmp_dirs + ([existing] if existing else [])
            )
    return subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(_REPO_ROOT), env=env
    )


def _copy_engine_templates(dst: Path) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for name in _PIPELINES:
        shutil.copy(_ENGINE_TEMPLATES / name, dst / name)
    return dst


def _copy_skills_pipelines(dst: Path) -> Path:
    assert _SKILLS_PIPELINES is not None
    dst.mkdir(parents=True, exist_ok=True)
    for name in _PIPELINES:
        shutil.copy(_SKILLS_PIPELINES / name, dst / name)
    return dst


def _load(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _dump(data: dict, path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


# ─────────────────────────────────────────────────────────────────────────────
# Graceful skip — always runnable (does NOT require skills present)
# ─────────────────────────────────────────────────────────────────────────────


def test_graceful_skip_when_skills_dir_absent(tmp_path: Path) -> None:
    """A bogus --skills-dir → exit 0 + a 'skipped' stdout message."""
    bogus = tmp_path / "no_such_skills" / "pipelines"
    result = _run_script(skills_dir=bogus)
    assert result.returncode == 0, (
        f"graceful skip must exit 0, got {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "cross-repo parity check skipped" in result.stdout


def test_graceful_skip_still_runs_intra_engine(tmp_path: Path) -> None:
    """Even when cross-repo skips, the intra-engine OK line is still printed."""
    bogus = tmp_path / "no_such_skills" / "pipelines"
    result = _run_script(skills_dir=bogus)
    assert result.returncode == 0
    assert "OK:" in result.stdout
    assert "strict" in result.stdout.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Clean cross-repo run — skipif-guarded (needs the reconciled skills checkout)
# ─────────────────────────────────────────────────────────────────────────────


@_skipif_no_skills
def test_clean_cross_repo_run_exits_zero() -> None:
    """Against the real sibling skills checkout, cross-repo parity exits 0."""
    result = _run_script(skills_dir=_SKILLS_PIPELINES)
    assert result.returncode == 0, (
        f"expected exit 0 against reconciled skills, got {result.returncode}.\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "cross-repo parity in sync" in result.stdout
    assert "model_tier locks OK" in result.stdout


# ─────────────────────────────────────────────────────────────────────────────
# Drift detection on a STRICT cross-repo phase — skipif-guarded, temp copies
# ─────────────────────────────────────────────────────────────────────────────


@_skipif_no_skills
def test_strict_phase_drift_detected(tmp_path: Path) -> None:
    """Mutating a STRICT cross-repo phase (spec) on the skills copy → exit 1."""
    eng_dir = _copy_engine_templates(tmp_path / "engine")
    sk_dir = _copy_skills_pipelines(tmp_path / "skills")

    sk_std = sk_dir / "coding-pipeline-standard.yaml"
    data = _load(sk_std)
    phase = next(p for p in data["phases"] if p["id"] == "spec")
    before = phase["prompt_template"]
    phase["prompt_template"] = before + "\n\nDRIFT INJECTED — not in engine."
    assert phase["prompt_template"] != before
    _dump(data, sk_std)

    result = _run_script(
        standard=eng_dir / "coding-pipeline-standard.yaml",
        skip_spec=eng_dir / "coding-pipeline-skip-spec.yaml",
        skills_dir=sk_dir,
    )
    assert result.returncode == 1, (
        f"expected exit 1 for strict drift, got {result.returncode}. "
        f"stderr={result.stderr!r}"
    )
    assert "spec" in result.stderr
    assert "drift" in result.stderr.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Anchored-region tolerance — skipif-guarded, temp copies
# ─────────────────────────────────────────────────────────────────────────────


@_skipif_no_skills
def test_anchored_region_mutation_tolerated(tmp_path: Path) -> None:
    """Mutating INSIDE a cross-repo anchor (skills MULTI_LANG body) → exit 0."""
    eng_dir = _copy_engine_templates(tmp_path / "engine")
    sk_dir = _copy_skills_pipelines(tmp_path / "skills")

    sk_std = sk_dir / "coding-pipeline-standard.yaml"
    data = _load(sk_std)
    phase = next(p for p in data["phases"] if p["id"] == "acceptance_test")
    pt = phase["prompt_template"]
    begin = "<!-- MULTI_LANG_BEGIN -->"
    assert begin in pt, "skills acceptance_test must carry a MULTI_LANG anchor"
    phase["prompt_template"] = pt.replace(
        begin,
        begin + "\n      ARBITRARY MUTATION INSIDE MULTI_LANG — elided",
        1,
    )
    _dump(data, sk_std)

    result = _run_script(
        standard=eng_dir / "coding-pipeline-standard.yaml",
        skip_spec=eng_dir / "coding-pipeline-skip-spec.yaml",
        skills_dir=sk_dir,
    )
    assert result.returncode == 0, (
        f"expected exit 0 (anchored mutation elided), got {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# model_tier lock (MINOR-5) — skipif-guarded, temp copies
# ─────────────────────────────────────────────────────────────────────────────


@_skipif_no_skills
def test_model_tier_lock_violation_detected(tmp_path: Path) -> None:
    """spec_adversary.model_tier opus→sonnet on the skills copy → exit 1."""
    eng_dir = _copy_engine_templates(tmp_path / "engine")
    sk_dir = _copy_skills_pipelines(tmp_path / "skills")

    sk_std = sk_dir / "coding-pipeline-standard.yaml"
    data = _load(sk_std)
    phase = next(p for p in data["phases"] if p["id"] == "spec_adversary")
    assert phase.get("model_tier") == "opus", (
        "precondition: reconciled skills spec_adversary should be opus"
    )
    phase["model_tier"] = "sonnet"
    _dump(data, sk_std)

    result = _run_script(
        standard=eng_dir / "coding-pipeline-standard.yaml",
        skip_spec=eng_dir / "coding-pipeline-skip-spec.yaml",
        skills_dir=sk_dir,
    )
    assert result.returncode == 1, (
        f"expected exit 1 for model_tier lock violation, got {result.returncode}. "
        f"stderr={result.stderr!r}"
    )
    assert "model_tier" in result.stderr
    assert "spec_adversary" in result.stderr


@_skipif_no_skills
def test_model_tier_lock_passes_when_opus(tmp_path: Path) -> None:
    """With skills spec_adversary at opus, the lock passes (clean exit 0)."""
    eng_dir = _copy_engine_templates(tmp_path / "engine")
    sk_dir = _copy_skills_pipelines(tmp_path / "skills")
    result = _run_script(
        standard=eng_dir / "coding-pipeline-standard.yaml",
        skip_spec=eng_dir / "coding-pipeline-skip-spec.yaml",
        skills_dir=sk_dir,
    )
    assert result.returncode == 0, (
        f"expected exit 0 for clean copies, got {result.returncode}. "
        f"stderr={result.stderr!r}"
    )
    assert "model_tier locks OK" in result.stdout
