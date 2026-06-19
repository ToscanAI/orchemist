"""TemplateSelector and InputExtractor — issue → pipeline input mapping.

:class:`TemplateSelector` maps a classification type to a pipeline template
name (using :data:`~.classifier.DEFAULT_TEMPLATE_MAPPING` by default).
:class:`InputExtractor` asks the LLM to extract pipeline input variables from
a GitHub issue, conforming to a template's ``config_schema``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

from .classifier import DEFAULT_TEMPLATE_MAPPING

logger = logging.getLogger(__name__)

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
            logger.debug("InputExtractor: no executor configured — returning stub response")
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
        except Exception as exc:  # noqa: BLE001
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
        match = re.search(r"\{[^{}]+\}", text, re.DOTALL)
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
