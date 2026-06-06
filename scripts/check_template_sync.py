#!/usr/bin/env python3
"""check_template_sync.py — drift lint for bundled coding-pipeline templates.

Closes #867 and #869.

PROBLEM
-------
The two bundled coding-pipeline templates,

    templates/coding-pipeline-standard.yaml
    templates/coding-pipeline-skip-spec.yaml

share several phase prompts that are intentionally either fully identical
(`postmortem_review`) or ~90% identical with named per-template carve-outs
(`acceptance_test`). When someone edits the prompt on one side and forgets the
other, the templates silently drift. This script catches that.

DESIGN
------
Two phase-config sets:

    STRICT_PHASES   — full byte-equality required (after whole-prompt strip())
    ANCHORED_PHASES — equality required AFTER eliding named anchor blocks

Anchor syntax (HTML comments embedded inside the literal-block prompt):

    <!-- SOME_NAME_BEGIN -->
    ... per-template content ...
    <!-- SOME_NAME_END -->

The script does NOT hard-code anchor names. The regex captures `(\\w+)` and
references the capture in the END line — so any new anchor name works
without a script edit (extensibility constraint per #869 design).

CROSS-REPO MODE (Issue #917)
----------------------------
The same prompts are ALSO published as standalone pipelines in the sibling
repository ``ToscanAI/orchemist-skills`` (``pipelines/*.yaml``). The engine is
canonical; the skills copies must not silently drift from it. Passing
``--skills-dir <path>`` (or setting ``ORCHEMIST_SKILLS_DIR``, or simply having
``../orchemist-skills/pipelines`` checked out as a sibling) additionally runs a
cross-repo parity check:

    * ``CROSS_REPO_STRICT_PHASES``   — prompt byte-equal after ``.strip()``
    * ``CROSS_REPO_ANCHORED_PHASES`` — prompt byte-equal after eliding anchors
    * ``CROSS_REPO_ALLOWLIST``       — shared phases intentionally NOT compared
    * ``CROSS_REPO_METADATA_LOCKS``  — the ONLY non-prompt field asserted across
      repos: ``spec_adversary.model_tier == "opus"`` in standard + maintenance.

Both sides are loaded through :class:`TemplateEngine.load_template` so the
engine's ``extends:`` skip-spec is merged before comparison (the skills copies
are standalone). When the skills directory is absent the cross-repo check is
SKIPPED gracefully (stdout message, exit 0) so plain ``pytest``/CI stays green
where only the engine is checked out. The intra-engine standard↔skip-spec lint
ALWAYS runs and is unchanged.

Exit codes:
    0 — no drift detected (intra-engine always; cross-repo if it ran)
    1 — drift detected (prints DRIFT lines + unified diff to stderr); this
        includes a cross-repo prompt drift OR a model_tier lock mismatch
    2 — configuration error (missing template files / missing configured
        phases / unbalanced or cross-named anchors)

Usage:
    python3 scripts/check_template_sync.py
    python3 scripts/check_template_sync.py --standard <path> --skip-spec <path>
    python3 scripts/check_template_sync.py --skills-dir <orchemist-skills/pipelines>

The default paths point to the bundled templates relative to the repository
root (located by walking up from this script until a pyproject.toml is found).
The default ``--skills-dir`` is the sibling ``../orchemist-skills/pipelines``
(overridable via ``ORCHEMIST_SKILLS_DIR`` or the explicit flag).
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from pathlib import Path
from typing import Sequence

try:
    import yaml
except ImportError as exc:  # pragma: no cover — pyyaml is a project dep
    print(f"ERROR: PyYAML is required: {exc}", file=sys.stderr)
    sys.exit(2)


# ─────────────────────────────────────────────────────────────────────────────
# Repo root discovery (must run BEFORE the TemplateEngine import so we can
# prepend the repo root to sys.path — Issue #704 made the drift lint
# composition-aware by reading MERGED template prompts via TemplateEngine,
# but the script is still invokable as a plain CLI from any cwd.)
# ─────────────────────────────────────────────────────────────────────────────


def _find_repo_root(start: Path) -> Path | None:
    """Walk up from `start` until a directory containing pyproject.toml is found."""
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


_REPO_ROOT_FOR_IMPORT = _find_repo_root(Path(__file__))
if _REPO_ROOT_FOR_IMPORT is not None and str(_REPO_ROOT_FOR_IMPORT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT_FOR_IMPORT))

# After sys.path manipulation, the engine import is safe.
try:
    from src.orchestration_engine.templates import (  # noqa: E402
        TemplateEngine,
    )
except ImportError as exc:  # pragma: no cover — would mean repo layout broke
    print(
        f"ERROR: could not import TemplateEngine — drift lint requires the "
        f"orchestration_engine package to be importable: {exc}",
        file=sys.stderr,
    )
    sys.exit(2)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration — phase sets to monitor
# ─────────────────────────────────────────────────────────────────────────────

# Phases whose prompt_template MUST be byte-equal across both templates
# (after whole-prompt outer-whitespace strip).
STRICT_PHASES: frozenset[str] = frozenset({"postmortem_review"})

# Phases whose prompt_template MUST be byte-equal after eliding anchor blocks.
ANCHORED_PHASES: frozenset[str] = frozenset({"acceptance_test"})

# Anchor regex — captures the anchor NAME so START and END must agree.
# Matches an entire line:  optional whitespace, <!-- NAME_BEGIN -->, optional ws.
_ANCHOR_BEGIN_RE = re.compile(r"^\s*<!--\s*(\w+)_BEGIN\s*-->\s*$")
_ANCHOR_END_RE = re.compile(r"^\s*<!--\s*(\w+)_END\s*-->\s*$")


# ─────────────────────────────────────────────────────────────────────────────
# Cross-repo configuration (Issue #917) — engine ↔ skills INSTRUCTION-TEXT parity
#
# Keyed PER PIPELINE because the shared-phase sets differ between
# standard / skip-spec / maintenance. These are SEPARATE constants from the
# intra-engine STRICT_PHASES/ANCHORED_PHASES above — the intra-engine code path
# is untouched.
# ─────────────────────────────────────────────────────────────────────────────

CROSS_REPO_PIPELINES = (
    "coding-pipeline-standard.yaml",
    "coding-pipeline-skip-spec.yaml",
    "coding-pipeline-maintenance.yaml",
)

# STRICT: prompt_template byte-equal after .strip() (no carve-outs).
CROSS_REPO_STRICT_PHASES: dict[str, set[str]] = {
    "coding-pipeline-standard.yaml": {
        "spec", "behavioral", "postmortem_spec", "fix", "acceptance_run", "test",
    },
    "coding-pipeline-skip-spec.yaml": {
        "fix", "acceptance_run", "test", "acceptance_test_adversary",
        "postmortem_review", "verify_tests_integrity",
    },
    "coding-pipeline-maintenance.yaml": {
        "spec", "spec_adversary", "existing_symbols_inventory", "implement",
        "review", "fix", "test",
    },
}

# ANCHORED: byte-equal after eliding <!-- *_BEGIN -->/<!-- *_END --> regions.
CROSS_REPO_ANCHORED_PHASES: dict[str, set[str]] = {
    "coding-pipeline-standard.yaml": {
        "spec_adversary", "existing_symbols_inventory", "acceptance_test",
    },
    "coding-pipeline-skip-spec.yaml": {"acceptance_test"},
    "coding-pipeline-maintenance.yaml": set(),
}

# ALLOWLIST: shared phases intentionally NOT compared (repo-unique or
# skills-ahead). Documented here for traceability; not asserted, but used to
# keep the "every configured phase exists on both sides" guard scoped to the
# STRICT/ANCHORED sets only.
CROSS_REPO_ALLOWLIST: dict[str, set[str]] = {
    "coding-pipeline-standard.yaml": {
        "implement", "review", "test_adversary", "postmortem_review",
    },
    "coding-pipeline-skip-spec.yaml": {"implement", "review"},
    "coding-pipeline-maintenance.yaml": set(),
}

# Cross-repo metadata locks — the ONLY non-prompt field asserted across repos.
# spec_adversary MUST run on opus in both repos (project policy
# feedback_max_effort_adversary_reviewer.md). The rest of model_tier and all
# other metadata stay UNGUARDED.
CROSS_REPO_METADATA_LOCKS: dict[str, dict[str, dict[str, str]]] = {
    "coding-pipeline-standard.yaml": {"spec_adversary": {"model_tier": "opus"}},
    "coding-pipeline-maintenance.yaml": {"spec_adversary": {"model_tier": "opus"}},
    # skip-spec has no spec_adversary phase → no lock.
}


# ─────────────────────────────────────────────────────────────────────────────
# Anchor elision
# ─────────────────────────────────────────────────────────────────────────────


class AnchorError(Exception):
    """Raised when anchors in a prompt are unbalanced or cross-named."""


def _elide_anchors(text: str, *, where: str) -> str:
    """Return `text` with all `<!-- NAME_BEGIN -->...<!-- NAME_END -->` blocks
    (inclusive of both delimiter lines) removed.

    Args:
        text: the prompt string
        where: identifier used in error messages (e.g. "standard:acceptance_test")

    Raises:
        AnchorError: on unbalanced or cross-named anchors.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        begin_match = _ANCHOR_BEGIN_RE.match(line)
        end_match = _ANCHOR_END_RE.match(line)
        if end_match and not begin_match:
            # An END before any BEGIN — unbalanced.
            raise AnchorError(
                f"{where}: unmatched {end_match.group(1)}_END anchor "
                f"(no preceding {end_match.group(1)}_BEGIN)"
            )
        if begin_match:
            anchor_name = begin_match.group(1)
            # Scan forward for the matching END.
            j = i + 1
            while j < n:
                end_here = _ANCHOR_END_RE.match(lines[j])
                begin_here = _ANCHOR_BEGIN_RE.match(lines[j])
                if begin_here:
                    raise AnchorError(
                        f"{where}: nested or duplicated {begin_here.group(1)}_BEGIN "
                        f"inside open {anchor_name}_BEGIN block"
                    )
                if end_here:
                    if end_here.group(1) != anchor_name:
                        raise AnchorError(
                            f"{where}: cross-named anchor — "
                            f"{anchor_name}_BEGIN closed by {end_here.group(1)}_END"
                        )
                    # Match found — skip the entire block (begin..end inclusive).
                    i = j + 1
                    break
                j += 1
            else:
                # Loop fell through — BEGIN without END.
                raise AnchorError(
                    f"{where}: unmatched {anchor_name}_BEGIN anchor (no closing "
                    f"{anchor_name}_END)"
                )
            continue
        # Regular line — keep it.
        out.append(line)
        i += 1
    return "".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# Template loading — Issue #704 update: read MERGED templates so the lint
