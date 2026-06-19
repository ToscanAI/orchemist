"""GitHub issue comment / label helpers and pipeline-input builder.

Thin wrappers over the GitHub CLI (``gh``) for posting comments and
managing labels on issues, plus :func:`generate_pipeline_input` which
assembles a ``coding-pipeline-v1`` input dict from issue metadata.

The ``gh`` CLI must be authenticated in the current environment.  All
network helpers log failures as warnings and return a falsy value rather
than raising, so callers can continue without the side effect.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..text_utils import slugify_branch

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# post_github_comment — post a comment to a GitHub issue via `gh api`
# ---------------------------------------------------------------------------


def post_github_comment(repo: str, issue_number: int, body: str) -> Optional[str]:
    """Post a comment to a GitHub issue via ``gh api``.

    Uses the GitHub CLI (``gh``) to create a comment on the specified issue.
    The ``gh`` CLI must be authenticated in the current environment.

    Args:
        repo:         Repository slug (e.g. ``"owner/repo"``).
        issue_number: GitHub issue number.
        body:         Markdown body text for the comment.

    Returns:
        The comment HTML URL (e.g. ``"https://github.com/owner/repo/issues/1#issuecomment-123"``)
        on success.  ``None`` on any failure — errors are logged as warnings,
        not raised, so callers can continue without a comment being posted.

    Example::

        url = post_github_comment("owner/repo", 42, "🤖 Pipeline launched!")
        if url:
            print(f"Comment posted at {url}")
    """
    import subprocess  # noqa: PLC0415

    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/{issue_number}/comments",
                "--method",
                "POST",
                "--field",
                f"body={body}",
                "--jq",
                ".html_url",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
        logger.warning(
            "post_github_comment: gh api failed (rc=%d): %s",
            result.returncode,
            result.stderr.strip(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("post_github_comment: error posting comment: %s", exc)

    return None


# ---------------------------------------------------------------------------
# generate_pipeline_input — build coding-pipeline-v1 input dict (Issue #511)
# ---------------------------------------------------------------------------


def generate_pipeline_input(
    issue_number: int,
    title: str,
    body: str,
    repo: str,
    repo_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a ``coding-pipeline-v1`` input dict for an issue.

    Constructs a deterministic branch name from the issue number and title
    slug, then assembles the full input dict suitable for ``--input-file``.

    Args:
        issue_number: GitHub issue number.
        title:        Issue title — used to derive the branch slug.
        body:         Issue body / description.
        repo:         Repository slug (e.g. ``"owner/repo"``).
        repo_path:    Optional local filesystem path to the repository.
                      When ``None``, the key is omitted from the dict.

    Returns:
        Dict with pipeline input variables for ``coding-pipeline-v1``::

            {
                "issue_number": 42,
                "repo": "owner/repo",
                "title": "Fix crash on empty input",
                "body": "...",
                "branch_name": "feat/42-fix-crash-on-empty-input",
                # optional:
                "repo_path": "/path/to/repo",
            }

    Example::

        inp = generate_pipeline_input(42, "Fix NPE in runner", "...", "org/repo")
        # inp["branch_name"] == "feat/42-fix-npe-in-runner"
    """
    slug = slugify_branch(title)
    branch_name = f"feat/{issue_number}-{slug}"

    result: Dict[str, Any] = {
        "issue_number": issue_number,
        "repo": repo,
        "title": title,
        "body": body,
        "branch_name": branch_name,
    }
    if repo_path is not None:
        result["repo_path"] = repo_path

    return result


# ---------------------------------------------------------------------------
# remove_github_label — DELETE a label from a GitHub issue (Issue #511)
# ---------------------------------------------------------------------------


def remove_github_label(repo: str, issue_number: int, label: str) -> bool:
    """Remove a label from a GitHub issue via ``gh api`` DELETE.

    Uses the GitHub CLI (``gh``) to call the REST API.  The label name is
    URL-encoded to handle labels with spaces or special characters.

    Args:
        repo:         Repository slug (e.g. ``"owner/repo"``).
        issue_number: GitHub issue number.
        label:        Label name to remove (URL-encoded internally).

    Returns:
        ``True`` when the label was removed successfully (exit code 0).
        ``False`` on any failure (gh not found, API error, timeout).

    Example::

        ok = remove_github_label("owner/repo", 42, "pipeline-ready")
        # ok == True when the label was successfully removed
    """
    import subprocess  # noqa: PLC0415
    from urllib.parse import quote  # noqa: PLC0415

    encoded_label = quote(label, safe="")
    endpoint = f"repos/{repo}/issues/{issue_number}/labels/{encoded_label}"

    try:
        result = subprocess.run(
            ["gh", "api", endpoint, "--method", "DELETE"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode == 0:
            return True
        logger.warning(
            "remove_github_label: gh api DELETE failed (rc=%d): %s",
            result.returncode,
            result.stderr.strip(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("remove_github_label: error removing label %r: %s", label, exc)

    return False


# ---------------------------------------------------------------------------
# add_github_label — POST a label onto a GitHub issue (Issue #514)
# ---------------------------------------------------------------------------


def add_github_label(repo: str, issue_number: int, label: str) -> bool:
    """Apply a label to a GitHub issue via ``gh api`` POST.

    Symmetric counterpart to :func:`remove_github_label`.  Uses the GitHub CLI
    (``gh``) to call the REST Labels API.

    Args:
        repo:         Repository slug (e.g. ``"owner/repo"``).
        issue_number: GitHub issue number.
        label:        Label name to apply.

    Returns:
        ``True`` when the label was applied successfully (exit code 0).
        ``False`` on any failure (gh not found, API error, timeout).

    Example::

        ok = add_github_label("owner/repo", 42, "pipeline-ready")
        # ok == True when the label was successfully applied
    """
    import subprocess  # noqa: PLC0415

    endpoint = f"repos/{repo}/issues/{issue_number}/labels"
    try:
        result = subprocess.run(
            ["gh", "api", endpoint, "--method", "POST", "--field", f"labels[]={label}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode == 0:
            return True
        logger.warning(
            "add_github_label: gh api POST failed (rc=%d): %s",
            result.returncode,
            result.stderr.strip(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("add_github_label: error adding label %r: %s", label, exc)

    return False


# ---------------------------------------------------------------------------
# get_github_issue_labels — GET label names for a GitHub issue (Issue #514)
# ---------------------------------------------------------------------------


def get_github_issue_labels(repo: str, issue_number: int) -> list:
    """Return the list of label names on a GitHub issue via ``gh api``.

    Args:
        repo:         Repository slug (e.g. ``"owner/repo"``).
        issue_number: GitHub issue number.

    Returns:
        List of label name strings.  Returns ``[]`` on any failure.

    Example::

        labels = get_github_issue_labels("owner/repo", 42)
        # e.g. ["pipeline-ready", "bug"]
    """
    import subprocess  # noqa: PLC0415

    endpoint = f"repos/{repo}/issues/{issue_number}"
    try:
        result = subprocess.run(
            ["gh", "api", endpoint, "--jq", ".labels[].name"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode == 0:
            return [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        logger.warning(
            "get_github_issue_labels: gh api failed (rc=%d): %s",
            result.returncode,
            result.stderr.strip(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("get_github_issue_labels: error: %s", exc)

    return []
