"""GitHub Projects v2 GraphQL utilities for sprint board automation (Issue #515).

This module provides best-effort, non-fatal helpers for keeping GitHub Projects v2
sprint boards synchronised with pipeline lifecycle events.

Environment variables (all optional — ``gh`` CLI auth is the fallback):

    GITHUB_PROJECTS_TOKEN
        Personal access token with ``project`` read/write scope for Projects v2
        GraphQL mutations.  Falls back to ``GH_TOKEN`` if unset.

    GITHUB_PROJECTS_FIELD_NAME
        Name of the single-select status field on project boards (default: ``"Status"``).

    GITHUB_PROJECTS_COLUMN_IN_PROGRESS
        Column name for the *pipeline-ready* / launch event (default: ``"In Progress"``).

    GITHUB_PROJECTS_COLUMN_REVIEW
        Column name for the *scoring passed* event (default: ``"Review"``).

    GITHUB_PROJECTS_COLUMN_DONE
        Column name for the *auto-merge success* event (default: ``"Done"``).

    GITHUB_PROJECTS_COLUMN_BLOCKED
        Column name for the *pipeline failure* event (default: ``"Blocked"``).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal GraphQL runner
# ---------------------------------------------------------------------------

def _run_gh_graphql(query: str, variables: dict, token: Optional[str] = None) -> dict:
    """Execute a GitHub GraphQL query via the ``gh api graphql`` CLI.

    Args:
        query:     GraphQL query or mutation string.
        variables: Variables dict to pass along with the query.
        token:     Optional GitHub token; overrides ``GH_TOKEN`` env var if set.

    Returns:
        Parsed ``data`` dict on success; empty dict on any error (non-zero exit,
        JSON parse failure, timeout, or other subprocess error).

    This function never raises — all errors are logged at ``WARNING`` level.
    """
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token

    cmd = [
        "gh", "api", "graphql",
        "-f", f"query={query}",
    ]
    for key, value in variables.items():
        if isinstance(value, (int, float, bool)):
            cmd += ["-F", f"{key}={value}"]
        else:
            cmd += ["-f", f"{key}={value}"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        logger.warning("GitHub GraphQL request timed out after 15s")
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("GitHub GraphQL subprocess error: %s", exc)
        return {}

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        logger.warning(
            "GitHub GraphQL request failed (exit %d): %s",
            result.returncode,
            detail,
        )
        return {}

    try:
        parsed = json.loads(result.stdout)
        return parsed.get("data") or {}
    except (json.JSONDecodeError, AttributeError) as exc:
        logger.warning("Failed to parse GitHub GraphQL response: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Issue project item membership query
# ---------------------------------------------------------------------------

_GET_ISSUE_PROJECT_ITEMS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      projectItems(first: 10) {
        nodes {
          id
          project {
            id
            title
            fields(first: 20) {
              nodes {
                ... on ProjectV2SingleSelectField {
                  id
                  name
                  options { id name }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


def get_issue_project_items(
    repo_owner: str,
    repo_name: str,
    issue_number: int,
    token: Optional[str] = None,
) -> list[dict]:
    """Fetch all GitHub Projects v2 board memberships for a given issue.

    Args:
        repo_owner:   GitHub organisation or user name.
        repo_name:    Repository name (without owner prefix).
        issue_number: GitHub issue number.
        token:        Optional GitHub token.

    Returns:
        List of dicts, each with keys:

        * ``item_id``       — project item node ID
        * ``project_id``    — project node ID
        * ``project_title`` — human-readable project title
        * ``fields``        — list of field node dicts

        Returns an empty list on any error or if the issue is not on any board.
    """
    variables = {"owner": repo_owner, "name": repo_name, "number": issue_number}
    data = _run_gh_graphql(_GET_ISSUE_PROJECT_ITEMS_QUERY, variables, token)

    try:
        nodes = data["repository"]["issue"]["projectItems"]["nodes"]
    except (KeyError, TypeError):
        return []

    items = []
    for node in nodes:
        project = node.get("project") or {}
        items.append({
            "item_id": node.get("id"),
            "project_id": project.get("id"),
            "project_title": project.get("title"),
            "fields": (project.get("fields") or {}).get("nodes") or [],
        })
    return items


# ---------------------------------------------------------------------------
# Board mutation
# ---------------------------------------------------------------------------

_UPDATE_PROJECT_ITEM_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId
    itemId: $itemId
    fieldId: $fieldId
    value: { singleSelectOptionId: $optionId }
  }) {
    projectV2Item { id }
  }
}
"""


