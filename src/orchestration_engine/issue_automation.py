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

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from orchestration_engine.notifications import NotificationDispatcher

if TYPE_CHECKING:
    from .db import Database

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
    "create_content_pr",
    # Re-exports from github_fetcher (Issue #507)
    "GitHubIssueData",
    "GitHubIssueFetcher",
    "fetch_github_issue",
]

# Re-exports from github_fetcher
from orchestration_engine.github_fetcher import (  # noqa: E402
    GitHubIssueData,
    GitHubIssueFetcher,
    fetch_github_issue,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The six valid classification types.
VALID_CLASSIFICATION_TYPES: frozenset = frozenset(
    {"bug", "feature", "docs", "refactor", "research", "content"}
)

#: Maps a classification type to its recommended pipeline template.
CLASSIFICATION_TEMPLATE_MAP: Dict[str, str] = {
    "bug":      "coding-pipeline-standard",
    "feature":  "coding-pipeline-standard",
    "refactor": "coding-pipeline-standard",
    "docs":     "content-pipeline-v27",
    "research": "research-competitive",
    "content":  "content-pipeline-v27",
}

#: Default mapping used by TemplateSelector.
#: Maps classification types to abstract pipeline template names.
#: These use version-agnostic names; callers resolve to concrete versioned templates.
DEFAULT_TEMPLATE_MAPPING: Dict[str, str] = {
    "bug":      "coding-pipeline",
    "feature":  "coding-pipeline",
    "refactor": "coding-pipeline",
    "docs":     "content-pipeline",
    "content":  "content-pipeline",
    "research": "research-pipeline",
}

# ---------------------------------------------------------------------------
# LLM prompt template (classification)
# ---------------------------------------------------------------------------

_CLASSIFY_PROMPT_TEMPLATE = """\
You are an expert software project manager. Classify the following GitHub issue \
into exactly one category.

## Categories
- bug: defect, error, crash, unexpected behaviour, wrong output
- feature: new functionality, enhancement, new capability
- docs: documentation-only change (README, docstring, wiki, etc.)
- refactor: code quality, cleanup, restructuring — no behaviour change
- research: investigation, spike, feasibility study, benchmarking
- content: blog post, article, marketing copy, non-code writing

## Issue
Title: {title}
Labels: {labels}
Body:
{body}

## Instructions
Respond with a single JSON object on ONE line. No markdown, no code fences.
Required fields:
  - "classification_type": one of bug/feature/docs/refactor/research/content
  - "confidence": float in [0.0, 1.0] representing your certainty
  - "reasoning": one-sentence justification (max 120 chars)

Example:
{{"classification_type": "bug", "confidence": 0.92, \
"reasoning": "Issue describes a crash when input is null."}}
"""


# ---------------------------------------------------------------------------
# IssueClassification dataclass
# ---------------------------------------------------------------------------


@dataclass
class IssueClassification:
    """Structured output of a single issue classification.

    Mirrors the ``issue_pipeline_map`` DB table and carries the full
    result of one :meth:`~IssueClassifier.classify` call.

    Attributes:
        issue_number:        GitHub issue number.
        repo:                Repository slug (e.g. ``"owner/repo"``).
        classification_type: One of ``bug``, ``feature``, ``docs``,
                             ``refactor``, ``research``, ``content``.
        confidence:          LLM confidence in ``[0.0, 1.0]``.
        template_id:         Recommended pipeline template for this issue.
        reasoning:           One-sentence LLM justification (≤ 200 chars).
        status:              Lifecycle status — ``"classified"`` on creation.
                             Downstream automation may update this to e.g.
                             ``"launched"``, ``"skipped"``.
        id:                  DB primary key.  ``None`` until the row has been
                             persisted.
        run_id:              Optional pipeline ``run_id`` linked after launch.
        created_at:          UTC ISO-8601 timestamp of creation.
    """

    issue_number: int
    repo: str
    classification_type: str
    confidence: float
    template_id: str
    reasoning: str
    status: str = "classified"
    id: Optional[int] = None
    run_id: Optional[str] = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain-dict representation suitable for DB insertion.

        Returns:
            Dict with all fields.  ``id`` is included and may be ``None``
            for unsaved instances.
        """
        return {
            "id":                  self.id,
            "issue_number":        self.issue_number,
            "repo":                self.repo,
            "classification_type": self.classification_type,
            "confidence":          self.confidence,
            "template_id":         self.template_id,
            "run_id":              self.run_id,
            "status":              self.status,
            "created_at":          self.created_at,
        }


# ---------------------------------------------------------------------------
# IssueClassifier
# ---------------------------------------------------------------------------


class IssueClassifier:
    """LLM-based GitHub issue classifier.

    Builds a prompt from issue metadata, calls the configured executor
    (intended to be Haiku for low latency and cost), parses the JSON
    response, and persists the result to the ``issue_pipeline_map`` DB table.

    Args:
        executor: An object with an ``execute(prompt: str) -> str`` (or an
                  object returning a ``.text`` attribute) interface.
                  When ``None``, the classifier operates in *stub mode*:
                  it returns a deterministic ``"feature"`` classification
                  with ``confidence=0.0`` (useful for offline tests and
                  dry runs).
        model:    Human-readable model label (informational, stored in logs).
                  Defaults to ``"haiku"``.

    Example::

        from unittest.mock import MagicMock

        mock_exec = MagicMock()
        mock_exec.execute.return_value = (
            '{"classification_type": "bug", "confidence": 0.95, '
            '"reasoning": "Describes a null pointer crash."}'
        )
        classifier = IssueClassifier(executor=mock_exec)
        result = classifier.classify(
            issue_number=42,
            repo="owner/repo",
            title="NPE in pipeline runner",
            body="When the task list is empty the runner crashes.",
        )
        assert result.classification_type == "bug"
    """

    #: Classification type returned in stub mode (no executor).
    _STUB_CLASSIFICATION: str = "feature"
    #: Confidence returned in stub mode (no executor).
    _STUB_CONFIDENCE: float = 0.0

    def __init__(
        self,
        executor: Optional[Any] = None,
        model: str = "haiku",
    ) -> None:
        self._executor = executor
        self.model = model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(
        self,
        issue_number: int,
        repo: str,
        title: str,
        body: str = "",
        labels: Optional[List[str]] = None,
        db: Optional["Database"] = None,
    ) -> IssueClassification:
        """Classify a GitHub issue and optionally persist the result.

        Steps:
            1. Build the classification prompt from issue metadata.
            2. Call the LLM executor (or return stub if no executor).
            3. Parse the JSON response into ``(type, confidence, reasoning)``.
            4. Map the classification type to its pipeline template.
            5. Persist to DB via
               :meth:`~db.Database.insert_issue_classification` if *db*
               is provided.
            6. Return the populated :class:`IssueClassification`.

        Args:
            issue_number: GitHub issue number (positive integer).
            repo:         Repository slug (e.g. ``"owner/repo"``).
            title:        Issue title string.
            body:         Issue body / description.  Truncated to 3 000
                          characters before being passed to the LLM.
            labels:       List of GitHub label strings attached to the issue.
                          Defaults to an empty list when not provided.
            db:           :class:`~db.Database` instance for persistence.
                          When ``None`` the classification is returned but
                          not stored in the database.

        Returns:
            :class:`IssueClassification` instance.  If *db* was provided
            the :attr:`~IssueClassification.id` field will be set to the
            newly created database primary key.
        """
        labels = labels or []
        prompt = self._build_prompt(title=title, body=body, labels=labels)
        raw_output = self._call_executor(prompt)
        classification_type, confidence, reasoning = self._parse_output(raw_output)
        template_id = CLASSIFICATION_TEMPLATE_MAP.get(
            classification_type,
            os.environ.get("ORCH_DEFAULT_TEMPLATE") or "coding-pipeline-standard",
        )

        result = IssueClassification(
            issue_number=issue_number,
            repo=repo,
            classification_type=classification_type,
            confidence=confidence,
            template_id=template_id,
            reasoning=reasoning,
        )

        if db is not None:
            row_id = db.insert_issue_classification(result.to_dict())
            result.id = row_id
            logger.debug(
                "IssueClassifier: issue #%d in %r classified as %r "
                "(confidence=%.2f, template=%r, db_id=%d)",
                issue_number,
                repo,
                classification_type,
                confidence,
                template_id,
                row_id,
            )
        else:
            logger.debug(
                "IssueClassifier: issue #%d in %r classified as %r "
                "(confidence=%.2f, template=%r, no db)",
                issue_number,
                repo,
                classification_type,
                confidence,
                template_id,
            )

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        title: str,
        body: str,
        labels: List[str],
    ) -> str:
        """Build the LLM classification prompt.

        Args:
            title:  Issue title.
            body:   Issue body (truncated to 3 000 characters).
            labels: List of label strings.

        Returns:
            Formatted prompt string ready for the executor.
        """
        body_truncated = body[:3000] if body else "(no body)"
        labels_str = ", ".join(labels) if labels else "(none)"
        return _CLASSIFY_PROMPT_TEMPLATE.format(
            title=title,
            body=body_truncated,
            labels=labels_str,
        )

    def _call_executor(self, prompt: str) -> str:
        """Send *prompt* to the executor and return the raw string response.

        Falls back to a stub JSON response when no executor is configured
        or when the executor raises an exception.

        Args:
            prompt: The formatted classification prompt.

        Returns:
            Raw string output from the LLM (or stub JSON on error).
        """
        if self._executor is None:
            logger.debug(
                "IssueClassifier: no executor configured — returning stub response"
            )
            return json.dumps({
                "classification_type": self._STUB_CLASSIFICATION,
                "confidence":          self._STUB_CONFIDENCE,
                "reasoning":           "Stub mode — no executor configured.",
            })

        try:
            result = self._executor.execute(prompt)
            # Accept both plain strings and objects with a .text attribute
            # (e.g. OpenClawExecutor returns ExecutorResult with .text)
            if isinstance(result, str):
                return result
            if hasattr(result, "text") and result.text is not None:
                return result.text
            return str(result)
        except Exception as exc:
            logger.warning(
                "IssueClassifier: executor.execute() raised %s — falling back to stub",
                exc,
            )
            return json.dumps({
                "classification_type": self._STUB_CLASSIFICATION,
                "confidence":          self._STUB_CONFIDENCE,
                "reasoning":           f"Executor error: {type(exc).__name__}",
            })

    def _parse_output(self, raw: str) -> Tuple[str, float, str]:
        """Parse LLM output and extract ``(classification_type, confidence, reasoning)``.

        Tries a direct JSON parse first; if that fails it searches for
        the first ``{...}`` block in the output (to gracefully handle
        models that prepend prose before the JSON object).

        Args:
            raw: Raw string output from the LLM.

        Returns:
            Tuple of ``(classification_type, confidence, reasoning)``.
            Falls back to ``("feature", 0.0, "Parse error")`` on failure.
        """
        text = raw.strip()
        parsed: Optional[Dict[str, Any]] = None

        # 1. Try direct JSON parse
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # 2. Search for first {…} object in the output
            match = re.search(r'\{[^{}]+\}', text, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group())
                except json.JSONDecodeError:
                    pass

        if parsed is None:
            logger.warning(
                "IssueClassifier: could not parse LLM output as JSON: %r",
                text[:200],
            )
            return (self._STUB_CLASSIFICATION, 0.0, "Parse error")

        # Validate and normalise classification_type
        cls_type = str(parsed.get("classification_type", "feature")).lower().strip()
        if cls_type not in VALID_CLASSIFICATION_TYPES:
            logger.warning(
                "IssueClassifier: unknown classification_type %r — "
                "defaulting to 'feature'",
                cls_type,
            )
            cls_type = "feature"

        # Validate and clamp confidence
        try:
            confidence = float(parsed.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.0

        reasoning = str(parsed.get("reasoning", "")).strip()[:200]

        return (cls_type, confidence, reasoning)


# ---------------------------------------------------------------------------
# LLM prompt template (input extraction)
# ---------------------------------------------------------------------------

_EXTRACT_PROMPT_TEMPLATE = """\
You are a pipeline configuration assistant. Given a GitHub issue and a pipeline \
config schema, extract the values needed to launch the pipeline.

## Pipeline Config Schema
{schema}

## GitHub Issue
Title: {title}

Body:
{body}

## Instructions
Return a single JSON object whose keys match the schema fields above.
Fill in values based on the issue content. Use sensible defaults when the \
issue does not mention a value. Do NOT include keys not in the schema.
Respond with only the JSON object on ONE line. No markdown, no code fences.

Example (for a schema with fields "issue_number" and "repo"):
{{"issue_number": 42, "repo": "owner/repo"}}
"""


# ---------------------------------------------------------------------------
# TemplateSelector
# ---------------------------------------------------------------------------


class TemplateSelector:
    """Configurable mapping from classification type to pipeline template name.

    Supports dependency injection of a custom mapping for testability and
    runtime overriding.  When no mapping is supplied,
    :data:`DEFAULT_TEMPLATE_MAPPING` is used.

    Args:
        mapping:  Dict mapping classification_type strings to template
                  identifiers.  When ``None``, :data:`DEFAULT_TEMPLATE_MAPPING`
                  is used.
        fallback: Template identifier returned for unknown classification
                  types.  Defaults to ``"coding-pipeline"``.

    Example::

        selector = TemplateSelector()
        template = selector.select("bug")   # → "coding-pipeline"
        template = selector.select("docs")  # → "content-pipeline"

        custom = TemplateSelector({"bug": "my-bug-pipeline"})
        template = custom.select("bug")     # → "my-bug-pipeline"
    """

    def __init__(
        self,
        mapping: Optional[Dict[str, str]] = None,
        fallback: str = "coding-pipeline",
    ) -> None:
        self._mapping: Dict[str, str] = dict(
            mapping if mapping is not None else DEFAULT_TEMPLATE_MAPPING
        )
        self._fallback = fallback

    def select(self, classification_type: str) -> str:
        """Return the pipeline template name for *classification_type*.

        Args:
            classification_type: A classification type string
                                 (e.g. ``"bug"``, ``"feature"``).

        Returns:
            The mapped template identifier, or the fallback template if
            the type is not in the mapping.
        """
        return self._mapping.get(classification_type, self._fallback)


# ---------------------------------------------------------------------------
# InputExtractor
# ---------------------------------------------------------------------------


class InputExtractor:
    """LLM-assisted extractor that maps a GitHub issue to a pipeline input dict.

    Takes an issue title, issue body, and a template's ``config_schema``
    (as parsed from the YAML ``config_schema:`` block) and asks the LLM
    to produce a JSON dict whose keys match the schema fields.  The result
    can be passed directly as the ``--input-file`` JSON when launching a
    pipeline run.

    Args:
        executor: An object with an ``execute(prompt: str) -> str`` interface
                  (or an object with a ``.text`` attribute on the return value).
                  When ``None``, the extractor operates in *stub mode*:
                  it returns an empty dict (useful for offline tests and
                  dry runs).
        model:    Human-readable model label (informational, stored in logs).
                  Defaults to ``"haiku"``.

    Example::

        from unittest.mock import MagicMock

        schema = {"issue_number": "int", "repo": "str"}
        mock_exec = MagicMock()
        mock_exec.execute.return_value = '{"issue_number": 42, "repo": "owner/repo"}'

        extractor = InputExtractor(executor=mock_exec)
        result = extractor.extract(
            issue_title="Fix crash on empty input",
            issue_body="When the list is empty the runner crashes.",
            config_schema=schema,
        )
        # result == {"issue_number": 42, "repo": "owner/repo"}
    """

    _STUB_RESULT: Dict[str, Any] = {}

    def __init__(
        self,
        executor: Optional[Any] = None,
        model: str = "haiku",
    ) -> None:
        self._executor = executor
        self.model = model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(
        self,
        issue_title: str,
        issue_body: str,
        config_schema: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Extract pipeline input values from a GitHub issue.

        Steps:
            1. Build the extraction prompt from issue metadata and schema.
            2. Call the LLM executor (or return stub if no executor).
            3. Parse the JSON response into a dict.

        Args:
            issue_title:   Issue title string.
            issue_body:    Issue body / description.  Truncated to 3 000
                           characters before being sent to the LLM.
            config_schema: Dict describing the expected input fields and
                           their types (as parsed from the template YAML).

        Returns:
            Dict whose keys/values conform to *config_schema*.  Returns an
            empty dict in stub mode or on parse failure.
        """
        prompt = self._build_prompt(
            title=issue_title,
            body=issue_body,
            schema=config_schema,
        )
        raw_output = self._call_executor(prompt)
        return self._parse_output(raw_output)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        title: str,
        body: str,
        schema: Dict[str, Any],
    ) -> str:
        """Build the LLM input-extraction prompt.

        Args:
            title:  Issue title.
            body:   Issue body (truncated to 3 000 characters).
            schema: Config schema dict.

        Returns:
            Formatted prompt string ready for the executor.
        """
        body_truncated = body[:3000] if body else "(no body)"
        schema_str = json.dumps(schema, indent=2)
        return _EXTRACT_PROMPT_TEMPLATE.format(
            title=title,
            body=body_truncated,
            schema=schema_str,
        )

    def _call_executor(self, prompt: str) -> str:
        """Send *prompt* to the executor and return the raw string response.

        Falls back to a stub JSON response when no executor is configured
        or when the executor raises an exception.

        Args:
            prompt: The formatted extraction prompt.

        Returns:
            Raw string output from the LLM (or stub JSON on error).
        """
        if self._executor is None:
            logger.debug(
                "InputExtractor: no executor configured — returning stub response"
            )
            return json.dumps(self._STUB_RESULT)

        try:
            result = self._executor.execute(prompt)
            # Accept both plain strings and objects with a .text attribute
            # (e.g. OpenClawExecutor returns ExecutorResult with .text)
            if isinstance(result, str):
                return result
            if hasattr(result, "text") and result.text is not None:
                return result.text
            return str(result)
        except Exception as exc:
            logger.warning(
                "InputExtractor: executor.execute() raised %s — falling back to stub",
                exc,
            )
            return json.dumps(self._STUB_RESULT)

    def _parse_output(self, raw: str) -> Dict[str, Any]:
        """Parse LLM output and return the extracted input dict.

        Tries a direct JSON parse first; if that fails it searches for
        the first ``{...}`` block in the output (to gracefully handle
        models that prepend prose before the JSON object).

        Args:
            raw: Raw string output from the LLM.

        Returns:
            Parsed dict, or an empty dict on failure.
        """
        text = raw.strip()

        # 1. Try direct JSON parse
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        # 2. Search for first {…} object in the output
        match = re.search(r'\{[^{}]+\}', text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        logger.warning(
            "InputExtractor: could not parse LLM output as JSON dict: %r",
            text[:200],
        )
        return {}


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
    import subprocess

    try:
        result = subprocess.run(
            [
                "gh", "api",
                f"repos/{repo}/issues/{issue_number}/comments",
                "--method", "POST",
                "--field", f"body={body}",
                "--jq", ".html_url",
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
# slugify_branch — convert a title to a git-branch-safe slug (Issue #511)
# ---------------------------------------------------------------------------


from .text_utils import slugify_branch  # noqa: F401 — re-export for backward compat

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
    import subprocess
    from urllib.parse import quote

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
    import subprocess

    endpoint = f"repos/{repo}/issues/{issue_number}/labels"
    try:
        result = subprocess.run(
            ["gh", "api", endpoint, "--method", "POST",
             "--field", f"labels[]={label}"],
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
    import subprocess

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
    import subprocess

    # Only append "Closes #N" if not already present to avoid duplication.
    _closes_marker = f"Closes #{issue_number}"
    if _closes_marker not in body:
        pr_body = f"{body}\n\n{_closes_marker}"
    else:
        pr_body = body
    try:
        result = subprocess.run(
            [
                "gh", "pr", "create",
                "--repo", repo,
                "--base", "main",
                "--head", branch_name,
                "--title", title,
                "--body", pr_body,
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
    cut = truncated.rfind(' ')
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
    import subprocess

    topic_truncated = _truncate_title(topic.strip()) if topic else 'content'
    title = f"{prefix}: {topic_truncated}"
    pr_body = f"{body}\n\n---\n*Run ID: `{run_id}`*"
    if issue_number is not None:
        _closes_marker = f"Closes #{issue_number}"
        if _closes_marker not in pr_body:
            pr_body = f"{pr_body}\n\n{_closes_marker}"

    try:
        result = subprocess.run(
            [
                "gh", "pr", "create",
                "--repo", repo,
                "--base", "main",
                "--head", branch_name,
                "--title", title,
                "--body", pr_body,
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
            lines.append(f"- **Confidence:** {confidence:.0%}" if isinstance(confidence, float) else f"- **Confidence:** {confidence}")

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
    if final_status == 'failed':
        return post_failure_summary_comment(
            repo=repo,
            issue_number=issue_number,
            error_message=error_message or 'Unknown error',
            run_id=run_id,
            diagnosis=diagnosis,
        )

    if classification_type in ('bug', 'feature', 'refactor'):
        return create_pr_for_issue(
            repo=repo,
            issue_number=issue_number,
            branch_name=branch_name or f"feat/issue-{issue_number}",
            title=pr_title or f"Pipeline result for #{issue_number}",
            body=result_text,
        )

    if classification_type in ('content', 'docs', 'research'):
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
            escalated = True

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
            except Exception as exc:
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
            except Exception as exc:
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
        issue_number: int,
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
            lines.append(
                "This issue has been escalated to human review via Telegram."
            )
        else:
            lines.append(f"**Pipeline:** `{template_name}`")
            if run_id:
                lines.append(f"**Run ID:** `{run_id}`")
            else:
                lines.append("**Run ID:** *(not launched — template unavailable or no launcher)*")
        if classification.reasoning:
            lines.append(f"**Reasoning:** {classification.reasoning}")
        return "\n".join(lines)
