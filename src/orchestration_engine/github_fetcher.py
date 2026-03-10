"""GitHub issue fetcher — auto-populate pipeline input from a GitHub issue (Issue #507).

Fetches issue metadata from GitHub via the ``gh`` CLI and returns a structured
:class:`GitHubIssueData` dataclass.  The result's
:meth:`~GitHubIssueData.to_input_dict` method produces a dict that can be
merged directly into a pipeline's ``initial_input``.

Merge strategy
--------------
Canonical issue fields (``issue_number``, ``title``, ``body``, ``labels``,
``assignees``, ``milestone``) always come from GitHub.  Any other keys already
present in ``initial_input`` are preserved; missing keys are filled from GitHub
data.

Graceful fallback
-----------------
If ``gh`` is unavailable or the API call fails, :func:`fetch_github_issue`
returns ``None`` and logs a warning.  Callers should continue with the
original ``initial_input`` unchanged.

Typical usage::

    from orchestration_engine.github_fetcher import fetch_github_issue

    issue_data = fetch_github_issue(repo="owner/repo", issue_number=42)
    if issue_data:
        initial_input = {**issue_data.to_input_dict(), **initial_input}
        # Canonical fields always overwritten by GitHub data:
        initial_input.update(issue_data.to_input_dict(canonical_only=True))
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = [
    "GitHubIssueData",
    "GitHubIssueFetcher",
    "fetch_github_issue",
]

logger = logging.getLogger(__name__)

# Canonical fields that are ALWAYS overwritten from GitHub data when merging.
_CANONICAL_FIELDS = frozenset(
    {"issue_number", "title", "body", "labels", "assignees", "milestone"}
)


# ---------------------------------------------------------------------------
# GitHubIssueData dataclass
# ---------------------------------------------------------------------------


@dataclass
class GitHubIssueData:
    """Structured representation of a GitHub issue fetched via the ``gh`` CLI.

    Attributes:
        issue_number: GitHub issue number.
        title:        Issue title string.
        body:         Issue body / description (may be empty string).
        labels:       List of label name strings.
        assignees:    List of assignee login strings.
        milestone:    Milestone title string, or ``None`` when no milestone.
        state:        Issue state (``"open"`` or ``"closed"``).
        html_url:     Full URL to the issue on GitHub.
        repo:         Repository slug (e.g. ``"owner/repo"``).
    """

    issue_number: int
    title: str
    body: str
    labels: List[str] = field(default_factory=list)
    assignees: List[str] = field(default_factory=list)
    milestone: Optional[str] = None
    state: str = "open"
    html_url: str = ""
    repo: str = ""

    def to_input_dict(self, canonical_only: bool = False) -> Dict[str, Any]:
        """Return a dict suitable for merging into a pipeline's ``initial_input``.

        Args:
            canonical_only: When ``True``, return only the canonical issue
                            fields (those that always override existing input).
                            When ``False`` (default), return all fields.

        Returns:
            Dict with pipeline-ready keys and values.
        """
        all_data: Dict[str, Any] = {
            # Canonical fields — always from GitHub
            "issue_number": self.issue_number,
            "title": self.title,
            "body": self.body,
            "labels": self.labels,
            "assignees": self.assignees,
            "milestone": self.milestone,
            # Extra context fields
            "state": self.state,
            "html_url": self.html_url,
            "repo": self.repo,
        }
        if canonical_only:
            return {k: v for k, v in all_data.items() if k in _CANONICAL_FIELDS}
        return all_data

    def merge_into(self, initial_input: Dict[str, Any]) -> Dict[str, Any]:
        """Merge this issue's data into *initial_input* following the merge strategy.

        Non-canonical keys from GitHub fill any missing keys in *initial_input*.
        Canonical issue fields always override whatever was in *initial_input*.

        Args:
            initial_input: The existing pipeline input dict (not mutated).

        Returns:
            New dict with merged values.
        """
        full = self.to_input_dict()
        result = dict(full)           # start with GitHub data (fills missing keys)
        result.update(initial_input)  # caller overrides non-canonical keys
        result.update(self.to_input_dict(canonical_only=True))  # canonical always wins
        return result


# ---------------------------------------------------------------------------
# GitHubIssueFetcher
# ---------------------------------------------------------------------------


class GitHubIssueFetcher:
    """Fetch GitHub issue data via the ``gh`` CLI.

    Args:
        timeout: Subprocess timeout in seconds.  Defaults to 15.

    Example::

        fetcher = GitHubIssueFetcher()
        data = fetcher.fetch("owner/repo", 42)
        if data:
            print(data.title)
    """

    def __init__(self, timeout: int = 15) -> None:
        self._timeout = timeout

    def fetch(self, repo: str, issue_number: int) -> Optional[GitHubIssueData]:
        """Fetch issue *issue_number* from *repo* and return :class:`GitHubIssueData`.

        Calls ``gh api repos/{repo}/issues/{number}`` and parses the JSON
        response.  Returns ``None`` on any failure (``gh`` not found,
        non-zero exit, network error, JSON parse error).

        Args:
            repo:         Repository slug (e.g. ``"owner/repo"``).
            issue_number: GitHub issue number (positive integer).

        Returns:
            :class:`GitHubIssueData` on success; ``None`` on failure.
        """
        try:
            result = subprocess.run(
                ["gh", "api", f"repos/{repo}/issues/{issue_number}"],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError:
            logger.warning(
                "GitHubIssueFetcher: 'gh' CLI not found — skipping issue fetch"
            )
            return None
        except subprocess.TimeoutExpired:
            logger.warning(
                "GitHubIssueFetcher: gh api timed out after %ds for %s#%d",
                self._timeout, repo, issue_number,
            )
            return None
        except OSError as exc:
            logger.warning(
                "GitHubIssueFetcher: OS error running gh: %s", exc
            )
            return None

        if result.returncode != 0:
            logger.warning(
                "GitHubIssueFetcher: gh api returned rc=%d for %s#%d: %s",
                result.returncode, repo, issue_number, result.stderr.strip(),
            )
            return None

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            logger.warning(
                "GitHubIssueFetcher: failed to parse gh api response: %s", exc
            )
            return None

        return self._parse_response(data, repo=repo)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(data: Dict[str, Any], repo: str) -> GitHubIssueData:
        """Parse the raw GitHub API JSON dict into a :class:`GitHubIssueData`.

        Args:
            data: Parsed JSON dict from ``gh api``.
            repo: Repository slug (used to populate the ``repo`` field).

        Returns:
            :class:`GitHubIssueData` instance.
        """
        labels: List[str] = [
            lbl.get("name", "") for lbl in data.get("labels", []) if lbl.get("name")
        ]
        assignees: List[str] = [
            usr.get("login", "") for usr in data.get("assignees", []) if usr.get("login")
        ]
        milestone_data = data.get("milestone") or {}
        milestone: Optional[str] = milestone_data.get("title") if milestone_data else None

        return GitHubIssueData(
            issue_number=int(data.get("number", 0)),
            title=str(data.get("title", "")),
            body=str(data.get("body") or ""),
            labels=labels,
            assignees=assignees,
            milestone=milestone,
            state=str(data.get("state", "open")),
            html_url=str(data.get("html_url", "")),
            repo=repo,
        )


# ---------------------------------------------------------------------------
# Module-level convenience wrapper
# ---------------------------------------------------------------------------

_default_fetcher = GitHubIssueFetcher()


def fetch_github_issue(repo: str, issue_number: int) -> Optional[GitHubIssueData]:
    """Fetch GitHub issue data using the module-level default fetcher.

    Convenience wrapper around :class:`GitHubIssueFetcher`.  Suitable for
    one-off calls without constructing an instance.

    Args:
        repo:         Repository slug (e.g. ``"owner/repo"``).
        issue_number: GitHub issue number.

    Returns:
        :class:`GitHubIssueData` on success; ``None`` on any failure.

    Example::

        data = fetch_github_issue("owner/repo", 42)
        if data:
            merged = data.merge_into(initial_input)
    """
    return _default_fetcher.fetch(repo=repo, issue_number=issue_number)
