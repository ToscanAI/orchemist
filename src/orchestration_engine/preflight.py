"""Preflight checks — Definition of Ready enforcement.

Runs locally before any LLM agent is spawned. Zero token cost.
If any check fails, the pipeline refuses to start.

Issue #476.
"""

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Required fields for coding pipeline input JSON
REQUIRED_INPUT_FIELDS = [
    'issue_title',
    'issue_body',
    'repo_path',
    'branch_name',
    'issue_number',
    'repo_url',
    'test_command',
]


@dataclass
class CheckItem:
    """Result of a single preflight check."""
    name: str
    passed: bool
    message: str
    severity: str = "error"  # "error" or "warning"


@dataclass
class PreflightResult:
    """Aggregated result of all preflight checks."""
    passed: bool = True
    checks: List[CheckItem] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def add_check(self, check: CheckItem) -> None:
        self.checks.append(check)
        if not check.passed:
            if check.severity == "error":
                self.passed = False
                self.errors.append(f"[{check.name}] {check.message}")
            else:
                self.warnings.append(f"[{check.name}] {check.message}")

    def summary(self) -> str:
        """Human-readable summary of all checks."""
        lines = []
        for c in self.checks:
            status = "✓" if c.passed else ("✗" if c.severity == "error" else "⚠")
            lines.append(f"  {status} {c.name}: {c.message}")
        return "\n".join(lines)


