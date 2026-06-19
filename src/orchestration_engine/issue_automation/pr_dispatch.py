"""PR / comment dispatch helpers — post pipeline results back to GitHub.

Opens pull requests (:func:`create_pr_for_issue`, :func:`create_content_pr`)
and posts result / failure comments (:func:`post_pipeline_result_comment`,
:func:`post_failure_summary_comment`) for a triggering GitHub issue, plus the
unified :func:`post_result_to_issue` dispatch facade.

The ``gh`` CLI must be authenticated in the current environment.  All network
helpers log failures as warnings and return a falsy value rather than raising,
so callers can continue without the side effect.

``post_github_comment`` is resolved through the package facade
(``orchestration_engine.issue_automation``) at call time via the ``_ia`` alias
so that the immutable tests, which patch the facade name, still intercept it.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import orchestration_engine.issue_automation as _ia

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# create_pr_for_issue — open a PR linked to a triggering GitHub issue
# ---------------------------------------------------------------------------


def create_pr_for_issue(
    repo: str,
    issue_number: int,
    branch_name: str,
    title: str,
    body: str,
) -> Optional[str]:
    """Open a pull request on *repo* that closes *issue_number*.

    Invokes ``gh pr create`` with ``--base main``, ``--head <branch_name>``,
    and appends ``Closes #<issue_number>`` to *body* so GitHub automatically
    links and closes the issue on merge.

    Args:
        repo:         Repository slug (e.g. ``"owner/repo"``).
        issue_number: GitHub issue number the PR resolves.
        branch_name:  Source branch name for the PR head.
        title:        PR title string.
        body:         PR description body (Markdown).

    Returns:
        The PR HTML URL string on success.  ``None`` on any failure — errors
        are logged as warnings so callers can continue without a PR.

    Example::

        url = create_pr_for_issue(
            "owner/repo", 42, "feat/my-branch",
            "feat: implement new feature", "Summary of changes.",
        )
        if url:
            print(f"PR opened: {url}")
    """
    import subprocess  # noqa: PLC0415

    # Only append "Closes #N" if not already present to avoid duplication.
    _closes_marker = f"Closes #{issue_number}"
    if _closes_marker not in body:
        pr_body = f"{body}\n\n{_closes_marker}"
    else:
        pr_body = body
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repo,
                "--base",
                "main",
                "--head",
                branch_name,
                "--title",
                title,
                "--body",
                pr_body,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            pr_url = result.stdout.strip()
            return pr_url or None
        logger.warning(
            "create_pr_for_issue: gh pr create failed (rc=%d): %s",
            result.returncode,
            result.stderr.strip(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("create_pr_for_issue: error creating PR: %s", exc)

    return None


# ---------------------------------------------------------------------------
# create_content_pr — open a content/docs pull request (Issue #578)
# ---------------------------------------------------------------------------


def _truncate_title(text: str, limit: int = 80) -> str:
    """Truncate *text* to at most *limit* chars without splitting a word.

    If *text* is already within *limit*, it is returned unchanged (stripped).
    Otherwise truncate to *limit*, then drop back to the last whitespace so the
    final word is not cut mid-token; the result is right-stripped. If there is no
    whitespace within the window (a single very long token), fall back to a hard
    *limit*-char slice so the title is still bounded.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    cut = truncated.rfind(" ")
    if cut > 0:
        truncated = truncated[:cut]
    return truncated.rstrip()


