"""IssueClassifier — LLM-based GitHub issue classification (Issue #5.1.1).

Classifies a GitHub issue into one of six categories using Claude Haiku
(fast, cheap) and persists the result to the ``issue_pipeline_map`` DB table.

Categories:
    ``bug``       — defects, errors, unexpected behaviour.
    ``feature``   — new functionality, enhancements.
    ``docs``      — documentation-only changes (README, docstrings, wiki).
    ``refactor``  — code quality / structure improvements, no behaviour change.
    ``research``  — investigation, spike, or feasibility study.
    ``content``   — blog posts, articles, marketing copy, non-code writing.

The classification result is available as an :class:`IssueClassification`
dataclass.  The :class:`IssueClassifier` class exposes a single
:meth:`~IssueClassifier.classify` method that builds the prompt, calls the
LLM, parses the JSON response, and persists the result to the DB.

Typical usage::

    from orchestration_engine.issue_automation import IssueClassifier

    classifier = IssueClassifier(executor=my_executor)
    result = classifier.classify(
        issue_number=42,
        repo="owner/repo",
        title="Fix null pointer in pipeline runner",
        body="When the pipeline runner receives an empty task list...",
        labels=["bug", "urgent"],
        db=db,
    )
    print(result.classification_type, result.confidence)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..notifications import NotificationDispatcher

if TYPE_CHECKING:
    from ..db import Database

__all__ = [
    "IssueClassification",
    "IssueClassifier",
    "VALID_CLASSIFICATION_TYPES",
    "CLASSIFICATION_TEMPLATE_MAP",
    "DEFAULT_TEMPLATE_MAPPING",
    "TemplateSelector",
    "InputExtractor",
    "IssueAutomation",
    "post_github_comment",
    "slugify_branch",
    "generate_pipeline_input",
    "remove_github_label",
    "add_github_label",
    "get_github_issue_labels",
    "create_pr_for_issue",
    "create_content_pr",
    # Re-exports from github_fetcher (Issue #507)
    "GitHubIssueData",
    "GitHubIssueFetcher",
    "fetch_github_issue",
]

# Re-exports from github_fetcher
from ..github_fetcher import (  # noqa: E402
    GitHubIssueData,
    GitHubIssueFetcher,
    fetch_github_issue,
)

# slugify_branch — re-export for backward compat (Issue #511)
from ..text_utils import slugify_branch  # noqa: E402, F401

# Re-imports from extracted sub-modules — these resolve the bare names used by
# the still-inline pr_dispatch group and ``IssueAutomation`` below, AND form the
# facade re-export surface.  (Wave 2 extracts pr_dispatch + IssueAutomation.)
from .classifier import (  # noqa: E402, F401
    CLASSIFICATION_TEMPLATE_MAP,
    DEFAULT_TEMPLATE_MAPPING,
    VALID_CLASSIFICATION_TYPES,
    IssueClassification,
    IssueClassifier,
)
from .extractor import InputExtractor, TemplateSelector  # noqa: E402, F401
from .github_labels import (  # noqa: E402, F401
    add_github_label,
    generate_pipeline_input,
    get_github_issue_labels,
    post_github_comment,
    remove_github_label,
)

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
    return post_github_comment(repo, issue_number, body)


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
    return post_github_comment(repo, issue_number, body)


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


# ---------------------------------------------------------------------------
# IssueAutomation — orchestrates classify → select → extract → launch
# ---------------------------------------------------------------------------


class IssueAutomation:
    """Orchestrates the full GitHub issue → pipeline run flow.

    Chains four steps:

    1. **Classify** — :class:`IssueClassifier` assigns a type (bug/feature/…)
       and persists the result to the DB.
    2. **Select template** — :class:`TemplateSelector` maps the classification
       type to a pipeline template name.
    3. **Extract inputs** — :class:`InputExtractor` reads the template's
       ``config_schema`` and populates pipeline input variables from the issue.
    4. **Launch run** — the caller-supplied *launcher* callable spawns a
       daemon subprocess and returns the pipeline run dict.

    Dependencies are injected at construction time to facilitate testing.

    Args:
        classifier: :class:`IssueClassifier` instance.
        selector:   :class:`TemplateSelector` instance.
        extractor:  :class:`InputExtractor` instance.

    Example::

        automation = IssueAutomation(
            classifier=IssueClassifier(),
            selector=TemplateSelector(),
            extractor=InputExtractor(),
        )
        result = automation.process(
            issue_number=42,
            repo="owner/repo",
            title="Fix crash on empty input",
            body="When the list is empty the runner crashes.",
            labels=["bug"],
        )
        print(result["run_id"], result["comment_body"])
    """

    def __init__(
        self,
        classifier: "IssueClassifier",
        selector: "TemplateSelector",
        extractor: "InputExtractor",
        confidence_threshold: float = 0.70,
        notification_dispatcher: Optional[NotificationDispatcher] = None,
    ) -> None:
        self.classifier = classifier
        self.selector = selector
        self.extractor = extractor
        self._confidence_threshold = confidence_threshold
        self._dispatcher = notification_dispatcher

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(
        self,
        issue_number: int,
        repo: str,
        title: str,
        body: str = "",
        labels: Optional[List[str]] = None,
        db: Optional["Database"] = None,
        launcher: Optional[Any] = None,
        template_resolver: Optional[Any] = None,
        template_engine: Optional[Any] = None,
        mode: str = "standalone",
        gateway_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Classify an issue, select a template, extract inputs, and launch a pipeline.

        Steps:

        1. Classify the issue via :meth:`~IssueClassifier.classify` (persists to
           DB when *db* is provided).
        2. Select the pipeline template name via :meth:`~TemplateSelector.select`.
        3. Load the template's ``config_schema`` (via *template_resolver* /
           *template_engine*) and extract pipeline inputs via
           :meth:`~InputExtractor.extract`.  Falls back to empty schema on
           failure.
        4. Launch the pipeline via *launcher* when provided.  Updates the
           classification row status to ``"launched"`` in the DB on success.

        Args:
            issue_number:      GitHub issue number.
            repo:              Repository slug (e.g. ``"owner/repo"``).
            title:             Issue title.
            body:              Issue body / description.  Defaults to ``""``.
            labels:            List of GitHub label strings.  Defaults to ``[]``.
            db:                :class:`~db.Database` instance for persistence.
                               When ``None``, classification is not persisted
                               and the status is not updated.
            launcher:          Callable matching the ``_launch_pipeline_from_trigger``
                               signature: ``(template_file, template, input_data,
                               mode, gateway_url, db) → run_dict``.
                               When ``None``, no pipeline is launched.
            template_resolver: Callable that resolves a template name to a
                               :class:`~pathlib.Path`.  Used to load the template.
                               When ``None``, the config schema defaults to ``{}``.
            template_engine:   Template engine instance with a ``load_template``
                               method.  Used together with *template_resolver*.
            mode:              Daemon execution mode (``"standalone"``, ``"openclaw"``,
                               ``"dry-run"``).  Defaults to ``"standalone"``.
            gateway_url:       OpenClaw gateway URL.  ``None`` to use env var.

        Returns:
            Dict with keys::

                issue_number       int   — original issue number
                repo               str   — repository slug
                classification_type str  — LLM-assigned type
                confidence         float — LLM confidence [0, 1]
                template           str   — selected pipeline template name
                run_id             str   — launched run ID, or None
                comment_body       str   — pre-formatted GitHub comment text
        """
        labels = labels or []

        # Step 1: Classify
        classification = self.classifier.classify(
            issue_number=issue_number,
            repo=repo,
            title=title,
            body=body,
            labels=labels,
            db=db,
        )

        # Confidence threshold gate — escalate to human review when confidence
        # is too low to trust automatic pipeline selection.
        run_id: Optional[str] = None
        escalated: bool = False

        if classification.confidence < self._confidence_threshold:
            logger.warning(
                "IssueAutomation: confidence %.2f below threshold %.2f for issue #%d "
                "in %r — escalating to human review",
                classification.confidence,
                self._confidence_threshold,
                issue_number,
                repo,
            )
            if self._dispatcher is not None:
                self._dispatcher.dispatch(
                    event="human_review",
                    run_id=f"issue-{issue_number}",
                    issue_number=issue_number,
                    confidence=f"{classification.confidence:.0%}",
                    summary=(
                        f"Low-confidence classification: "
                        f"{classification.classification_type} "
                        f"({classification.confidence:.0%}). Manual review needed."
                    ),
                    tier="escalation",
                )
            escalated = True  # noqa: F841 — escalation outcome flag, kept intentional

            # Update DB status to 'escalated' so the row reflects the outcome.
            if db is not None and classification.id is not None:
                db.update_issue_classification_status(classification.id, "escalated")

            template_name = self.selector.select(classification.classification_type)

            comment_body = self._build_comment(
                issue_number=issue_number,
                classification=classification,
                template_name=template_name,
                run_id=None,
                escalated=True,
            )

            return {
                "issue_number": issue_number,
                "repo": repo,
                "classification_type": classification.classification_type,
                "confidence": classification.confidence,
                "template": template_name,
                "run_id": None,
                "comment_body": comment_body,
                "escalated": True,
            }

        # Step 2: Select template
        template_name = self.selector.select(classification.classification_type)

        # Step 3: Load config_schema and extract pipeline inputs
        config_schema: Dict[str, Any] = {}
        template_path_obj = None
        template_obj = None

        if template_resolver is not None and template_engine is not None:
            try:
                template_path_obj = template_resolver(template_name)
                template_obj = template_engine.load_template(template_path_obj)
                config_schema = template_obj.config_schema or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "IssueAutomation: could not load template schema for %r: %s",
                    template_name,
                    exc,
                )

        pipeline_inputs = self.extractor.extract(
            issue_title=title,
            issue_body=body,
            config_schema=config_schema,
        )
        # Always ensure issue context is in inputs as a fallback
        pipeline_inputs.setdefault("issue_number", issue_number)
        pipeline_inputs.setdefault("repo", repo)

        # Step 4: Launch pipeline
        if launcher is not None and template_path_obj is not None and template_obj is not None:
            try:
                run_dict = launcher(
                    template_file=template_path_obj,
                    template=template_obj,
                    input_data=pipeline_inputs,
                    mode=mode,
                    gateway_url=gateway_url,
                    db=db,
                )
                run_id = run_dict.get("run_id")
                # Update the classification row to reflect the pipeline was launched
                if db is not None and classification.id is not None:
                    db.update_issue_classification_status(classification.id, "launched")
                logger.info(
                    "IssueAutomation: launched pipeline %r for issue #%d in %r (run_id=%r)",
                    template_name,
                    issue_number,
                    repo,
                    run_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "IssueAutomation: failed to launch pipeline for issue #%d: %s",
                    issue_number,
                    exc,
                )

        comment_body = self._build_comment(
            issue_number=issue_number,
            classification=classification,
            template_name=template_name,
            run_id=run_id,
            escalated=False,
        )

        return {
            "issue_number": issue_number,
            "repo": repo,
            "classification_type": classification.classification_type,
            "confidence": classification.confidence,
            "template": template_name,
            "run_id": run_id,
            "comment_body": comment_body,
            "escalated": False,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_comment(
        self,
        issue_number: int,  # noqa: ARG002
        classification: "IssueClassification",
        template_name: str,
        run_id: Optional[str],
        escalated: bool = False,
    ) -> str:
        """Build the GitHub comment body for an issue automation result.

        Args:
            issue_number:    GitHub issue number (informational).
            classification:  The populated :class:`IssueClassification` instance.
            template_name:   Selected pipeline template name.
            run_id:          Pipeline run ID, or ``None`` when no run was launched.
            escalated:       When ``True``, the comment reflects that the issue
                             was escalated to human review due to low confidence.

        Returns:
            Markdown-formatted comment text suitable for posting as a GitHub issue comment.
        """
        lines: List[str] = [
            "🤖 **Orchemist** has picked up this issue.",
            "",
            f"**Classification:** `{classification.classification_type}` "
            f"(confidence: {classification.confidence:.0%})",
        ]
        if escalated:
            lines.append(
                f"⚠️ **Confidence too low for automatic launch** "
                f"(`{classification.confidence:.0%}` < threshold)"
            )
            lines.append("This issue has been escalated to human review via Telegram.")
        else:
            lines.append(f"**Pipeline:** `{template_name}`")
            if run_id:
                lines.append(f"**Run ID:** `{run_id}`")
            else:
                lines.append("**Run ID:** *(not launched — template unavailable or no launcher)*")
        if classification.reasoning:
            lines.append(f"**Reasoning:** {classification.reasoning}")
        return "\n".join(lines)
