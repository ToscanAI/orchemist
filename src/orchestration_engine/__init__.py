"""Orchestration Engine - AI Agent Task Coordination.

A Python CLI tool for multi-agent task orchestration on top of OpenClaw.
Provides task queuing, retry logic, quality gates, and reusable orchestra templates.
"""

__version__ = "0.13.1"
__author__ = "Conny Lazo"
__email__ = "contact@renerivera.net"

from .audit import (  # noqa: F401  # Issue #4.1.4
    AuditIssue,
    AuditPhase,
    AuditResult,
)
from .confidence import (  # noqa: F401  # Issue #331.1 / #429.1
    AUTO_MERGE_THRESHOLD,
    DEFAULT_WEIGHTS,
    DEFAULT_WEIGHTS_V2,
    HUMAN_REVIEW_THRESHOLD,
    ConfidenceCalculator,
    ConfidenceResult,
    ConfidenceSignal,
)
from .confidence import (
    ConfidenceLevel as RunConfidenceLevel,
)
from .cost_tracker import (  # noqa: F401  # Issue #5.2.1
    BudgetExceededError,
    CostTracker,
    PricingTable,
)
from .db import Database
from .issue_automation import (  # noqa: F401  # Issue #5.1.1 / #5.1.2 / #5.1.3
    CLASSIFICATION_TEMPLATE_MAP,
    DEFAULT_TEMPLATE_MAPPING,
    VALID_CLASSIFICATION_TYPES,
    InputExtractor,
    IssueAutomation,
    IssueClassification,
    IssueClassifier,
    TemplateSelector,
    post_github_comment,
)
from .queue import TaskQueue
from .review_catch_value import (  # noqa: F401  # Issue #4.1.3
    SEVERITY_WEIGHTS,
    ReviewCatchValueCalculator,
)
from .reviewer_calibration import (  # noqa: F401  # Issue #4.1.5
    CalibrationMetrics,
    ReviewerCalibrator,
)
from .routing import (  # noqa: F401  # Issue #331.2
    DEFAULT_ROUTING_CONFIG,
    RoutingConfig,
    RoutingDecision,
    RoutingEngine,
    RoutingTier,
)
from .schemas import (
    OrchestraSpec,
    OrchestraStatus,
    Priority,
    QueueStats,
    TaskResult,
    TaskSpec,
    TaskState,
    TaskStatus,
    TaskSummary,
    TaskType,
)
from .sequencer import StateMachineSequencer
from .trust import (  # noqa: F401  # Issue #4.2.1 / #4.2.2 / #4.2.4
    DECAY_FLOOR,
    DECAY_THRESHOLD_DAYS,
    DEFAULT_DECAY_RATE,
    OUTCOME_SCORES,
    VALID_OUTCOMES,
    TrustCalibrator,
    TrustConfig,
    TrustProfile,
    decay_idle_profiles,
)
from .webhooks import VALID_MODES, TriggerConfig, TriggerValidationError  # noqa: F401

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
    # Template selector + input extractor (Issue #5.1.2)
    "DEFAULT_TEMPLATE_MAPPING",
    "TemplateSelector",
    "InputExtractor",
    # Issue automation orchestrator + GitHub comment utility (Issue #5.1.3)
    "IssueAutomation",
    "post_github_comment",
    # Cost tracking + pricing table (Issue #5.2.1)
    "PricingTable",
    "CostTracker",
    "BudgetExceededError",
]
