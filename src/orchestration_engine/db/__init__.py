"""Database layer for the Orchestration Engine.

Provides SQLite-backed persistent storage with WAL mode, proper indexing,
connection management, and schema migrations.

EPIC #942 / sub-issue 951a: ``db.py`` is now the ``db/`` package. This module
is the *facade* — it re-exports the exact public surface the original module
exposed (``Database``, ``default_db_path``, ``parse_json_list``,
``TERMINAL_STATUSES``, ``STALE_TASK_THRESHOLD_MINUTES``) so no caller import
line changes anywhere. The connection/transaction core lives in
:mod:`._core` (``CoreMixin``); module constants + the sqlite3 adapter
registration live in :mod:`._consts`; schema DDL in :mod:`._schema`
(``SchemaMixin``); migrations in :mod:`._migrations` (``MigrationsMixin``);
the task / task-run domain in :mod:`._tasks` (``TasksMixin``, sub-issue 951c);
the pipeline-run domain in :mod:`._pipeline_runs`
(``PipelineRunsMixin``, sub-issue 951c); the trigger / webhook-invocation
domain in :mod:`._triggers` (``TriggersMixin``, sub-issue 951d); the
sprint-chain + chain-query domain in :mod:`._chains` (``ChainsMixin``,
sub-issue 951d); the cost-API query domain in :mod:`._cost` (``CostMixin``,
sub-issue 951d); and the issue-classification domain in :mod:`._issues`
(``IssuesMixin``, sub-issue 951d).

Sub-issue 951e (the final db leaf) drains the remaining inline ``Database``
methods into per-domain mixins, leaving the class an empty composition of its
bases. The new mixins are: the review-queue / regression / CI-green /
review-outcome domain in :mod:`._reviews` (``ReviewsMixin``); the
reviewer-calibration domain in :mod:`._calibration` (``CalibrationMixin``);
the trust-profile domain in :mod:`._trust` (``TrustMixin``); the
admin-audit-log domain in :mod:`._audit` (``AuditMixin``); the
orchestra / dead-letter-queue / queue-stats domain in :mod:`._orchestra`
(``OrchestraMixin``); the diagnosis-result domain in :mod:`._diagnosis`
(``DiagnosisMixin``); the routing-decision domain in :mod:`._routing`
(``RoutingMixin``); and the failure-pattern domain in
:mod:`._failure_patterns` (``FailurePatternMixin``). ``Database`` now has an
empty body — every public method resolves through the MRO from a mixin.
"""

import logging
from pathlib import (
    Path,  # noqa: F401  # re-exported: tests patch db.Path.home for default_db_path()
)

from ._audit import AuditMixin
from ._calibration import CalibrationMixin
from ._chains import ChainsMixin
from ._consts import (  # noqa: F401  # re-exported public surface + sqlite adapter registration
    STALE_TASK_THRESHOLD_MINUTES,
    TERMINAL_STATUSES,
    default_db_path,
    parse_json_list,
)
from ._core import CoreMixin
from ._cost import CostMixin
from ._diagnosis import DiagnosisMixin
from ._failure_patterns import FailurePatternMixin
from ._issues import IssuesMixin
from ._migrations import MigrationsMixin
from ._orchestra import OrchestraMixin
from ._pipeline_runs import PipelineRunsMixin
from ._reviews import ReviewsMixin
from ._routing import RoutingMixin
from ._schema import SchemaMixin
from ._tasks import TasksMixin
from ._triggers import TriggersMixin
from ._trust import TrustMixin

logger = logging.getLogger(__name__)


class Database(
    CoreMixin,
    SchemaMixin,
    MigrationsMixin,
    TasksMixin,
    PipelineRunsMixin,
    TriggersMixin,
    ChainsMixin,
    CostMixin,
    IssuesMixin,
    OrchestraMixin,
    DiagnosisMixin,
    RoutingMixin,
    FailurePatternMixin,
    ReviewsMixin,
    CalibrationMixin,
    TrustMixin,
    AuditMixin,
):
    """SQLite database manager with connection pooling and migrations.

    The full public method surface is composed from the domain mixins listed in
    the class bases (and documented in the module docstring). db has a trivial
    single-chain MRO and no ``super()`` calls, so the empty body here is a pure
    composition: every ``db.Database.<method>`` resolves to exactly one mixin
    definition, and ``mock.patch`` on any inherited method patches+restores
    cleanly through the MRO.
    """
