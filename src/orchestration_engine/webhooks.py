"""Webhook trigger configuration schema and validation.

Defines TriggerConfig — the canonical in-memory representation of a
webhook trigger that maps an incoming HTTP request to a pipeline run.

Also provides:
  - TriggerMatcher: evaluates filter conditions against an incoming payload
  - InputMapper: resolves ``{{payload.x.y}}`` template strings from a payload

This module is intentionally HTTP-agnostic: it contains only data
structures and validation, not request handling.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)


# Valid mode values (exhaustive)
VALID_MODES = frozenset({"sync", "async", "fire_and_forget"})

# Trigger ID format: alphanumeric + hyphens/underscores, 3-64 chars,
# must start and end with alphanumeric character.
_ID_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{1,62}[a-zA-Z0-9]$')


class TriggerValidationError(ValueError):
    """Raised when TriggerConfig fails validation."""


@dataclass
class TriggerConfig:
    """Configuration for a webhook trigger.

    Attributes:
        id: Unique trigger identifier. Must match
            ``[a-zA-Z0-9][a-zA-Z0-9_-]{1,62}[a-zA-Z0-9]``.
        template_id: ID of the pipeline template to run when triggered.
            Caller must validate existence against the DB/template registry.
        mode: Execution mode — one of ``'sync'``, ``'async'``,
            ``'fire_and_forget'``.
        secret: Optional shared HMAC secret for request verification.
            Stored as plain string; hashing is future scope.
        rate_limit: Maximum requests per minute (0 = unlimited). Must be >= 0.
        input_map: Dict mapping webhook payload fields to pipeline input vars.
        filters: List of filter dicts (e.g.
            ``[{"field": "event", "eq": "push"}]``).
        created_at: ISO-format timestamp (set automatically on creation via
            ``to_dict()`` if not provided).
    """

    id: str
    template_id: str
    mode: str = "async"
    secret: Optional[str] = None
    rate_limit: int = 0
    input_map: Dict[str, Any] = field(default_factory=dict)
    filters: List[Dict[str, Any]] = field(default_factory=list)
    created_at: Optional[str] = None
    enabled: bool = True

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Validate all fields. Raises TriggerValidationError on failure."""
        # id format: 3-64 chars, alphanumeric/hyphens/underscores,
        # must start and end with alphanumeric character.
        if not self.id or not _ID_RE.match(self.id):
            raise TriggerValidationError(
                f"Invalid trigger id {self.id!r}. Must be 3-64 chars, "
                "alphanumeric/hyphens/underscores, start and end with alphanumeric."
            )
        # template_id must be present and non-blank
        if not self.template_id or not self.template_id.strip():
            raise TriggerValidationError("template_id must not be empty.")
        # mode must be one of the accepted values
        if self.mode not in VALID_MODES:
            raise TriggerValidationError(
                f"Invalid mode {self.mode!r}. Must be one of: {sorted(VALID_MODES)}"
            )
        # rate_limit must be a non-negative integer
        if not isinstance(self.rate_limit, int) or self.rate_limit < 0:
            raise TriggerValidationError(
                f"rate_limit must be a non-negative integer, got {self.rate_limit!r}."
            )
        # input_map must be a dict
        if not isinstance(self.input_map, dict):
            raise TriggerValidationError("input_map must be a dict.")
        # filters must be a list
        if not isinstance(self.filters, list):
            raise TriggerValidationError("filters must be a list.")
        # enabled must be a bool
        if not isinstance(self.enabled, bool):
            raise TriggerValidationError("enabled must be a bool.")

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict suitable for DB insertion.

        Note:
            ``input_map`` and ``filters`` are returned as Python objects
            (dict and list respectively). The DB layer is responsible for
            JSON-serialising them before storage.
        """
        return {
            "id": self.id,
            "template_id": self.template_id,
            "mode": self.mode,
            "secret": self.secret,
            "rate_limit": self.rate_limit,
            "input_map": self.input_map,   # JSON-serialised by DB layer
            "filters": self.filters,       # JSON-serialised by DB layer
            "created_at": self.created_at or datetime.now().isoformat(),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TriggerConfig":
        """Deserialise from a plain dict (e.g. a DB row).

        Missing optional fields are filled with safe defaults so that rows
        written by older schema versions continue to deserialise correctly.
        """
        return cls(
            id=data["id"],
            template_id=data["template_id"],
            mode=data.get("mode", "async"),
            secret=data.get("secret"),
            rate_limit=data.get("rate_limit", 0),
            input_map=data.get("input_map") or {},
            filters=data.get("filters") or [],
            created_at=data.get("created_at"),
            enabled=bool(data.get("enabled", True)),
        )

    @staticmethod
    def generate_id() -> str:
        """Generate a valid random trigger ID.

        Returns:
            A unique string matching the format ``trig-<12 hex chars>``.
        """
        return f"trig-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# TriggerMatcher
# ---------------------------------------------------------------------------

_KNOWN_FILTER_KEYS = frozenset({"branch", "labels", "action", "event_type"})


class TriggerMatcher:
    """Evaluates trigger filter conditions against an incoming webhook payload.

    Filters are AND-combined: all filters must pass for the trigger to fire.
    Each filter is a dict with one or more of the following keys:

    * ``branch`` (str): Matches ``payload["ref"]`` after stripping the
      ``refs/heads/`` prefix.  Case-sensitive exact match.
    * ``labels`` (list[str]): Matches when **any** label name from the payload
      is present in the filter list.  Looks in ``payload["label"]["name"]``
      (single label object) and ``payload["labels"]`` (list of label dicts,
      each with a ``name`` key).
    * ``action`` (str): Exact match against ``payload["action"]``.
    * ``event_type`` (str): Exact match against ``payload["event_type"]``.

    Unknown filter keys emit a :pylogging:`logging.WARNING` and are ignored
    (they do **not** cause the match to fail).
    """

    @staticmethod
    def matches(filters: list, payload: dict) -> bool:
        """Return True when *all* filters match the payload, False otherwise.

        An empty ``filters`` list means "no filtering" — always returns ``True``.

        Args:
            filters: List of filter dicts.  Each dict is evaluated
                independently; all must pass (AND logic).
            payload: Parsed webhook JSON body.

        Returns:
            ``True`` if all filters match (or if filters is empty),
            ``False`` if any filter does not match.
        """
        for f in filters:
            # Warn on unknown keys but do not fail the match
            for key in f:
                if key not in _KNOWN_FILTER_KEYS:
                    _logger.warning(
                        "TriggerMatcher: unknown filter key %r — ignoring", key
                    )

            # branch filter: strip refs/heads/ prefix, then exact compare
            if "branch" in f:
                ref = payload.get("ref", "")
                branch = (
                    ref[len("refs/heads/"):]
                    if ref.startswith("refs/heads/")
                    else ref
                )
                if branch != f["branch"]:
                    return False

            # labels filter: ANY label name in the payload must be in the filter list
            if "labels" in f:
                allowed = set(f["labels"])
                payload_labels: list = []

                # Single label object: {"label": {"name": "bug"}}
                single = payload.get("label")
                if isinstance(single, dict) and "name" in single:
                    payload_labels.append(single["name"])

                # List of label objects: {"labels": [{"name": "bug"}, ...]}
                multi = payload.get("labels")
                if isinstance(multi, list):
                    for lbl in multi:
                        if isinstance(lbl, dict) and "name" in lbl:
                            payload_labels.append(lbl["name"])

                # Must have at least one matching label
                if not any(lbl in allowed for lbl in payload_labels):
                    return False

            # action filter: exact match
            if "action" in f:
                if payload.get("action") != f["action"]:
                    return False

            # event_type filter: exact match
            if "event_type" in f:
                if payload.get("event_type") != f["event_type"]:
                    return False

        return True


# ---------------------------------------------------------------------------
# InputMapper
# ---------------------------------------------------------------------------

import re as _re  # noqa: E402 — already imported at top; alias for clarity

_TEMPLATE_RE = _re.compile(r"\{\{payload(?:\.([^}]+))?\}\}")


class InputMapper:
    """Resolves ``{{payload.x.y}}`` template strings from a webhook payload.

    This class is **additive** to the existing ``_apply_input_map()`` helper
    in ``web/api.py``, which handles ``$.dot.path`` expressions.

    ``InputMapper.apply()`` walks the *input_map* dict and substitutes any
    value that is a string of the form ``{{payload.a.b.c}}`` with the value
    found at ``payload["a"]["b"]["c"]`` using dot-notation traversal.

    * Values that do **not** match ``{{payload.*}}`` are returned as-is.
    * Paths that cannot be resolved yield ``None``.
    * Partial template strings (i.e. a template token embedded in a larger
      string) are **not** supported — the whole value must be a template.
    """

    @staticmethod
    def _resolve_path(payload: dict, dot_path: str) -> Any:
        """Traverse *payload* using *dot_path* (e.g. ``"repository.full_name"``).

        Args:
            payload: Parsed webhook JSON body.
            dot_path: Dot-separated key path into the payload dict.

        Returns:
            The resolved value, or ``None`` if any segment is missing.
        """
        parts = dot_path.split(".")
        value: Any = payload
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return None
        return value

    @staticmethod
    def apply(payload: dict, input_map: dict) -> dict:
        """Substitute ``{{payload.x.y}}`` templates in *input_map* values.

        Args:
            payload: Parsed webhook JSON body.
            input_map: Dict mapping pipeline variable names to template
                strings or literal values.

        Returns:
            A new dict where each ``{{payload.*}}`` value has been replaced
            with the corresponding value from *payload*.  Non-template values
            are returned unchanged.

        Example::

            payload   = {"repository": {"full_name": "org/repo"}, "ref": "refs/heads/main"}
            input_map = {"repo": "{{payload.repository.full_name}}", "env": "prod"}
            # result: {"repo": "org/repo", "env": "prod"}
        """
        result: dict = {}
        for var_name, value in input_map.items():
            if isinstance(value, str):
                m = _TEMPLATE_RE.fullmatch(value)
                if m:
                    if m.group(1) is None:
                        # bare {{payload}} — return the entire payload dict
                        result[var_name] = payload
                    else:
                        result[var_name] = InputMapper._resolve_path(payload, m.group(1))
                else:
                    result[var_name] = value
            else:
                result[var_name] = value
        return result