def create_content_pr(
    repo: str,
    branch_name: str,
    topic: str,
    body: str,
    run_id: str,
    issue_number: Optional[int] = None,
    prefix: str = "content",
) -> Optional[str]:
    """Open a content pull request on *repo* for *branch_name*.

    Unlike :func:`create_pr_for_issue`, this function:

    - Does NOT require an issue number.
    - Uses a configurable title format ``{prefix}: {topic}`` (default
      ``content:``; pass ``prefix="docs"`` for docs pipelines).
    - Appends ``Closes #N`` to the body ONLY when *issue_number* is provided.

    Used for content-category and docs-category pipelines (Issue #578).

    Args:
        repo:         Repository slug (e.g. ``"owner/repo"``).
        branch_name:  Source branch name for the PR head.
        topic:        Content topic — used in the PR title (truncated word-safe
                      to 80 chars).
        body:         PR description body (Markdown).
        run_id:       Pipeline run ID appended as a footer for traceability.
        issue_number: Optional GitHub issue number. When set, ``Closes #N`` is
                      appended to the body (dedup-guarded) so GitHub links and
                      closes the issue on merge. Default ``None`` (no ``Closes``).
        prefix:       PR title prefix; the title is ``{prefix}: {topic}``.
                      Default ``"content"``.

    Returns:
        The PR HTML URL string on success, ``None`` on any failure — errors
        are logged as warnings so callers can continue without a PR.
    """
    import subprocess  # noqa: PLC0415

    topic_truncated = _truncate_title(topic.strip()) if topic else "content"
    title = f"{prefix}: {topic_truncated}"
    pr_body = f"{body}\n\n---\n*Run ID: `{run_id}`*"
    if issue_number is not None:
        _closes_marker = f"Closes #{issue_number}"
        if _closes_marker not in pr_body:
            pr_body = f"{pr_body}\n\n{_closes_marker}"

    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repo,
                "--base",
                "main",
                "--head",
                branch_name,
                "--title",
                title,
                "--body",
                pr_body,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
        logger.warning(
            "create_content_pr: gh pr create failed (rc=%d): %s",
            result.returncode,
            result.stderr.strip(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("create_content_pr: error creating content PR: %s", exc)

    return None


# ---------------------------------------------------------------------------
# post_pipeline_result_comment — post pipeline output to issue thread
# ---------------------------------------------------------------------------


def post_pipeline_result_comment(
    repo: str,
    issue_number: int,
    classification_type: str,
    result_text: str,
    run_id: str,
) -> Optional[str]:
    """Post the pipeline output as a comment on *issue_number*.

    Formats a Markdown comment containing the pipeline result text and run
    metadata, then delegates to :func:`post_github_comment`.

    Used for non-code pipelines (``content``, ``docs``, ``research``) where
    the output is delivered directly as an issue comment rather than a PR.

    Args:
        repo:                Repository slug (e.g. ``"owner/repo"``).
        issue_number:        GitHub issue number.
        classification_type: Classification type label (e.g. ``"content"``).
        result_text:         Main output text from the pipeline run.
        run_id:              Pipeline run ID for traceability.

    Returns:
        The comment HTML URL on success, or ``None`` on failure.

    Example::

        url = post_pipeline_result_comment(
            "owner/repo", 42, "research",
            "Here are the findings...", "abc-123",
        )
    """
    body = (
        f"## 🤖 Pipeline Result — `{classification_type}`\n\n"
        f"{result_text}\n\n"
        f"---\n"
        f"*Run ID: `{run_id}`*"
    )
    return _ia.post_github_comment(repo, issue_number, body)


# ---------------------------------------------------------------------------
# post_failure_summary_comment — post a human-readable failure summary
# ---------------------------------------------------------------------------


def post_failure_summary_comment(
    repo: str,
    issue_number: int,
    error_message: str,
    run_id: str,
    diagnosis: Optional[object] = None,
) -> Optional[str]:
    """Post a failure summary comment on *issue_number*.

    Formats a Markdown failure summary including the error message and, when
    available, diagnosis fields (``failure_class``, ``remediation``,
    ``confidence``).  Delegates to :func:`post_github_comment`.

    Args:
        repo:          Repository slug (e.g. ``"owner/repo"``).
        issue_number:  GitHub issue number.
        error_message: Human-readable error or abort message.
        run_id:        Pipeline run ID for traceability.
        diagnosis:     Optional diagnosis object/dict with ``failure_class``,
                       ``remediation``, and ``confidence`` attributes/keys.
                       ``None`` when no diagnosis was produced.

    Returns:
        The comment HTML URL on success, or ``None`` on failure.

    Example::

        url = post_failure_summary_comment(
            "owner/repo", 42, "Phase 'build' timed out", "abc-123",
        )
    """
    lines = [
        "## ❌ Pipeline Failed\n",
        f"**Error:** {error_message}\n",
    ]

    if diagnosis is not None:
        # Support both dict-like and object-like diagnosis results.
        def _get(obj: Any, key: str) -> Any:
            if isinstance(obj, dict):
                return obj.get(key)
            return getattr(obj, key, None)

        failure_class = _get(diagnosis, "failure_class")
        remediation = _get(diagnosis, "remediation")
        confidence = _get(diagnosis, "confidence")

        lines.append("\n### 🔍 Diagnosis\n")
        if failure_class:
            lines.append(f"- **Failure class:** `{failure_class}`")
        if remediation:
            lines.append(f"- **Remediation:** {remediation}")
        if confidence is not None:
            lines.append(
                f"- **Confidence:** {confidence:.0%}"
                if isinstance(confidence, float)
                else f"- **Confidence:** {confidence}"
            )

    lines.append(f"\n---\n*Run ID: `{run_id}`*")

    body = "\n".join(lines)
    return _ia.post_github_comment(repo, issue_number, body)


# ---------------------------------------------------------------------------
# post_result_to_issue — unified dispatch facade (Issue #5.1.4)
# ---------------------------------------------------------------------------

_RESULT_TEXT_MAX_CHARS = 65_000


def post_result_to_issue(
    repo: str,
    issue_number: int,
    run_id: str,
    final_status: str,
    classification_type: str,
    result_text: str,
    branch_name: Optional[str] = None,
    pr_title: Optional[str] = None,
    error_message: Optional[str] = None,
    diagnosis: Optional[object] = None,
) -> Optional[str]:
    """Unified entry point: post a pipeline result back to the triggering issue.

    Selects the correct posting path based on *final_status* and
    *classification_type*:

    - ``final_status == 'failed'`` → :func:`post_failure_summary_comment`
    - ``classification_type`` in ``{'bug', 'feature', 'refactor'}`` →
      :func:`create_pr_for_issue`
    - ``classification_type`` in ``{'content', 'docs', 'research'}`` →
      :func:`post_pipeline_result_comment` (result_text truncated to 65 000 chars)
    - Any other type → returns ``None``

    Args:
        repo:                Repository slug (e.g. ``"owner/repo"``).
        issue_number:        GitHub issue number.
        run_id:              Pipeline run ID for traceability.
        final_status:        Terminal status string (e.g. ``"success"``, ``"failed"``).
        classification_type: Classification type (e.g. ``"feature"``, ``"research"``).
        result_text:         Main pipeline output text; truncated to 65 000 chars for
                             content/docs/research paths.
        branch_name:         Branch name for PR creation (code pipelines).
        pr_title:            PR title (code pipelines).
        error_message:       Error message (failed pipelines).
        diagnosis:           Optional diagnosis object/dict (failed pipelines).

    Returns:
        URL string of the created PR or comment, or ``None`` on failure/no-op.

    Example::

        url = post_result_to_issue(
            repo="owner/repo",
            issue_number=42,
            run_id="abc-123",
            final_status="success",
            classification_type="research",
            result_text="Here are the findings...",
        )
    """
    if final_status == "failed":
        return post_failure_summary_comment(
            repo=repo,
            issue_number=issue_number,
            error_message=error_message or "Unknown error",
            run_id=run_id,
            diagnosis=diagnosis,
        )

    if classification_type in ("bug", "feature", "refactor"):
        return create_pr_for_issue(
            repo=repo,
            issue_number=issue_number,
            branch_name=branch_name or f"feat/issue-{issue_number}",
            title=pr_title or f"Pipeline result for #{issue_number}",
            body=result_text,
        )

    if classification_type in ("content", "docs", "research"):
        truncated = result_text[:_RESULT_TEXT_MAX_CHARS]
        return post_pipeline_result_comment(
            repo=repo,
            issue_number=issue_number,
            classification_type=classification_type,
            result_text=truncated,
            run_id=run_id,
        )

    return None
