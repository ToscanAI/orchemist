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
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

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
]

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
    "bug":      "coding-pipeline-v1",
    "feature":  "coding-pipeline-v1",
    "refactor": "coding-pipeline-v1",
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
            classification_type, "coding-pipeline-v1"
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
