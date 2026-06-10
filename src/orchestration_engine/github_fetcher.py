"""github_fetcher.py — Fetch GitHub issue data via ``gh`` CLI (Issue #507).

Used by ``orch launch --issue <number>`` to auto-populate pipeline input,
eliminating the need to manually craft ``input.json`` files for every run.

The ``gh`` CLI must be authenticated in the current environment.  If it
is unavailable or the API call fails, :func:`fetch_github_issue` returns
``None`` and logs a warning — callers decide whether to abort or continue.

Merge strategy
--------------
:data:`_CANONICAL_FIELDS` are always written from GitHub data into the
pipeline's ``initial_input``, overriding any user-supplied values for those
keys.  All other fetched fields fill *missing* keys only — the user's
explicit ``--input`` / ``--input-file`` values take precedence.

Typical usage::

    from orchestration_engine.github_fetcher import fetch_github_issue

    data = fetch_github_issue(repo="owner/repo", issue_number=507)
    if data:
        initial_input = data.merge_into(initial_input)

Or via the class directly::

    from orchestration_engine.github_fetcher import GitHubIssueFetcher

    fetcher = GitHubIssueFetcher(timeout=30)
    data = fetcher.fetch("owner/repo", 507)
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
    "_CANONICAL_FIELDS",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Fields that are always overwritten from GitHub data when merging into
#: ``initial_input``.  Non-canonical fields fill missing keys only.
_CANONICAL_FIELDS: frozenset = frozenset(
    {"issue_number", "title", "body", "labels", "assignees", "milestone"}
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class GitHubIssueData:
    """Structured representation of a GitHub issue fetched via ``gh api``.

    Attributes:
        issue_number: GitHub issue number (e.g. ``507``).
        title:        Issue title string.
        body:         Issue body / description (may be empty string).
        labels:       List of label name strings.
        assignees:    List of assignee login strings.
        milestone:    Milestone title string, or ``None`` if unset.
        state:        Issue state — ``"open"`` or ``"closed"``.
        html_url:     HTML URL of the issue on GitHub.
        repo:         Repository slug used to fetch this issue (``"owner/repo"``).
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
        """Return a dict suitable for use as ``initial_input`` in a pipeline.

        Args:
            canonical_only: When ``True``, return only the
                :data:`_CANONICAL_FIELDS` subset — the fields that always
                override user-provided input.  When ``False`` (default),
                return all fields.

        Returns:
            Dict with pipeline-ready keys and values.
        """
        all_data: Dict[str, Any] = {
            "issue_number": self.issue_number,
            "title": self.title,
            "body": self.body,
            "labels": self.labels,
            "assignees": self.assignees,
            "milestone": self.milestone,
            "state": self.state,
            "html_url": self.html_url,
            "repo": self.repo,
        }
        if canonical_only:
            return {k: v for k, v in all_data.items() if k in _CANONICAL_FIELDS}
        return all_data

    def merge_into(self, initial_input: Dict[str, Any]) -> Dict[str, Any]:
        """Merge this issue's data into *initial_input* following the merge strategy.

        * Non-canonical GitHub fields fill only **missing** keys in
          *initial_input* — caller values win for those keys.
        * :data:`_CANONICAL_FIELDS` (``issue_number``, ``title``, ``body``,
          ``labels``, ``assignees``, ``milestone``) **always** override whatever
          was in *initial_input*.

        Args:
            initial_input: The existing pipeline input dict (not mutated).

        Returns:
            New merged dict (does not mutate *initial_input*).
        """
        # Start with all GitHub data (fills any missing keys).
        result: Dict[str, Any] = dict(self.to_input_dict())
        # Caller's explicit values override non-canonical GitHub fields.
        result.update(initial_input)
        # Canonical fields always come from GitHub — force overwrite.
        result.update(self.to_input_dict(canonical_only=True))
        return result


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------


