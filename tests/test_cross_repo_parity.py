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

It ALSO covers the intra-skills phase-skill boilerplate drift lint (Issue #929
part 2): the fresh-subagent policy anchors must be present (verbatim substring)
in every phase-wrapper skill ``.md``. Those tests live alongside the cross-repo
ones because the new check derives its ``skills/`` dir from the same
``--skills-dir ../orchemist-skills/pipelines`` surface and reuses the same
``_run_script``/skipif fixtures:

* constant-integrity unit assertion (always runnable, no subprocess),
* graceful-skip when the skills ``skills/`` dir is absent (always runnable),
* a clean real-sibling run → 0 drift (skipif-guarded),
* synthetic-drift CAUGHT → nonzero exit naming the file (skipif-guarded),
* adversary's absence-of-anchor does NOT trigger drift (skipif-guarded).

The temp-copy tests mutate copies of BOTH repos' files and point the engine at
the temp engine dir via ``ORCH_TEMPLATES_PATH`` so the skip-spec ``extends:``
resolves locally (same mechanism as tests/test_check_template_sync.py).
"""

from __future__ import annotations

import importlib.util
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


# ─────────────────────────────────────────────────────────────────────────────
# Phase-skill boilerplate (Issue #929 part 2) — resolve the skills ``skills/``
# dir (the SIBLING of ``pipelines/``, where the orchemist-*.md wrappers live).
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_skills_md() -> Path | None:
    """Resolve the skills ``skills/`` dir as the sibling of ``pipelines/``.

    Mirrors the script's own derivation (``<pipelines dir>.parent / "skills"``)
    so the test resolves the same directory the lint will check.
    """
    if _SKILLS_PIPELINES is None:
        return None
    return _SKILLS_PIPELINES.parent / "skills"


_SKILLS_MD = _resolve_skills_md()
_SKILLS_MD_PRESENT = _SKILLS_MD is not None and _SKILLS_MD.is_dir()
_skipif_no_skills_md = pytest.mark.skipif(
    not _SKILLS_MD_PRESENT,
    reason="orchemist-skills 'skills/' dir not resolvable as sibling of 'pipelines/'",
)


def _load_check_module():
    """Import scripts/check_template_sync.py as a module for unit assertions.

    The script is not part of an importable package (it lives in ``scripts/``),
    so load it by file path. Used by the always-runnable constant-integrity test
    so the constant shape is guarded even when the skills sibling is absent, and
    to source the pinned file set for the temp-copy tests (single source of
    truth — no second hard-coded list in the test).
    """
    spec = importlib.util.spec_from_file_location(
        "_check_template_sync_under_test", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The script module imported once for the constant-integrity unit and to source
# the pinned file set (so the tests track the engine's single source of truth).
_CHECK_MOD = _load_check_module()
_PHASE_SKILL_ANCHOR_FILES: tuple[str, ...] = _CHECK_MOD.PHASE_SKILL_ANCHOR_FILES


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


def _copy_skills_md(dst: Path, *, names: list[str] | None = None) -> Path:
    """Copy the real skills ``*.md`` wrappers into ``dst`` (the temp skills/ dir).

    By default copies the 7 pinned anchor-bearing files; pass ``names`` to copy a
    different set (e.g. to also include ``orchemist-adversary.md`` for the
    exclusion test). Mirrors ``_copy_skills_pipelines``' real-file-copy approach,
    which is why callers are skipif-guarded on the real sibling.
    """
    assert _SKILLS_MD is not None
    dst.mkdir(parents=True, exist_ok=True)
    for name in names if names is not None else list(_PHASE_SKILL_ANCHOR_FILES):
        shutil.copy(_SKILLS_MD / name, dst / name)
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


# ─────────────────────────────────────────────────────────────────────────────
# Phase-skill boilerplate drift lint (Issue #929 part 2)
#
# Intra-skills consistency: the fresh-subagent policy anchors must be present
# (verbatim substring) in every phase-wrapper skill .md. The skills .md files
# live in the SIBLING ``skills/`` dir of ``pipelines/``; the script derives that
# dir as ``<--skills-dir>.parent / "skills"``. The temp-copy tests therefore
# build BOTH a ``pipelines/`` dir (copied real, so the cross-repo check passes
# cleanly and never exits 2) AND a ``skills/`` dir (copied real, optionally with
# one anchor mutated), then point ``--skills-dir`` at ``tmp/pipelines``.
# ─────────────────────────────────────────────────────────────────────────────

# A perturbation that breaks substring-containment of ANCHOR 1 while leaving the
# rest of the file (and ANCHOR 2) intact. Replacing "non-negotiable" inside the
# Step-1 sentence with "NEGOTIABLE" both removes the exact ANCHOR 1 substring and
# inverts the policy meaning — exactly the silent drift the lint must catch.
_STEP1_TOKEN = "non-negotiable; do NOT execute the prompt inline"
_STEP1_PERTURBED = "NEGOTIABLE; you MAY execute the prompt inline"


def test_phase_skill_anchor_constants_are_well_formed() -> None:
    """6.0 — constant-integrity unit (ALWAYS runnable, no subprocess).

    Guards against an accidental join-spacing / em-dash byte edit in the pinned
    anchor constants and against the file set silently changing membership
    (adversary must stay OUT, existing-symbols-inventory must stay IN).
    """
    step1 = _CHECK_MOD.PHASE_SKILL_ANCHOR_STEP1
    step2 = _CHECK_MOD.PHASE_SKILL_ANCHOR_STEP2
    files = _CHECK_MOD.PHASE_SKILL_ANCHOR_FILES

    # ANCHOR 1: exact expected bytes, em-dash is U+2014, ends at "inline" with NO
    # trailing period (so implement.md's trailing parenthetical is still a
    # superstring → substring-containment holds).
    assert step1 == (
        "Per [[feedback_fresh_subagent_per_phase]] — the fresh-context-window "
        "property is non-negotiable; do NOT execute the prompt inline"
    )
    assert "—" in step1, "ANCHOR 1 must contain the U+2014 em-dash"
    assert "--" not in step1, "ANCHOR 1 must not use an ASCII double-hyphen"
    assert step1.endswith("inline"), "ANCHOR 1 must end at 'inline' (no period)"

    # ANCHOR 2: exact expected bytes; begins with the lowercase 'per [[feedback_'.
    assert step2 == (
        "per [[feedback_fresh_subagent_per_phase]], the fresh-context-window "
        "property is non-negotiable"
    )
    assert step2.startswith("per [[feedback_")

    # The pinned set: exactly 7 stems; adversary OUT, existing-symbols-inventory IN.
    assert isinstance(files, tuple)
    assert len(files) == 7, f"expected 7 pinned files, got {len(files)}: {files}"
    assert "orchemist-adversary.md" not in files
    assert "orchemist-existing-symbols-inventory.md" in files
    assert set(files) == {
        "orchemist-spec.md",
        "orchemist-behavioral.md",
        "orchemist-acceptance-test.md",
        "orchemist-existing-symbols-inventory.md",
        "orchemist-implement.md",
        "orchemist-review.md",
        "orchemist-fix.md",
    }


def test_phase_skill_graceful_skip_when_skills_md_absent(tmp_path: Path) -> None:
    """6.a — graceful-skip (ALWAYS runnable): a --skills-dir whose sibling
    ``skills/`` does not exist → exit 0 + the new skip note, no failure."""
    bogus = tmp_path / "no_such" / "pipelines"
    result = _run_script(skills_dir=bogus)
    assert result.returncode == 0, (
        f"graceful skip must exit 0, got {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    # The new phase-skill check's distinct skip note.
    assert "phase-skill boilerplate check skipped" in result.stdout
    # Sanity: the always-runs intra-engine lint is unaffected.
    assert "OK:" in result.stdout
    assert "strict" in result.stdout.lower()


@_skipif_no_skills_md
def test_phase_skill_clean_real_sibling_passes() -> None:
    """6.b — clean real-sibling run: the boilerplate check passes (0 drift)
    against the actual skills ``skills/`` dir."""
    result = _run_script(skills_dir=_SKILLS_PIPELINES)
    assert result.returncode == 0, (
        f"expected exit 0 against the real skills checkout, got "
        f"{result.returncode}.\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    # The new OK line, scoped so it does not duplicate the cross-repo OK assertion.
    assert "phase-skill boilerplate anchors present in 7" in result.stdout


@_skipif_no_skills_md
def test_phase_skill_synthetic_drift_caught(tmp_path: Path) -> None:
    """6.c — synthetic-drift CAUGHT: mutate ONE pinned file's Step-1 anchor →
    NONZERO exit + a drift message naming that file. Proves not trivially green.

    Builds a tmp tree with copied real ``pipelines/`` (so the cross-repo check
    passes cleanly and the ONLY failing signal is the new one) plus copied real
    ``skills/`` with one file's ANCHOR 1 perturbed.
    """
    eng_dir = _copy_engine_templates(tmp_path / "engine")
    pipe_dir = _copy_skills_pipelines(tmp_path / "pipelines")
    _copy_skills_md(tmp_path / "skills")  # sibling of pipe_dir; the 7 pinned files

    # Mutate ONE file's Step-1 anchor so substring-containment fails for it only.
    target = "orchemist-review.md"
    target_path = tmp_path / "skills" / target
    original = target_path.read_text(encoding="utf-8")
    assert _STEP1_TOKEN in original, (
        f"precondition: {target} should contain the Step-1 anchor token"
    )
    target_path.write_text(
        original.replace(_STEP1_TOKEN, _STEP1_PERTURBED, 1), encoding="utf-8"
    )

    result = _run_script(
        standard=eng_dir / "coding-pipeline-standard.yaml",
        skip_spec=eng_dir / "coding-pipeline-skip-spec.yaml",
        skills_dir=pipe_dir,
    )
    assert result.returncode == 1, (
        f"expected exit 1 for phase-skill drift, got {result.returncode}.\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    # The drift message must name the offending file and identify the check.
    assert target in result.stderr, (
        f"drift message must name {target}; stderr={result.stderr!r}"
    )
    assert "phase-skill" in result.stderr
    assert "drift" in result.stderr.lower()


@_skipif_no_skills_md
def test_phase_skill_adversary_exclusion(tmp_path: Path) -> None:
    """6.e — adversary-exclusion: adversary.md NOT containing the anchor does
    NOT trigger drift (it is not in the pinned set).

    Copies the 7 pinned files (clean) PLUS adversary, then mutates/strips any
    fresh-subagent wording inside adversary. The check must still exit 0 — it
    neither reads nor requires the anchor in adversary.
    """
    eng_dir = _copy_engine_templates(tmp_path / "engine")
    pipe_dir = _copy_skills_pipelines(tmp_path / "pipelines")
    sk_md = _copy_skills_md(
        tmp_path / "skills",
        names=list(_PHASE_SKILL_ANCHOR_FILES) + ["orchemist-adversary.md"],
    )

    # Overwrite adversary with content that carries NEITHER anchor. If a future
    # refactor wrongly pinned adversary, this would flip the run to exit 1.
    adv_path = sk_md / "orchemist-adversary.md"
    adv_path.write_text(
        "# adversary wrapper (no fresh-subagent anchor by design)\n",
        encoding="utf-8",
    )

    result = _run_script(
        standard=eng_dir / "coding-pipeline-standard.yaml",
        skip_spec=eng_dir / "coding-pipeline-skip-spec.yaml",
        skills_dir=pipe_dir,
    )
    assert result.returncode == 0, (
        f"adversary lacking the anchor must NOT trigger drift; got exit "
        f"{result.returncode}.\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "phase-skill boilerplate anchors present in 7" in result.stdout