class PreflightChecker:
    """Runs Definition of Ready checks before pipeline execution.

    Parameters
    ----------
    input_data : dict
        The parsed input JSON for the pipeline run.
    db : optional
        Database instance for dedup checks.
    required_fields : list[str] | None
        Override default required fields. Pass empty list to skip field check.
    """

    def __init__(
        self,
        input_data: Dict[str, Any],
        db: Any = None,
        required_fields: Optional[List[str]] = None,
    ):
        self.input_data = input_data
        self.db = db
        self.required_fields = (
            required_fields if required_fields is not None
            else REQUIRED_INPUT_FIELDS
        )

    def run_all(self) -> PreflightResult:
        """Execute all preflight checks and return aggregated result."""
        result = PreflightResult()

        self._check_input_fields(result)
        self._check_missing_placeholders(result)
        self._check_git_readiness(result)
        self._check_dedup(result)
        self._check_dependencies(result)

        return result

    def _check_input_fields(self, result: PreflightResult) -> None:
        """Verify all required input fields are present and non-empty."""
        missing = []
        empty = []
        for field_name in self.required_fields:
            if field_name not in self.input_data:
                missing.append(field_name)
            elif not str(self.input_data[field_name]).strip():
                empty.append(field_name)

        if missing:
            result.add_check(CheckItem(
                name="input_fields_present",
                passed=False,
                message=f"Missing required fields: {', '.join(missing)}",
            ))
        elif empty:
            result.add_check(CheckItem(
                name="input_fields_present",
                passed=False,
                message=f"Empty required fields: {', '.join(empty)}",
            ))
        else:
            result.add_check(CheckItem(
                name="input_fields_present",
                passed=True,
                message=f"All {len(self.required_fields)} required fields present",
            ))

    def _check_missing_placeholders(self, result: PreflightResult) -> None:
        """Check for <MISSING:key> placeholder patterns in input values."""
        found = []
        for key, value in self.input_data.items():
            val_str = str(value)
            if '<MISSING:' in val_str:
                found.append(f"{key} contains '<MISSING:...>'")

        if found:
            result.add_check(CheckItem(
                name="no_missing_placeholders",
                passed=False,
                message=f"Unresolved placeholders: {'; '.join(found)}",
            ))
        else:
            result.add_check(CheckItem(
                name="no_missing_placeholders",
                passed=True,
                message="No <MISSING:> placeholders found",
            ))

    def _check_git_readiness(self, result: PreflightResult) -> None:
        """Check git state: repo exists, working tree clean, main up to date."""
        repo_path = self.input_data.get('repo_path', '')
        if not repo_path:
            result.add_check(CheckItem(
                name="git_readiness",
                passed=True,
                message="No repo_path specified, skipping git checks",
                severity="warning",
            ))
            return

        repo = Path(repo_path)
        if not repo.exists():
            result.add_check(CheckItem(
                name="git_readiness",
                passed=False,
                message=f"Repository path does not exist: {repo_path}",
            ))
            return

        if not (repo / '.git').exists():
            result.add_check(CheckItem(
                name="git_readiness",
                passed=False,
                message=f"Not a git repository: {repo_path}",
            ))
            return

        # Check working tree is clean
        try:
            status = subprocess.run(
                ['git', 'status', '--porcelain'],
                cwd=repo_path, capture_output=True, text=True, timeout=10,
            )
            if status.stdout.strip():
                dirty_count = len(status.stdout.strip().split('\n'))
                result.add_check(CheckItem(
                    name="git_clean",
                    passed=False,
                    message=f"Working tree has {dirty_count} uncommitted change(s)",
                ))
            else:
                result.add_check(CheckItem(
                    name="git_clean",
                    passed=True,
                    message="Working tree clean",
                ))
        except Exception as exc:
            result.add_check(CheckItem(
                name="git_clean",
                passed=False,
                message=f"Git status check failed: {exc}",
                severity="warning",
            ))

        # Check main is up to date
        try:
            subprocess.run(
                ['git', 'fetch', 'origin', 'main', '--quiet'],
                cwd=repo_path, capture_output=True, text=True, timeout=30,
            )
            diff = subprocess.run(
                ['git', 'rev-list', '--count', 'HEAD..origin/main'],
                cwd=repo_path, capture_output=True, text=True, timeout=10,
            )
            behind = int(diff.stdout.strip()) if diff.stdout.strip() else 0
            if behind > 0:
                result.add_check(CheckItem(
                    name="git_main_current",
                    passed=False,
                    message=f"Local is {behind} commit(s) behind origin/main. Run 'git pull origin main'.",
                    severity="warning",
                ))
            else:
                result.add_check(CheckItem(
                    name="git_main_current",
                    passed=True,
                    message="Main branch is up to date",
                ))
        except Exception as exc:
            result.add_check(CheckItem(
                name="git_main_current",
                passed=True,
                message=f"Could not verify main freshness: {exc}",
                severity="warning",
            ))

    def _check_dedup(self, result: PreflightResult) -> None:
        """Check no active/pending run exists for same issue+repo."""
        if self.db is None:
            result.add_check(CheckItem(
                name="dedup",
                passed=True,
                message="No DB available, skipping dedup check",
                severity="warning",
            ))
            return

        issue_number = str(self.input_data.get('issue_number', ''))
        repo_url = self.input_data.get('repo_url', '')

        if not issue_number:
            result.add_check(CheckItem(
                name="dedup",
                passed=True,
                message="No issue_number, skipping dedup",
            ))
            return

        try:
            # Query active runs from DB
            runs = self.db.list_pipeline_runs(
                status_filter=['running', 'pending', 'pending_review'],
                limit=100,
            )
            duplicates = []
            for run in runs:
                try:
                    run_input = json.loads(run.get('input_json', '{}'))
                    run_issue = str(run_input.get('issue_number', ''))
                    run_repo = run_input.get('repo_url', '')
                    if run_issue == issue_number and run_repo == repo_url:
                        duplicates.append(run['run_id'][:8])
                except (json.JSONDecodeError, KeyError):
                    continue

            if duplicates:
                result.add_check(CheckItem(
                    name="dedup",
                    passed=False,
                    message=f"Active run(s) for issue #{issue_number}: {', '.join(duplicates)}",
                    severity="warning",  # Warning, not error — allow relaunch
                ))
            else:
                result.add_check(CheckItem(
                    name="dedup",
                    passed=True,
                    message=f"No active runs for issue #{issue_number}",
                ))
        except Exception as exc:
            result.add_check(CheckItem(
                name="dedup",
                passed=True,
                message=f"Dedup check failed (non-fatal): {exc}",
                severity="warning",
            ))

    def _check_dependencies(self, result: PreflightResult) -> None:
        """Check if dependent issues are merged (parses 'depends on #NNN' from issue body)."""

        issue_body = self.input_data.get('issue_body', '')
        repo_url = self.input_data.get('repo_url', '')

        # Find patterns like "depends on #123", "after #456", "requires #789"
        dep_pattern = r'(?:depends?\s+on|after|requires|blocked\s+by)\s+#(\d+)'
        deps = re.findall(dep_pattern, issue_body, re.IGNORECASE)

        if not deps:
            result.add_check(CheckItem(
                name="dependencies",
                passed=True,
                message="No dependencies declared",
            ))
            return

        if not repo_url:
            result.add_check(CheckItem(
                name="dependencies",
                passed=True,
                message=f"Dependencies found ({', '.join('#'+d for d in deps)}) but no repo_url to verify",
                severity="warning",
            ))
            return

        # Try to check via gh CLI
        unmerged = []
        for dep_num in deps:
            try:
                # Extract owner/repo from URL
                parts = repo_url.rstrip('/').split('/')
                owner_repo = f"{parts[-2]}/{parts[-1]}"
                check = subprocess.run(
                    ['gh', 'issue', 'view', dep_num, '--repo', owner_repo,
                     '--json', 'state', '--jq', '.state'],
                    capture_output=True, text=True, timeout=10,
                )
                state = check.stdout.strip()
                if state != 'CLOSED':
                    unmerged.append(f"#{dep_num} ({state})")
            except Exception:
                # Can't verify, skip
                continue

        if unmerged:
            result.add_check(CheckItem(
                name="dependencies",
                passed=False,
                message=f"Unresolved dependencies: {', '.join(unmerged)}",
                severity="warning",  # Warning — might be intentional
            ))
        else:
            result.add_check(CheckItem(
                name="dependencies",
                passed=True,
                message=f"All dependencies resolved: {', '.join('#'+d for d in deps)}",
            ))