# remains meaningful when one template `extends:` another.
# ─────────────────────────────────────────────────────────────────────────────


def _load_template_phases(path: Path) -> dict[str, dict]:
    """Return ``{phase_id: {"prompt_template": str}}`` for the MERGED template.

    Uses :class:`TemplateEngine.load_template` so any ``extends:`` /
    ``exclude_phases:`` directives are resolved before the drift check runs.
    Prior to Issue #704 this read raw YAML — that worked because both
    bundled coding-pipeline templates duplicated the locked phases verbatim.
    After skip-spec switched to ``extends: coding-pipeline-standard``, the
    raw YAML no longer contains the inherited phases at all, so a raw-YAML
    lint would always fail with "phase missing".

    Args:
        path: Absolute path to the template YAML file.

    Returns:
        Mapping of phase id → ``{"prompt_template": str}``.

    Raises:
        FileNotFoundError: When the YAML file is missing.
        ValueError: When the YAML parses to a non-dict or fails engine load.
    """
    if not path.is_file():
        raise FileNotFoundError(f"template file not found: {path}")
    engine = TemplateEngine()
    template_obj = engine.load_template(path)
    return {
        phase.id: {"prompt_template": phase.prompt_template or ""}
        for phase in template_obj.phases
    }


def _load_template_phase_meta(path: Path) -> dict[str, dict]:
    """Return ``{phase_id: {"model_tier": str | None}}`` for the MERGED template.

    Sibling helper to :func:`_load_template_phases` for the cross-repo
    metadata locks (Issue #917). Kept SEPARATE so the existing
    ``{"prompt_template": str}`` shape used by the intra-engine path is not
    mutated. ``TemplateEngine.load_template`` already resolves ``model_tier``
    (including ``extends:`` inheritance), so reading ``phase.model_tier`` from
    the merged template is correct for both standalone-skills and
    ``extends:``-engine sides.

    Args:
        path: Absolute path to the template YAML file.

    Returns:
        Mapping of phase id → ``{"model_tier": str | None}``.
    """
    if not path.is_file():
        raise FileNotFoundError(f"template file not found: {path}")
    engine = TemplateEngine()
    template_obj = engine.load_template(path)
    return {
        phase.id: {"model_tier": getattr(phase, "model_tier", None)}
        for phase in template_obj.phases
    }


