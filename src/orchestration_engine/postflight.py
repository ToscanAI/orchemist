"""Postflight checks — Definition of Done enforcement.

Runs locally after scoring, before routing. Zero token cost.
Postflight is advisory — warnings are surfaced but score gate remains
the hard gate.

Issue #476.
"""

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


def ensure_branch_pushed(
    repo_path: Union[str, "Path"],
    branch_name: str,
) -> bool:
    """Verify *branch_name* exists on ``origin``; push it if missing.

    Runs ``git ls-remote --heads origin <branch_name>`` to check remote
    state, then pushes with ``--set-upstream`` if the branch is absent.

    Returns:
        ``True``  — branch is on the remote (already there or just pushed).
        ``False`` — push failed; callers should skip PR creation.

    Issue #487.
    """
    repo_path = str(repo_path)
    try:
        ls = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", branch_name],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=repo_path,
        )
        # ls-remote prints a line per matching ref; empty output = not on remote
        if ls.returncode == 0 and branch_name in ls.stdout:
            logger.debug(
                "ensure_branch_pushed: %s already on remote", branch_name
            )
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("ensure_branch_pushed: ls-remote failed: %s", exc)
        return False

    # Branch not on remote — push it.
    logger.info(
        "ensure_branch_pushed: pushing %s to origin", branch_name
    )
    try:
        push = subprocess.run(
            ["git", "push", "--set-upstream", "origin", branch_name],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=repo_path,
        )
        if push.returncode == 0:
            logger.info(
                "ensure_branch_pushed: push succeeded for %s", branch_name
            )
            return True
        logger.warning(
            "ensure_branch_pushed: push failed (rc=%d): %s",
            push.returncode,
            push.stderr.strip(),
        )
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("ensure_branch_pushed: push raised %s", exc)
        return False


@dataclass
class PostflightCheckItem:
    """Result of a single postflight check."""
    name: str
    passed: bool
    message: str
    severity: str = "warning"  # postflight is advisory by default


@dataclass
class PostflightResult:
    """Aggregated result of all postflight checks."""
    passed: bool = True
    checks: List[PostflightCheckItem] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    github_comment: Optional[str] = None

    def add_check(self, check: PostflightCheckItem) -> None:
        self.checks.append(check)
        if not check.passed:
            self.warnings.append(f"[{check.name}] {check.message}")

    def summary(self) -> str:
        """Human-readable summary."""
        lines = []
        for c in self.checks:
            status = "✓" if c.passed else "⚠"
            lines.append(f"  {status} {c.name}: {c.message}")
        return "\n".join(lines)


