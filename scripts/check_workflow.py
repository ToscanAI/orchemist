#!/usr/bin/env python3
"""check_workflow.py — structural lint for .github/workflows/ci.yml.

Closes #889.

PROBLEM
-------
Issue #889 adds a new `playwright-e2e` job to `.github/workflows/ci.yml` that
gates PR merges on green Playwright e2e tests. The job has a load-bearing
structural shape (engine bringup + Next dev bringup + Playwright invocation +
browser cache + docs-only gate + cleanup) that is easy to break with a sloppy
edit — and the workflow itself only runs on GH Actions, so a broken edit can
land before being noticed.

This script asserts the structural shape against the PARSED YAML dict (not
raw textual grep), so:

  - Indentation/quoting changes are tolerated.
  - Comment-only stubs (`# orch api-server is great`) do NOT satisfy the
    engine-bringup rule — the substring must appear inside a `step.run` value.
  - The existing test-job's substantive steps are protected — a sloppy edit
    that deletes the pytest step fails the lint.

DESIGN
------
The script walks the parsed dict and applies one rule per behavioral contract
from .orchemist/runs/<id>/behavioral.md. For each rule that fails, the script
prints `MISSING: <field-path> — <reason>` on its own line. On all-pass, it
prints `OK: ci.yml has playwright-e2e job with expected shape`.

Exit codes:
    0 — no missing rules
    1 — one or more missing rules (or unreadable workflow file)

This is the structural acceptance gate. It runs in milliseconds, doesn't
require GitHub Actions hardware, and can be invoked from a pre-commit hook
or directly from a CI step.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover — PyYAML is a transitive dep
    print(
        "MISSING: PyYAML import failed — install via `pip install pyyaml`",
        file=sys.stderr,
    )
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load_workflow(path: Path) -> dict[str, Any]:
    """Parse a workflow YAML into a dict.

    Coerces PyYAML's `on:` → True-key quirk back to string 'on' for stable
    lookup.

    Raises:
        FileNotFoundError: if the workflow file does not exist
        yaml.YAMLError: if the file is not valid YAML
    """
    with path.open() as fh:
        doc = yaml.safe_load(fh) or {}
    if True in doc and "on" not in doc:
        doc["on"] = doc.pop(True)
    return doc


def _extract_dorny_filters(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Find dorny/paths-filter step and parse its `with.filters` block.

    The `filters` value can be either:
      - A YAML-block string (multi-line) — the typical action input.
      - A pre-parsed dict (when the workflow author inlined the dict).

    Returns the parsed dict (label → list of globs), or empty dict if no
    paths-filter step is found.
    """
    for s in steps:
        uses = s.get("uses", "")
        if uses.startswith("dorny/paths-filter@"):
            filters = s.get("with", {}).get("filters")
            if isinstance(filters, str):
                return yaml.safe_load(filters) or {}
            if isinstance(filters, dict):
                return filters
    return {}


