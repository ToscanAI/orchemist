"""Orchestration Engine - AI Agent Task Coordination.

A Python CLI tool for multi-agent task orchestration on top of OpenClaw.
Provides task queuing, retry logic, quality gates, and reusable orchestra templates.
"""

__version__ = "0.1.0"
__author__ = "Conny Lazo"
__email__ = "contact@renerivera.net"

from .schemas import (
    TaskSpec,
    TaskStatus,
    TaskResult,
    TaskSummary,
    OrchestraSpec,
    OrchestraStatus,
    QueueStats,
    Priority,
    TaskType,
    TaskState,
)
from .queue import TaskQueue
from .db import Database
from .sequencer import StateMachineSequencer
from .webhooks import TriggerConfig, TriggerValidationError, VALID_MODES  # noqa: F401
from .confidence import (  # noqa: F401  # Issue #331.1 / #429.1
    ConfidenceCalculator,
    ConfidenceResult,
    ConfidenceSignal,
    ConfidenceLevel as RunConfidenceLevel,
    DEFAULT_WEIGHTS,
    DEFAULT_WEIGHTS_V2,
    AUTO_MERGE_THRESHOLD,
    HUMAN_REVIEW_THRESHOLD,
)
from .review_catch_value import (  # noqa: F401  # Issue #4.1.3
    ReviewCatchValueCalculator,
    SEVERITY_WEIGHTS,
)
from .audit import (  # noqa: F401  # Issue #4.1.4
    AuditPhase,
    AuditResult,
    AuditIssue,
)
from .reviewer_calibration import (  # noqa: F401  # Issue #4.1.5
    ReviewerCalibrator,
    CalibrationMetrics,
)
from .trust import (  # noqa: F401  # Issue #4.2.1 / #4.2.2 / #4.2.4
    TrustProfile,
    TrustConfig,
    TrustCalibrator,
    OUTCOME_SCORES,
    VALID_OUTCOMES,
    decay_idle_profiles,
    DEFAULT_DECAY_RATE,
    DECAY_FLOOR,
    DECAY_THRESHOLD_DAYS,
)
from .routing import (  # noqa: F401  # Issue #331.2
    RoutingTier,
    RoutingConfig,
    RoutingDecision,
    RoutingEngine,
    DEFAULT_ROUTING_CONFIG,
)
from .issue_automation import (  # noqa: F401  # Issue #5.1.1
    IssueClassifier,
    IssueClassification,
    VALID_CLASSIFICATION_TYPES,
    CLASSIFICATION_TEMPLATE_MAP,
)

__all__ = [
    "__version__",
    "__author__",
    "__email__",
    # Schemas
    "TaskSpec",
    "TaskStatus",
    "TaskResult",
    "TaskSummary",
    "OrchestraSpec",
    "OrchestraStatus",
    "QueueStats",
    "Priority",
    "TaskType",
    "TaskState",
    # Core classes
    "TaskQueue",
    "Database",
    "StateMachineSequencer",
    # Webhooks (Issue #329.1)
    "TriggerConfig",
    "TriggerValidationError",
    "VALID_MODES",
    # Confidence scoring (Issue #331.1 / #429.1)
    "ConfidenceCalculator",
    "ConfidenceResult",
    "ConfidenceSignal",
    "RunConfidenceLevel",
    "DEFAULT_WEIGHTS",
    "DEFAULT_WEIGHTS_V2",
    "AUTO_MERGE_THRESHOLD",
    "HUMAN_REVIEW_THRESHOLD",
    # Review catch value signal (Issue #4.1.3)
    "ReviewCatchValueCalculator",
    "SEVERITY_WEIGHTS",
    # Adversarial audit phase (Issue #4.1.4)
    "AuditPhase",
    "AuditResult",
    "AuditIssue",
    # Reviewer calibration (Issue #4.1.5)
    "ReviewerCalibrator",
    "CalibrationMetrics",
    # Trust profile data model + calibrator + decay (Issue #4.2.1 / #4.2.2 / #4.2.4)
    "TrustProfile",
    "TrustConfig",
    "TrustCalibrator",
    "OUTCOME_SCORES",
    "VALID_OUTCOMES",
    "decay_idle_profiles",
    "DEFAULT_DECAY_RATE",
    "DECAY_FLOOR",
    "DECAY_THRESHOLD_DAYS",
    # Confidence-based routing (Issue #331.2)
    "RoutingTier",
    "RoutingConfig",
    "RoutingDecision",
    "RoutingEngine",
    "DEFAULT_ROUTING_CONFIG",
    # LLM-based issue classification (Issue #5.1.1)
    "IssueClassifier",
    "IssueClassification",
    "VALID_CLASSIFICATION_TYPES",
    "CLASSIFICATION_TEMPLATE_MAP",
]
