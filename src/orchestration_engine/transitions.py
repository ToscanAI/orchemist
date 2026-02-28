"""Phase transition types for state-machine pipeline execution."""
from enum import Enum


class PhaseOutcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"
