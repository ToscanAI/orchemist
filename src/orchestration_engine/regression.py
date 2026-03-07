"""Regression data model for the Orchestration Engine.

Provides types for tracking CI regression events detected on main:
what commit caused the break, which files are affected, diagnosis summary,
fix attempt status, and resolution lifecycle.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class RegressionStatus(str, Enum):
    """Lifecycle status of a detected regression.

    Inherits from ``str`` so values can be stored/compared as plain strings
    (consistent with the rest of the codebase, e.g. ``FailureClass``,
    ``TaskState``).
    """

    DETECTED = "detected"
    """Regression identified; no diagnosis or fix attempted yet."""

    DIAGNOSING = "diagnosing"
    """Automated diagnosis is running to classify the failure."""

    FIXING = "fixing"
    """A fix pipeline run has been spawned."""

    FIXED = "fixed"
    """Fix was applied and verified; regression resolved."""

    ESCALATED = "escalated"
    """Automated fix failed or was not feasible; escalated to human."""


@dataclass
class Regression:
    """A detected regression event on the main branch.

    Attributes:
        id:                Unique UUID for this regression record.
        commit_sha:        Git SHA of the commit that introduced the regression.
        ci_run_url:        URL of the failing CI run (GitHub Actions, etc.).
        failure_type:      Short label classifying the failure (e.g. 'test_failure',
                           'build_error', 'lint_error').
        affected_files:    List of file paths implicated in the failure.
        diagnosis:         Human-readable or LLM-produced diagnosis summary.
        fix_run_id:        run_id of the spawned fix pipeline run (if any).
        status:            Current lifecycle status (see RegressionStatus).
        fix_attempt_count: Number of fix pipeline runs spawned so far.
        created_at:        UTC datetime when the regression was first detected.
    """

    commit_sha: str
    ci_run_url: str
    failure_type: str

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    affected_files: List[str] = field(default_factory=list)
    diagnosis: Optional[str] = field(default=None)
    fix_run_id: Optional[str] = field(default=None)
    status: RegressionStatus = field(default=RegressionStatus.DETECTED)
    fix_attempt_count: int = field(default=0)
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict suitable for DB insertion.

        JSON-encodes ``affected_files`` so it can be stored as a TEXT column
        and round-tripped via ``Database._row_to_dict``.

        Returns:
            Dict with all fields serialised to DB-compatible types.
        """
        return {
            "id": self.id,
            "commit_sha": self.commit_sha,
            "ci_run_url": self.ci_run_url,
            "failure_type": self.failure_type,
            "affected_files": json.dumps(self.affected_files),
            "diagnosis": self.diagnosis,
            "fix_run_id": self.fix_run_id,
            "status": self.status.value if isinstance(self.status, RegressionStatus) else self.status,
            "fix_attempt_count": self.fix_attempt_count,
            "created_at": self.created_at.isoformat(),
        }
