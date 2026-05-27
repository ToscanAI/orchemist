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

Exit codes:
    0 — no drift detected
    1 — drift detected (prints DRIFT lines + unified diff to stderr)
    2 — configuration error (missing template files / missing configured
        phases / unbalanced or cross-named anchors)

Usage:
    python3 scripts/check_template_sync.py
    python3 scripts/check_template_sync.py --standard <path> --skip-spec <path>

The default paths point to the bundled templates relative to the repository
root (located by walking up from this script until a pyproject.toml is found).
"""

from __future__ import annotations

import argparse
import difflib
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
# Repo root discovery
# ─────────────────────────────────────────────────────────────────────────────


def _find_repo_root(start: Path) -> Path | None:
    """Walk up from `start` until a directory containing pyproject.toml is found."""
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


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
# YAML loading
# ─────────────────────────────────────────────────────────────────────────────


def _load_template(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"template file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"template {path} did not parse to a mapping")
    return data


def _phases_by_id(template: dict, path: Path) -> dict[str, dict]:
    """Return {phase_id: phase_dict} for the template.

    Templates may put `phases:` at the top level (current convention) or nested
    under a `pipeline:` key. Support both for forward-compat.
    """
    phases = template.get("phases")
    if phases is None:
        pipeline = template.get("pipeline") or {}
        phases = pipeline.get("phases") or []
    if not isinstance(phases, list):
        raise ValueError(f"template {path}: phases is not a list")
    out: dict[str, dict] = {}
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        pid = phase.get("id")
        if isinstance(pid, str):
            out[pid] = phase
    return out


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
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    repo_root = _find_repo_root(Path(__file__))
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
        std_template = _load_template(args.standard)
        skip_template = _load_template(args.skip_spec)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except (ValueError, yaml.YAMLError) as e:
        print(f"ERROR: failed to parse template: {e}", file=sys.stderr)
        return 2

    std_phases = _phases_by_id(std_template, args.standard)
    skip_phases = _phases_by_id(skip_template, args.skip_spec)

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

    if drift_lines:
        for line in drift_lines:
            print(line, file=sys.stderr)
        print(
            f"FAIL: template drift detected in {len([1 for l in drift_lines if l.startswith('DRIFT')])} "
            "phase(s). Fix the prompt on one side or extract a shared anchor block.",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: {len(STRICT_PHASES)} strict-locked phase(s), "
        f"{len(ANCHORED_PHASES)} anchored phase(s) in sync between "
        f"{args.standard.name} and {args.skip_spec.name}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
