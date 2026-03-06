"""Webhook trigger configuration schema and validation.

Defines TriggerConfig — the canonical in-memory representation of a
webhook trigger that maps an incoming HTTP request to a pipeline run.

This module is intentionally HTTP-agnostic: it contains only data
structures and validation, not request handling.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


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
        )

    @staticmethod
    def generate_id() -> str:
        """Generate a valid random trigger ID.

        Returns:
            A unique string matching the format ``trig-<12 hex chars>``.
        """
        return f"trig-{uuid.uuid4().hex[:12]}"
