"""Acceptance tests for issue #890 — v1 governance hardening.

Verifies the governance file set required by the v1 readiness gate:

  - MAINTAINERS.md   (who can release)
  - CODE_OF_CONDUCT.md (adopted-by-reference link to Contributor Covenant v2.1)
  - .github/CODEOWNERS (gates supply-chain files)
  - .github/workflows/publish.yaml (runs `git tag -v` in WARN mode)
  - docs/RELEASE-SOP.md §6 table updated for CI-enforced rows

These are static-content checks: stdlib only, no network, no git.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# A.1 MAINTAINERS.md
# ----------------------------------------------------------------------

def test_maintainers_md_exists_and_lists_handle_and_email() -> None:
    path = REPO_ROOT / "MAINTAINERS.md"
    assert path.exists(), "MAINTAINERS.md must exist at repo root"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), "MAINTAINERS.md must be non-empty"

    handle_pattern = re.compile(r"@[A-Za-z0-9][A-Za-z0-9-]*")
    assert handle_pattern.search(text), (
        "MAINTAINERS.md must list at least one GitHub handle "
        "matching @[A-Za-z0-9][A-Za-z0-9-]*"
    )

    email_pattern = re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    )
    assert email_pattern.search(text), (
        "MAINTAINERS.md must include at least one contact email"
    )

    line_count = sum(1 for _ in text.splitlines())
    assert line_count <= 80, (
        f"MAINTAINERS.md should be factual (<=80 lines); got {line_count}"
    )


# ----------------------------------------------------------------------
# A.2 CODE_OF_CONDUCT.md — adopted by reference
# ----------------------------------------------------------------------

CANONICAL_COVENANT_URL = (
    "https://www.contributor-covenant.org/version/2/1/code_of_conduct/"
)


def test_code_of_conduct_adopts_covenant_by_reference() -> None:
    path = REPO_ROOT / "CODE_OF_CONDUCT.md"
    assert path.exists(), "CODE_OF_CONDUCT.md must exist at repo root"
    text = path.read_text(encoding="utf-8")
    assert "Contributor Covenant" in text, (
        "CODE_OF_CONDUCT.md must name the Contributor Covenant"
    )
    assert "2.1" in text, (
        "CODE_OF_CONDUCT.md must reference version 2.1"
    )
    assert CANONICAL_COVENANT_URL in text, (
        f"CODE_OF_CONDUCT.md must link to {CANONICAL_COVENANT_URL}"
    )


def test_code_of_conduct_is_link_only_short_file() -> None:
    """Adoption-by-reference: file must NOT inline the full policy text."""
    text = _read("CODE_OF_CONDUCT.md")
    non_blank = [ln for ln in text.splitlines() if ln.strip()]
    assert len(non_blank) <= 30, (
        f"CODE_OF_CONDUCT.md is adopted-by-reference and must stay short "
        f"(<=30 non-blank lines); got {len(non_blank)}"
    )


def test_code_of_conduct_has_report_pointer_and_timeline() -> None:
    """File must point to a reporting channel and state a response timeline."""
    text = _read("CODE_OF_CONDUCT.md")
    text_lower = text.lower()

    email_pattern = re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    )
    has_report_pointer = (
        bool(email_pattern.search(text))
        or "maintainers.md" in text_lower
        or "maintainers" in text_lower
    )
    assert has_report_pointer, (
        "CODE_OF_CONDUCT.md must include either an email or pointer to "
        "MAINTAINERS.md for reporting"
    )

    day_pattern = re.compile(r"\b(\d+)\s*days?\b", re.IGNORECASE)
    day_matches = day_pattern.findall(text)
    assert len(day_matches) >= 2, (
        "CODE_OF_CONDUCT.md must state two response timelines "
        "(acknowledgement + resolution) using 'N days' phrasing; "
        f"found {len(day_matches)} day-references"
    )


# ----------------------------------------------------------------------
# A.3 .github/CODEOWNERS
# ----------------------------------------------------------------------

def test_codeowners_exists_and_owns_supply_chain_files() -> None:
    path = REPO_ROOT / ".github" / "CODEOWNERS"
    assert path.exists(), ".github/CODEOWNERS must exist"
    text = path.read_text(encoding="utf-8")

    assert "pyproject.toml" in text, (
        "CODEOWNERS must list pyproject.toml so version bumps require "
        "maintainer review"
    )
    assert "publish.yaml" in text, (
        "CODEOWNERS must list publish.yaml (supply-chain critical)"
    )
    assert "RELEASE-SOP.md" in text, (
        "CODEOWNERS must list RELEASE-SOP.md (release governance)"
    )

    owner_pattern = re.compile(r"@[A-Za-z0-9][A-Za-z0-9-/]*")
    for line in text.splitlines():
        if "pyproject.toml" in line:
            assert owner_pattern.search(line), (
                f"The pyproject.toml CODEOWNERS line must name an owner: "
                f"{line!r}"
            )
            break
    else:
        raise AssertionError(
            "Could not locate a CODEOWNERS line for pyproject.toml"
        )


# ----------------------------------------------------------------------
# A.4 + A.9  publish.yaml — signed tag verification (WARN mode)
# ----------------------------------------------------------------------

def test_publish_workflow_runs_signed_tag_verification() -> None:
    path = REPO_ROOT / ".github" / "workflows" / "publish.yaml"
    assert path.exists(), "publish.yaml must exist"
    text = path.read_text(encoding="utf-8")

    assert "git tag -v" in text, (
        "publish.yaml must include `git tag -v` to verify a signed tag"
    )

    tag_verify_pos = text.find("git tag -v")
    build_pos = text.find("python -m build")
    assert build_pos != -1, "publish.yaml must contain `python -m build`"
    assert tag_verify_pos < build_pos, (
        "The `git tag -v` step must run BEFORE `python -m build` so an "
        "unsigned tag does not trigger a build"
    )


def test_publish_workflow_warn_mode_with_fail_date() -> None:
    text = _read(".github/workflows/publish.yaml")

    assert "2026-06-03" in text, (
        "publish.yaml must reference the WARN->FAIL transition date "
        "2026-06-03 as a TODO/comment for the follow-up PR"
    )

    has_warn_marker = ("::warning::" in text) or ("WARN" in text)
    assert has_warn_marker, (
        "publish.yaml must mark the tag-verify step as WARN-mode "
        "(via ::warning:: annotation or a 'WARN' comment)"
    )

    # Find the executable line (not a YAML comment) that invokes `git tag -v`.
    # Comment lines start with optional whitespace + '#'.
    exec_lines = [
        ln for ln in text.splitlines()
        if "git tag -v" in ln and not ln.lstrip().startswith("#")
    ]
    assert exec_lines, (
        "publish.yaml must include an executable `git tag -v` line "
        "(not just commentary referencing it)"
    )
    has_fallthrough = (
        any("||" in ln for ln in exec_lines)
        or "continue-on-error: true" in text
    )
    assert has_fallthrough, (
        "The executable `git tag -v` line must NOT fail the workflow in "
        "WARN mode. Use `git tag -v ... || echo ...` or "
        "`continue-on-error: true` at the step level."
    )


# ----------------------------------------------------------------------
# A.5 docs/RELEASE-SOP.md — §6 table updated
# ----------------------------------------------------------------------

def test_release_sop_table_marks_ci_for_enforced_rows() -> None:
    text = _read("docs/RELEASE-SOP.md")
    assert "| Check |" in text or "|Check|" in text.replace(" ", ""), (
        "RELEASE-SOP.md must still contain the §6 enforcement table"
    )

    lower = text.lower()

    reviewer_keys = ("2-reviewer", "2 reviewer", "two-reviewer", "two reviewer")
    reviewer_ok = False
    for line in lower.splitlines():
        if any(k in line for k in reviewer_keys) and "ci" in line:
            reviewer_ok = True
            break
    assert reviewer_ok, (
        "RELEASE-SOP §6 must mark the 2-reviewer rule as enforced by CI "
        "(via CODEOWNERS)"
    )

    tag_keys = ("tag signing", "signed tag", "tag-sign", "tag signature")
    tag_ok = False
    for line in lower.splitlines():
        if any(k in line for k in tag_keys) and "ci" in line:
            tag_ok = True
            break
    assert tag_ok, (
        "RELEASE-SOP §6 must mark tag signing as enforced by CI "
        "(WARN mode is acceptable)"
    )


# ----------------------------------------------------------------------
# A.6 CONTRIBUTING.md unchanged-ish (size sanity)
# ----------------------------------------------------------------------

CONTRIBUTING_BASELINE_BYTES = 12360  # captured at HEAD on 2026-05-27
CONTRIBUTING_TOLERANCE = 0.10  # ±10%


def test_contributing_md_unchanged_size_within_tolerance() -> None:
    path = REPO_ROOT / "CONTRIBUTING.md"
    assert path.exists(), "CONTRIBUTING.md must continue to exist"
    actual = path.stat().st_size
    low = int(CONTRIBUTING_BASELINE_BYTES * (1 - CONTRIBUTING_TOLERANCE))
    high = int(CONTRIBUTING_BASELINE_BYTES * (1 + CONTRIBUTING_TOLERANCE))
    assert low <= actual <= high, (
        f"CONTRIBUTING.md size {actual}B is outside ±10% of baseline "
        f"{CONTRIBUTING_BASELINE_BYTES}B — this PR must NOT meaningfully "
        f"modify CONTRIBUTING.md"
    )