# ─────────────────────────────────────────────────────────────────────────────
# Drift checks
# ─────────────────────────────────────────────────────────────────────────────


def _format_diff(label_a: str, label_b: str, text_a: str, text_b: str) -> str:
    diff = difflib.unified_diff(
        text_a.splitlines(keepends=True),
        text_b.splitlines(keepends=True),
        fromfile=label_a,
        tofile=label_b,
        n=3,
    )
    return "".join(diff)


def _check_strict(
    phase_id: str,
    prompt_std: str,
    prompt_skip: str,
    std_path: Path,
    skip_path: Path,
) -> list[str]:
    """Return a list of human-readable DRIFT lines (empty if in sync)."""
    a = prompt_std.strip()
    b = prompt_skip.strip()
    if a == b:
        return []
    diff = _format_diff(
        f"{std_path.name}:{phase_id}",
        f"{skip_path.name}:{phase_id}",
        a + "\n",
        b + "\n",
    )
    return [
        f"DRIFT: phase={phase_id} mode=strict (byte-equality required after strip)",
        f"  {std_path}",
        f"  {skip_path}",
        diff,
    ]


def _check_anchored(
    phase_id: str,
    prompt_std: str,
    prompt_skip: str,
    std_path: Path,
    skip_path: Path,
) -> list[str]:
    """Return a list of human-readable DRIFT lines (empty if in sync)."""
    std_elided = _elide_anchors(prompt_std, where=f"{std_path.name}:{phase_id}").strip()
    skip_elided = _elide_anchors(prompt_skip, where=f"{skip_path.name}:{phase_id}").strip()
    if std_elided == skip_elided:
        return []
    diff = _format_diff(
        f"{std_path.name}:{phase_id} (anchors elided)",
        f"{skip_path.name}:{phase_id} (anchors elided)",
        std_elided + "\n",
        skip_elided + "\n",
    )
    return [
        f"DRIFT: phase={phase_id} mode=anchored (byte-equality required after eliding "
        f"<!-- *_BEGIN -->/<!-- *_END --> blocks)",
        f"  {std_path}",
        f"  {skip_path}",
        diff,
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Cross-repo drift check (Issue #917) — engine ↔ skills
# ─────────────────────────────────────────────────────────────────────────────


class CrossRepoConfigError(Exception):
    """Raised on a cross-repo configuration error (missing file/phase) → exit 2."""


def _resolve_skills_dir(explicit: Path | None) -> Path | None:
    """Resolve the skills ``pipelines/`` directory.

    Precedence: explicit ``--skills-dir`` > ``ORCHEMIST_SKILLS_DIR`` env var >
    the sibling ``<repo_root>/../orchemist-skills/pipelines`` default. Returns
    ``None`` only when no repo root is known AND no explicit/env value is set.
    """
    if explicit is not None:
        return explicit
    env = os.environ.get("ORCHEMIST_SKILLS_DIR")
    if env:
        return Path(env)
    if _REPO_ROOT_FOR_IMPORT is not None:
        return _REPO_ROOT_FOR_IMPORT.parent / "orchemist-skills" / "pipelines"
    return None


def _run_cross_repo(
    engine_templates_dir: Path, skills_pipelines_dir: Path
) -> list[str]:
    """Compare shared phases' prompt_template (+ the model_tier locks) between
    the engine templates and the skills pipelines.

    Returns a list of human-readable DRIFT lines (empty if fully in sync).

    Raises:
        CrossRepoConfigError: missing pipeline file or a configured
            strict/anchored/locked phase missing on either side (→ exit 2).
        AnchorError: unbalanced/cross-named anchors (→ exit 2).
    """
    drift_lines: list[str] = []

    for name in CROSS_REPO_PIPELINES:
        engine_path = engine_templates_dir / name
        skills_path = skills_pipelines_dir / name
        if not engine_path.is_file():
            raise CrossRepoConfigError(
                f"engine pipeline not found: {engine_path}"
            )
        if not skills_path.is_file():
            raise CrossRepoConfigError(
                f"skills pipeline not found: {skills_path}"
            )

        # Same loader BOTH sides → engine extends: is merged before comparison.
        engine_phases = _load_template_phases(engine_path)
        skills_phases = _load_template_phases(skills_path)

        strict_set = CROSS_REPO_STRICT_PHASES.get(name, set())
        anchored_set = CROSS_REPO_ANCHORED_PHASES.get(name, set())

        # Missing-phase guard: every configured strict/anchored phase MUST exist
        # on BOTH sides for that pipeline. Allowlisted phases need not exist.
        for pid in sorted(strict_set | anchored_set):
            if pid not in engine_phases:
                raise CrossRepoConfigError(
                    f"phase {pid!r} missing from engine:{name} ({engine_path})"
                )
            if pid not in skills_phases:
                raise CrossRepoConfigError(
                    f"phase {pid!r} missing from skills:{name} ({skills_path})"
                )

        for pid in sorted(strict_set):
            prompt_eng = engine_phases[pid].get("prompt_template", "")
            prompt_sk = skills_phases[pid].get("prompt_template", "")
            drift_lines += _check_strict(
                f"{name}:{pid}", prompt_eng, prompt_sk, engine_path, skills_path
            )

        for pid in sorted(anchored_set):
            prompt_eng = engine_phases[pid].get("prompt_template", "")
            prompt_sk = skills_phases[pid].get("prompt_template", "")
            drift_lines += _check_anchored(
                f"{name}:{pid}", prompt_eng, prompt_sk, engine_path, skills_path
            )

        # Metadata locks — the ONLY non-prompt field asserted across repos.
        locks = CROSS_REPO_METADATA_LOCKS.get(name, {})
        if locks:
            engine_meta = _load_template_phase_meta(engine_path)
            skills_meta = _load_template_phase_meta(skills_path)
            for pid, fields in locks.items():
                if pid not in engine_meta:
                    raise CrossRepoConfigError(
                        f"locked phase {pid!r} missing from engine:{name}"
                    )
                if pid not in skills_meta:
                    raise CrossRepoConfigError(
                        f"locked phase {pid!r} missing from skills:{name}"
                    )
                for field, expected in fields.items():
                    eng_val = engine_meta[pid].get(field)
                    sk_val = skills_meta[pid].get(field)
                    if eng_val != expected:
                        drift_lines.append(
                            f"DRIFT: pipeline={name} phase={pid} field={field} "
                            f"expected={expected} got={eng_val} (engine)"
                        )
                    if sk_val != expected:
                        drift_lines.append(
                            f"DRIFT: pipeline={name} phase={pid} field={field} "
                            f"expected={expected} got={sk_val} (skills)"
                        )

    return drift_lines


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    repo_root = _REPO_ROOT_FOR_IMPORT
    default_standard = (
        repo_root / "templates" / "coding-pipeline-standard.yaml"
        if repo_root is not None
        else None
    )
    default_skip_spec = (
        repo_root / "templates" / "coding-pipeline-skip-spec.yaml"
        if repo_root is not None
        else None
    )
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else "template drift lint",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--standard",
        type=Path,
        default=default_standard,
        help="path to coding-pipeline-standard.yaml (default: repo templates/)",
    )
    p.add_argument(
        "--skip-spec",
        type=Path,
        default=default_skip_spec,
        help="path to coding-pipeline-skip-spec.yaml (default: repo templates/)",
    )
    p.add_argument(
        "--skills-dir",
        type=Path,
        default=None,
        help=(
            "path to the orchemist-skills 'pipelines/' directory for the "
            "cross-repo parity check (Issue #917). Precedence: this flag > "
            "$ORCHEMIST_SKILLS_DIR > sibling ../orchemist-skills/pipelines. "
            "When absent the cross-repo check is skipped gracefully (exit 0). "
            "The intra-engine lint always runs regardless."
        ),
    )
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.standard is None or args.skip_spec is None:
        print(
            "ERROR: could not locate repo root (no pyproject.toml found walking up "
            f"from {Path(__file__).resolve()}). Pass --standard and --skip-spec "
            "explicitly.",
            file=sys.stderr,
        )
        return 2

    try:
        std_phases = _load_template_phases(args.standard)
        skip_phases = _load_template_phases(args.skip_spec)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except (ValueError, yaml.YAMLError, KeyError) as e:
        print(f"ERROR: failed to load template: {e}", file=sys.stderr)
        return 2

    # Verify every configured phase exists in BOTH templates.
    configured = STRICT_PHASES | ANCHORED_PHASES
    missing: list[str] = []
    for pid in configured:
        if pid not in std_phases:
            missing.append(f"phase {pid!r} missing from {args.standard}")
        if pid not in skip_phases:
            missing.append(f"phase {pid!r} missing from {args.skip_spec}")
    if missing:
        print("ERROR: configured drift-locked phases not found:", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        return 2

    # Run checks.
    drift_lines: list[str] = []

    for pid in sorted(STRICT_PHASES):
        prompt_std = std_phases[pid].get("prompt_template", "")
        prompt_skip = skip_phases[pid].get("prompt_template", "")
        if not isinstance(prompt_std, str) or not isinstance(prompt_skip, str):
            print(
                f"ERROR: phase {pid}: prompt_template is not a string in one of the "
                "templates",
                file=sys.stderr,
            )
            return 2
        drift_lines += _check_strict(
            pid, prompt_std, prompt_skip, args.standard, args.skip_spec
        )

    for pid in sorted(ANCHORED_PHASES):
        prompt_std = std_phases[pid].get("prompt_template", "")
        prompt_skip = skip_phases[pid].get("prompt_template", "")
        if not isinstance(prompt_std, str) or not isinstance(prompt_skip, str):
            print(
                f"ERROR: phase {pid}: prompt_template is not a string in one of the "
                "templates",
                file=sys.stderr,
            )
            return 2
        try:
            drift_lines += _check_anchored(
                pid, prompt_std, prompt_skip, args.standard, args.skip_spec
            )
        except AnchorError as e:
            print(f"ERROR: unbalanced anchor: {e}", file=sys.stderr)
            return 2

    intra_failed = False
    if drift_lines:
        for line in drift_lines:
            print(line, file=sys.stderr)
        print(
            f"FAIL: template drift detected in {len([1 for l in drift_lines if l.startswith('DRIFT')])} "
            "phase(s). Fix the prompt on one side or extract a shared anchor block.",
            file=sys.stderr,
        )
        intra_failed = True
    else:
        print(
            f"OK: {len(STRICT_PHASES)} strict-locked phase(s), "
            f"{len(ANCHORED_PHASES)} anchored phase(s) in sync between "
            f"{args.standard.name} and {args.skip_spec.name}"
        )

    # ── Cross-repo parity check (Issue #917) ────────────────────────────────
    # The intra-engine check above ALWAYS runs and is unchanged. The cross-repo
    # check runs additionally when the skills pipelines directory is present;
    # otherwise it is skipped gracefully (exit 0 contribution). The final exit
    # code is the AND of both: 1 if EITHER drifts, 2 on any config/anchor error.
    #
    # The ENGINE side of the cross-repo comparison is ALWAYS the bundled
    # canonical templates/ directory (located via the repo root), NOT
    # ``args.standard.parent``. ``--standard``/``--skip-spec`` only redirect the
    # *intra-engine* lint (e.g. the acceptance tests point them at mutated 2-file
    # temp copies); resolving the cross-repo engine side from those would treat
    # a partial temp dir as canonical and exit 2 on the absent
    # coding-pipeline-maintenance.yaml. Decoupling here keeps cross-repo mode
    # correct regardless of how the intra-engine lint was redirected.
    if _REPO_ROOT_FOR_IMPORT is not None:
        engine_templates_dir = _REPO_ROOT_FOR_IMPORT / "templates"
    else:  # pragma: no cover — only when no pyproject.toml was found walking up
        engine_templates_dir = args.standard.parent
    skills_dir = _resolve_skills_dir(args.skills_dir)

    cross_failed = False
    if skills_dir is None or not skills_dir.is_dir():
        shown = skills_dir if skills_dir is not None else "<no skills dir resolved>"
        print(
            f"INFO: orchemist-skills not found at {shown} — "
            "cross-repo parity check skipped"
        )
    else:
        try:
            cross_drift = _run_cross_repo(engine_templates_dir, skills_dir)
        except CrossRepoConfigError as e:
            print(f"ERROR: cross-repo configuration error: {e}", file=sys.stderr)
            return 2
        except FileNotFoundError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        except AnchorError as e:
            print(f"ERROR: unbalanced anchor: {e}", file=sys.stderr)
            return 2
        except (ValueError, yaml.YAMLError, KeyError) as e:
            print(f"ERROR: failed to load template: {e}", file=sys.stderr)
            return 2

        if cross_drift:
            for line in cross_drift:
                print(line, file=sys.stderr)
            print(
                f"FAIL: cross-repo parity drift detected in "
                f"{len([1 for l in cross_drift if l.startswith('DRIFT')])} "
                f"phase(s)/field(s) between engine and skills ({skills_dir}). "
                "Reconcile the skills copy to the engine (canonical) prompt.",
                file=sys.stderr,
            )
            cross_failed = True
        else:
            n_strict = sum(len(s) for s in CROSS_REPO_STRICT_PHASES.values())
            n_anchored = sum(len(s) for s in CROSS_REPO_ANCHORED_PHASES.values())
            print(
                f"OK: cross-repo parity in sync across {len(CROSS_REPO_PIPELINES)} "
                f"pipeline(s) ({n_strict} strict + {n_anchored} anchored phase "
                f"comparison(s)); model_tier locks OK"
            )

    if intra_failed or cross_failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