class PostflightChecker:
    """Runs Definition of Done checks after pipeline scoring.

    Parameters
    ----------
    input_data : dict
        The parsed input JSON for the pipeline run.
    run_id : str
        The pipeline run identifier.
    output_dir : Path
        Where phase outputs were written.
    scoring_passed : bool
        Whether auto-scoring passed.
    scoring_score : float | None
        The scoring score value.
    completed_phases : list[str]
        List of completed phase IDs.
    elapsed_seconds : float | None
        Total pipeline elapsed time.
    """

    def __init__(
        self,
        input_data: Dict[str, Any],
        run_id: str,
        output_dir: Path,
        scoring_passed: bool = False,
        scoring_score: Optional[float] = None,
        completed_phases: Optional[List[str]] = None,
        elapsed_seconds: Optional[float] = None,
    ):
        self.input_data = input_data
        self.run_id = run_id
        self.output_dir = output_dir
        self.scoring_passed = scoring_passed
        self.scoring_score = scoring_score
        self.completed_phases = completed_phases or []
        self.elapsed_seconds = elapsed_seconds

    def run_all(self) -> PostflightResult:
        """Execute all postflight checks and return aggregated result."""
        result = PostflightResult()

        self._check_test_regression(result)
        self._check_phase_completeness(result)
        self._check_branch_pushed(result)
        self._build_github_comment(result)

        return result

    def _check_test_regression(self, result: PostflightResult) -> None:
        """Check if new code was added but no new tests were written."""
        # Read the implement phase output to detect new files
        impl_path = self.output_dir / "implement.md"
        impl_json_path = self.output_dir / "implement.json"

        has_new_code = False
        has_new_tests = False

        # Check implement output for mentions of test files
        for path in [impl_path, impl_json_path]:
            if path.exists():
                content = path.read_text(errors='replace')
                # Look for test file creation/modification
                if re.search(r'test_\w+\.py|tests/|_test\.py|conftest\.py', content):
                    has_new_tests = True
                # Look for code creation
                if re.search(r'\.py\b', content) and not has_new_tests:
                    has_new_code = True

        # Check test phase output for test counts
        test_path = self.output_dir / "test.json"
        if test_path.exists():
            try:
                test_data = json.loads(test_path.read_text())
                test_result_text = str(test_data.get('result', ''))
                # Try to extract test count from output
                match = re.search(r'(\d+)\s+passed', test_result_text)
                if match:
                    test_count = int(match.group(1))
                    result.add_check(PostflightCheckItem(
                        name="test_count",
                        passed=True,
                        message=f"{test_count} tests passed",
                    ))
            except (json.JSONDecodeError, ValueError):
                pass

        if has_new_code and not has_new_tests:
            result.add_check(PostflightCheckItem(
                name="test_regression",
                passed=False,
                message="New code detected but no new test files found. Consider adding tests.",
            ))
        else:
            result.add_check(PostflightCheckItem(
                name="test_regression",
                passed=True,
                message="Test coverage looks reasonable",
            ))

    def _check_phase_completeness(self, result: PostflightResult) -> None:
        """Verify all expected phases completed."""
        expected_phases = ['spec', 'implement', 'review', 'test']
        missing = [p for p in expected_phases if p not in self.completed_phases]

        if missing:
            result.add_check(PostflightCheckItem(
                name="phase_completeness",
                passed=False,
                message=f"Missing phases: {', '.join(missing)}",
            ))
        else:
            result.add_check(PostflightCheckItem(
                name="phase_completeness",
                passed=True,
                message=f"All {len(expected_phases)} core phases completed",
            ))

    def _check_branch_pushed(self, result: PostflightResult) -> None:
        """Verify the feature branch exists on the remote; auto-push if missing.

        Calls :func:`ensure_branch_pushed` with ``repo_path`` and
        ``branch_name`` from the pipeline input.  Records a
        :class:`PostflightCheckItem` regardless of outcome so the result is
        always visible in the GitHub comment.

        Issue #487.
        """
        repo_path = self.input_data.get('repo_path', '')
        branch_name = self.input_data.get('branch_name', '')

        if not repo_path or not branch_name:
            result.add_check(PostflightCheckItem(
                name="branch_pushed",
                passed=False,
                message="Cannot verify remote branch: repo_path or branch_name missing from input",
            ))
            return

        pushed = ensure_branch_pushed(repo_path, branch_name)
        if pushed:
            result.add_check(PostflightCheckItem(
                name="branch_pushed",
                passed=True,
                message=f"Branch '{branch_name}' is on the remote",
            ))
        else:
            result.add_check(PostflightCheckItem(
                name="branch_pushed",
                passed=False,
                message=f"Branch '{branch_name}' could not be pushed to remote — PR creation may fail",
            ))

    def _build_github_comment(self, result: PostflightResult) -> None:
        """Build and optionally post a GitHub comment on the issue."""
        issue_number = self.input_data.get('issue_number', '')
        repo_url = self.input_data.get('repo_url', '')

        if not issue_number or not repo_url:
            return

        # Build comment body
        score_str = f"{self.scoring_score:.3f}" if self.scoring_score is not None else "N/A"
        score_emoji = "✅" if self.scoring_passed else "❌"
        elapsed_str = f"{self.elapsed_seconds:.0f}s" if self.elapsed_seconds else "N/A"

        phases_str = " → ".join(self.completed_phases) if self.completed_phases else "none"

        warnings_section = ""
        if result.warnings:
            warnings_str = "\n".join(f"  - {w}" for w in result.warnings)
            warnings_section = f"\n\n**⚠️ Warnings:**\n{warnings_str}"

        comment = (
            f"### 🏭 Orchemist Pipeline Run `{self.run_id[:8]}`\n\n"
            f"| Metric | Value |\n"
            f"|--------|-------|\n"
            f"| Score | {score_emoji} {score_str} |\n"
            f"| Phases | {phases_str} |\n"
            f"| Duration | {elapsed_str} |\n"
            f"| Scoring | {'PASSED' if self.scoring_passed else 'FAILED'} |"
            f"{warnings_section}"
        )

        result.github_comment = comment

        # Post to GitHub
        try:
            parts = repo_url.rstrip('/').split('/')
            owner_repo = f"{parts[-2]}/{parts[-1]}"
            post = subprocess.run(
                ['gh', 'issue', 'comment', str(issue_number),
                 '--repo', owner_repo, '--body', comment],
                capture_output=True, text=True, timeout=15,
            )
            if post.returncode == 0:
                result.add_check(PostflightCheckItem(
                    name="github_comment",
                    passed=True,
                    message=f"Posted run summary to issue #{issue_number}",
                ))
                logger.info("Postflight: posted comment to issue #%s", issue_number)
            else:
                result.add_check(PostflightCheckItem(
                    name="github_comment",
                    passed=False,
                    message=f"Failed to post comment: {post.stderr[:200]}",
                ))
        except Exception as exc:
            result.add_check(PostflightCheckItem(
                name="github_comment",
                passed=False,
                message=f"GitHub comment failed: {exc}",
            ))