class GitHubIssueFetcher:
    """Fetch a single GitHub issue via the ``gh`` CLI.

    The class is intentionally lightweight — no external dependencies beyond
    the standard library and the ``gh`` CLI binary.  It follows the same
    constructor pattern as other automation helpers in the engine so it
    integrates naturally with the rest of the automation layer.

    Args:
        timeout: Subprocess timeout in seconds (default: ``15``).

    Example::

        fetcher = GitHubIssueFetcher()
        data = fetcher.fetch("owner/repo", 507)
        if data:
            print(data.title)
    """

    def __init__(self, timeout: int = 15) -> None:
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch(self, repo: str, issue_number: int) -> Optional[GitHubIssueData]:
        """Fetch issue *issue_number* from *repo* and return structured data.

        Calls ``gh api repos/{repo}/issues/{number}`` and parses the JSON
        response into a :class:`GitHubIssueData` instance.

        Args:
            repo:         Repository slug, e.g. ``"owner/repo"``.
            issue_number: The GitHub issue number to fetch.

        Returns:
            A :class:`GitHubIssueData` instance on success, or ``None`` if
            the ``gh`` CLI is unavailable, returns a non-zero exit code, or
            the response is not valid JSON.  Failures are logged as warnings.
        """
        try:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{repo}/issues/{issue_number}",
                ],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError:
            logger.warning("GitHubIssueFetcher: 'gh' CLI not found — cannot fetch issue")
            return None
        except subprocess.TimeoutExpired:
            logger.warning(
                "GitHubIssueFetcher: gh api timed out after %ds for %s#%d",
                self._timeout,
                repo,
                issue_number,
            )
            return None
        except OSError as exc:
            logger.warning(
                "GitHubIssueFetcher: OS error running gh: %s",
                exc,
            )
            return None

        if result.returncode != 0:
            logger.warning(
                "GitHubIssueFetcher: gh api returned rc=%d for %s#%d: %s",
                result.returncode,
                repo,
                issue_number,
                result.stderr.strip(),
            )
            return None

        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            logger.warning(
                "GitHubIssueFetcher: invalid JSON from gh api: %s",
                exc,
            )
            return None

        return self._parse_response(raw, repo=repo)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(data: Dict[str, Any], repo: str) -> GitHubIssueData:
        """Convert a raw GitHub API issue dict into :class:`GitHubIssueData`.

        Args:
            data: The parsed JSON dict returned by ``gh api``.
            repo: Repository slug (echoed into the result).

        Returns:
            A populated :class:`GitHubIssueData` instance.  Missing or
            ``None`` fields from the API response are replaced with safe
            defaults.
        """
        labels: List[str] = [
            lbl.get("name", "")
            for lbl in (data.get("labels") or [])
            if isinstance(lbl, dict) and lbl.get("name")
        ]
        assignees: List[str] = [
            usr.get("login", "")
            for usr in (data.get("assignees") or [])
            if isinstance(usr, dict) and usr.get("login")
        ]
        milestone_obj = data.get("milestone") or {}
        milestone: Optional[str] = (
            milestone_obj.get("title")
            if isinstance(milestone_obj, dict) and milestone_obj
            else None
        )

        return GitHubIssueData(
            issue_number=int(data.get("number", 0)),
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            labels=labels,
            assignees=assignees,
            milestone=milestone,
            state=str(data.get("state") or "open"),
            html_url=str(data.get("html_url") or ""),
            repo=repo,
        )


# ---------------------------------------------------------------------------
# Module-level convenience wrapper
# ---------------------------------------------------------------------------


def fetch_github_issue(
    repo: str,
    issue_number: int,
) -> Optional[GitHubIssueData]:
    """Convenience wrapper around :class:`GitHubIssueFetcher`.

    Creates a fresh fetcher with the default timeout and calls
    :meth:`GitHubIssueFetcher.fetch`.

    Args:
        repo:         Repository slug (e.g. ``"owner/repo"``).
        issue_number: GitHub issue number to fetch.

    Returns:
        A :class:`GitHubIssueData` instance on success, or ``None`` on failure.

    Example::

        data = fetch_github_issue("Rene-Rivera/orchestration-engine", 507)
        if data:
            initial_input = data.merge_into({})
    """
    return GitHubIssueFetcher().fetch(repo=repo, issue_number=issue_number)