def move_issue_on_board(
    repo_owner: str,
    repo_name: str,
    issue_number: Optional[int],
    column_name: str,
    token: Optional[str] = None,
    field_name: str = "Status",
) -> bool:
    """Move a GitHub issue to a specific column on all its Projects v2 boards.

    This is a **best-effort, non-fatal** operation.  Any failure (API error,
    permission error, missing field/column, network error) is logged as a
    ``WARNING`` and the function returns without raising.

    Args:
        repo_owner:   GitHub organisation or user name.
        repo_name:    Repository name (without owner prefix).
        issue_number: GitHub issue number.  ``None`` or ``0`` → no-op (contract 10).
        column_name:  Target column / single-select option name (e.g. ``"Review"``).
        token:        Optional GitHub token (falls back to ``GH_TOKEN`` / ``gh auth``).
        field_name:   Name of the single-select status field on the board.
                      Defaults to ``"Status"``; overridden by the
                      ``GITHUB_PROJECTS_FIELD_NAME`` env var.

    Returns:
        ``True`` if at least one board column was updated successfully (contract 12).
        ``False`` if the issue is not on any board, no matching field/column was
        found, or all transition attempts failed.
    """
    # Contract 10: skip when no issue number is available
    if not issue_number:
        logger.debug("move_issue_on_board: no issue_number provided, skipping")
        return False

    # Allow env-var override of the status field name
    effective_field_name = os.environ.get("GITHUB_PROJECTS_FIELD_NAME", field_name)

    # Fetch project board memberships (contract 5: empty → no-op)
    items = get_issue_project_items(repo_owner, repo_name, issue_number, token)
    if not items:
        logger.debug(
            "move_issue_on_board: issue #%d is not on any project board — skipping",
            issue_number,
        )
        return False

    success_count = 0

    for item in items:
        project_title = item.get("project_title") or "(unknown)"
        item_id = item.get("item_id")
        project_id = item.get("project_id")
        fields = item.get("fields") or []

        try:
            # ── Find the single-select status field (case-insensitive) ──────
            status_field = None
            for field in fields:
                # GraphQL inline fragments on non-matching types return empty dicts
                if not field.get("name"):
                    continue
                if field["name"].lower() == effective_field_name.lower():
                    status_field = field
                    break

            if status_field is None:
                # Contract 7: field not found → WARNING + skip board
                logger.warning(
                    "Status field '%s' not found on board '%s'",
                    effective_field_name,
                    project_title,
                )
                continue

            # ── Find the option matching column_name (case-insensitive) ─────
            option_id = None
            for opt in status_field.get("options") or []:
                if opt.get("name", "").lower() == column_name.lower():
                    option_id = opt["id"]
                    break

            if option_id is None:
                # Contract 6: column not found → WARNING + skip board
                logger.warning(
                    "Column '%s' not found in field '%s' on board '%s'",
                    column_name,
                    effective_field_name,
                    project_title,
                )
                continue

            # ── Execute the mutation ─────────────────────────────────────────
            mutation_variables = {
                "projectId": project_id,
                "itemId": item_id,
                "fieldId": status_field["id"],
                "optionId": option_id,
            }
            mutation_data = _run_gh_graphql(
                _UPDATE_PROJECT_ITEM_MUTATION, mutation_variables, token
            )

            if mutation_data:
                logger.info(
                    "Moved issue #%d to '%s' on board '%s'",
                    issue_number,
                    column_name,
                    project_title,
                )
                success_count += 1
            else:
                # _run_gh_graphql already logged a warning for the error
                logger.warning(
                    "Failed to move issue #%d to '%s' on board '%s' "
                    "(mutation returned no data)",
                    issue_number,
                    column_name,
                    project_title,
                )

        except Exception as exc:  # noqa: BLE001  # contract 8: best-effort, non-fatal
            logger.warning(
                "Board transition for board '%s' failed (non-fatal): %s",
                project_title,
                exc,
            )

    # Contract 12: return True if at least one board was updated
    return success_count > 0


# ---------------------------------------------------------------------------
# Token and column name helpers
# ---------------------------------------------------------------------------

def get_board_token() -> Optional[str]:
    """Return the GitHub token to use for Projects v2 operations.

    Checks ``GITHUB_PROJECTS_TOKEN`` first, then falls back to ``GH_TOKEN``.
    Returns ``None`` if neither is set (the ``gh`` CLI may have a stored token
    from ``gh auth login``).
    """
    return os.environ.get("GITHUB_PROJECTS_TOKEN") or os.environ.get("GH_TOKEN") or None


_COLUMN_DEFAULTS: dict[str, tuple[str, str]] = {
    "in_progress": ("GITHUB_PROJECTS_COLUMN_IN_PROGRESS", "In Progress"),
    "review":      ("GITHUB_PROJECTS_COLUMN_REVIEW",      "Review"),
    "done":        ("GITHUB_PROJECTS_COLUMN_DONE",         "Done"),
    "blocked":     ("GITHUB_PROJECTS_COLUMN_BLOCKED",      "Blocked"),
}


def get_column_name(event: str) -> str:
    """Map a pipeline lifecycle event string to a project board column name.

    Checks env-var overrides before returning the built-in defaults.

    Args:
        event: One of ``"in_progress"``, ``"review"``, ``"done"``, ``"blocked"``.

    Returns:
        Column name string suitable for use as a Projects v2 single-select option.
    """
    env_var, default = _COLUMN_DEFAULTS.get(event, ("", event))
    if env_var:
        return os.environ.get(env_var, default)
    return default