def check_workflow(path: Path) -> list[str]:
    """Run all structural checks. Return list of missing-rule messages.

    Empty list = all checks passed.
    """
    missing: list[str] = []

    try:
        doc = _load_workflow(path)
    except FileNotFoundError:
        return [f"MISSING: workflow file not found at {path}"]
    except yaml.YAMLError as exc:
        return [f"MISSING: workflow file at {path} is not valid YAML — {exc}"]

    jobs = doc.get("jobs", {})

    # ------------------------------------------------------------------
    # Group 1 — playwright-e2e job shape
    # ------------------------------------------------------------------
    pw_job = jobs.get("playwright-e2e")
    if pw_job is None:
        # Without the job, every subsequent rule is moot — short-circuit with
        # one line for clarity.
        missing.append("MISSING: jobs.playwright-e2e — job not defined")
        return missing

    if pw_job.get("runs-on") != "ubuntu-latest":
        missing.append(
            f"MISSING: jobs.playwright-e2e.runs-on — expected 'ubuntu-latest', "
            f"got {pw_job.get('runs-on')!r}"
        )

    timeout = pw_job.get("timeout-minutes")
    if not isinstance(timeout, int) or not (1 <= timeout <= 30):
        missing.append(
            f"MISSING: jobs.playwright-e2e.timeout-minutes — expected int in "
            f"[1, 30], got {timeout!r}"
        )

    pw_steps = pw_job.get("steps", [])

    # Engine bringup in a real step.run (comments don't satisfy this)
    if not any("orch api-server" in s.get("run", "") for s in pw_steps):
        missing.append(
            "MISSING: jobs.playwright-e2e.steps[*].run — no step invokes "
            "`orch api-server`"
        )

    # Playwright invocation step with both `npx playwright test` and `PW_BASE_URL`
    if not any(
        "npx playwright test" in s.get("run", "") and "PW_BASE_URL" in s.get("run", "")
        for s in pw_steps
    ):
        missing.append(
            "MISSING: jobs.playwright-e2e.steps[*].run — no step has BOTH "
            "`npx playwright test` and `PW_BASE_URL`"
        )

    # actions/cache@v4 targeting ms-playwright
    if not any(
        s.get("uses", "").startswith("actions/cache@v4")
        and "ms-playwright" in str(s.get("with", {}).get("path", ""))
        for s in pw_steps
    ):
        missing.append(
            "MISSING: jobs.playwright-e2e.steps[*] — no actions/cache@v4 step "
            "targets the `~/.cache/ms-playwright` directory"
        )

    # dorny/paths-filter step in the playwright-e2e job
    has_dorny_pw = any(
        s.get("uses", "").startswith("dorny/paths-filter@") for s in pw_steps
    )
    if not has_dorny_pw:
        missing.append(
            "MISSING: jobs.playwright-e2e.steps[*] — no dorny/paths-filter step "
            "(docs-only gate)"
        )

    # All four docs-only globs present
    if has_dorny_pw:
        filters = _extract_dorny_filters(pw_steps)
        all_globs: list[str] = []
        for v in filters.values():
            if isinstance(v, list):
                all_globs.extend(v)
            elif isinstance(v, str):
                all_globs.append(v)
        needed = {"**.md", "docs/**", "LICENSE", ".github/ISSUE_TEMPLATE/**"}
        missing_globs = needed - set(all_globs)
        if missing_globs:
            missing.append(
                f"MISSING: jobs.playwright-e2e dorny/paths-filter.with.filters — "
                f"missing globs: {sorted(missing_globs)}"
            )

    # wait_for_url.sh invoked >= 2 times (engine + Next dev)
    wait_count = sum(
        "scripts/wait_for_url.sh" in s.get("run", "") for s in pw_steps
    )
    if wait_count < 2:
        missing.append(
            f"MISSING: jobs.playwright-e2e.steps[*].run — "
            f"scripts/wait_for_url.sh must be invoked at least twice "
            f"(engine + Next dev probes), got {wait_count}"
        )

    # upload-artifact steps all have if: always()
    upload_steps = [
        s for s in pw_steps
        if s.get("uses", "").startswith("actions/upload-artifact@")
    ]
    if not upload_steps:
        missing.append(
            "MISSING: jobs.playwright-e2e.steps[*] — no actions/upload-artifact "
            "step (Playwright report not uploaded)"
        )
    else:
        for s in upload_steps:
            if s.get("if") != "always()":
                missing.append(
                    f"MISSING: jobs.playwright-e2e upload-artifact step "
                    f"{s.get('name', '?')!r} — must have `if: always()`"
                )

    # final step is cleanup with if: always()
    if pw_steps:
        cleanup = pw_steps[-1]
        if cleanup.get("if") != "always()":
            missing.append(
                f"MISSING: jobs.playwright-e2e.steps[-1].if — expected "
                f"`always()`, got {cleanup.get('if')!r}"
            )

    # ------------------------------------------------------------------
    # Group 2 — existing test job preservation
    # ------------------------------------------------------------------
    test_job = jobs.get("test")
    if test_job is None:
        missing.append("MISSING: jobs.test — existing test job missing")
    else:
        matrix = test_job.get("strategy", {}).get("matrix", {}).get(
            "python-version", []
        )
        if matrix != ["3.10", "3.11", "3.12"]:
            missing.append(
                f"MISSING: jobs.test.strategy.matrix.python-version — expected "
                f"['3.10', '3.11', '3.12'], got {matrix!r}"
            )

        test_steps = test_job.get("steps", [])
        names = {s.get("name", "") for s in test_steps}
        expected_names = {
            "Checkout code",
            "Install dependencies",
            "Run tests",
            "Validate bundled templates",
            "Template drift lint",
        }
        missing_names = expected_names - names
        if missing_names:
            missing.append(
                f"MISSING: jobs.test.steps[*].name — pre-existing step names "
                f"removed: {sorted(missing_names)}"
            )

        # dorny/paths-filter on the test job too
        if not any(
            s.get("uses", "").startswith("dorny/paths-filter@") for s in test_steps
        ):
            missing.append(
                "MISSING: jobs.test.steps[*] — no dorny/paths-filter step on "
                "the existing test job (docs-only gate)"
            )

    return missing


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Structural lint for .github/workflows/ci.yml — asserts the "
            "playwright-e2e job has the expected shape and the existing "
            "test job is preserved."
        )
    )
    parser.add_argument(
        "--workflow",
        type=Path,
        default=DEFAULT_WORKFLOW,
        help="Path to the workflow YAML (default: .github/workflows/ci.yml)",
    )
    args = parser.parse_args()

    missing = check_workflow(args.workflow)
    if missing:
        for line in missing:
            print(line)
        return 1

    print("OK: ci.yml has playwright-e2e job with expected shape")
    return 0


if __name__ == "__main__":
    sys.exit(main())
