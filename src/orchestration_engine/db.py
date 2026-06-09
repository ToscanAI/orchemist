"""Database layer for the Orchestration Engine.

Provides SQLite-backed persistent storage with WAL mode, proper indexing,
connection management, and schema migrations.
"""

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Explicit datetime adapters — required for Python 3.12+ which deprecated
# the built-in sqlite3 datetime adapter/converter.
sqlite3.register_adapter(datetime, lambda val: val.isoformat())
sqlite3.register_converter(
    "timestamp", lambda val: datetime.fromisoformat(val.decode())
)

from .schemas import TaskState, OrchestraState, Priority, TaskType
from .timestamps import normalize_ts, now_utc

# ---------------------------------------------------------------------------
# Shared terminal-state set — single source of truth used by db, api, and
# any other module that needs to distinguish "run is done" from "run is live".
# Add new terminal statuses here; they propagate automatically everywhere.
# ---------------------------------------------------------------------------
TERMINAL_STATUSES: frozenset = frozenset({
    "success",
    "failed",
    "cancelled",
    "crashed",
    "scoring_failed",
    "pending_review",
    "rejected",
    "escalated",   # Issue #396: retry was escalated — original run is terminal
})


# ---------------------------------------------------------------------------
# Issue #932 (item 1) — staleness threshold for queue-health reporting.
# A task that has been in 'running' state strictly longer than this many
# minutes is considered stale. Single source of truth: matches the
# QueueStats docstring ("True if tasks stuck > 30min", schemas.py). queue.py
# consumes this via has_stale_running_tasks() rather than redefining it.
# ---------------------------------------------------------------------------
STALE_TASK_THRESHOLD_MINUTES = 30


# ---------------------------------------------------------------------------
# Issue #864 — canonical default DB path resolver
# ---------------------------------------------------------------------------
# Previously this logic was duplicated 5 ways across cli.py, web/api.py,
# mcp/tools.py, daemon.py, and inline inside Database.__init__.  The mcp
# variant was the only one creating parent directories (``parents=True``),
# so the canonical form preserves that behaviour — operators who haven't
# created ``~/.orchestration-engine`` see the path created on first access
# rather than a ``FileNotFoundError``.
# ---------------------------------------------------------------------------


def default_db_path() -> Path:
    """Return the canonical persistent on-disk DB path used by async runs.

    Resolves to ``$HOME/.orchestration-engine/engine.db`` and ensures the
    parent directory exists (``mkdir(parents=True, exist_ok=True)``).  This
    is the canonical location previously duplicated 5 ways across
    :mod:`cli`, :mod:`web.api`, :mod:`mcp.tools`, :mod:`daemon`, and inline
    inside :class:`Database`.

    Returns:
        ``Path`` pointing at the engine database file.  Callers that need
        a string path can wrap with ``str(default_db_path())``.
    """
    default_dir = Path.home() / ".orchestration-engine"
    default_dir.mkdir(parents=True, exist_ok=True)
    return default_dir / "engine.db"


# ---------------------------------------------------------------------------
# Issue #866 — canonical JSON-list column parser
# ---------------------------------------------------------------------------
# The ``completed_phases`` column is stored as a JSON string in SQLite but
# may be returned by drivers as a native list (e.g. when wrapped by tests
# using TypedDict fixtures).  Both mcp/tools.py and the ``_run_to_dict``
# closure in web/api.py reinvented this parser; consolidating here keeps
# both call sites consistent if the column type ever changes.
# ---------------------------------------------------------------------------


def parse_json_list(val: Any) -> list:
    """Safely parse a JSON-list column that may be None, list, or JSON string.

    Used for the ``completed_phases`` column on ``pipeline_runs``.  Returns
    an empty list when *val* is ``None`` or cannot be decoded — callers
    should never receive a partial / malformed list from this helper.

    Args:
        val: Raw column value from a ``pipeline_runs`` row.  Tolerates
             ``None``, ``list`` (already decoded), or any other type that
             can be passed to :func:`json.loads`.

    Returns:
        A native Python list (possibly empty).  Never raises.
    """
    if val is None:
        return []
    if isinstance(val, list):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return []


class Database:
    """SQLite database manager with connection pooling and migrations."""
    
    def __init__(self, db_path: Optional[Path] = None):
        """Initialize database connection.

        Args:
            db_path: Path to SQLite database file. Defaults to ~/.orchestration-engine/engine.db
        """
        if db_path is None:
            db_path = default_db_path()
        
        # Thread-local storage for connections
        self._local = threading.local()
        
        # Detect :memory: databases — each raw ":memory:" connection is isolated
        # per connection, so threads would see empty databases.  Instead we use
        # SQLite's shared-cache in-memory URI so every thread-local connection
        # attaches to the same in-memory database while still having its own
        # connection object (avoiding multi-thread write races).
        self._shared_connection: Optional[sqlite3.Connection] = None
        if str(db_path) == ":memory:":
            db_name = uuid.uuid4().hex[:12]
            self._db_uri: Optional[str] = f"file:memdb_{db_name}?mode=memory&cache=shared"
            self.db_path = Path(":memory:")
        else:
            self._db_uri = None
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # SQLite shared-cache in-memory databases use *table-level* locking
        # rather than the WAL-based database-level locking used by file DBs.
        # Table-level locks are not subject to busy_timeout, so concurrent
        # write transactions from multiple threads (parallel pipeline phases)
        # immediately raise "database table is locked" instead of retrying.
        # We serialise all write transactions with a threading.Lock when
        # operating in shared-cache mode so callers never see that error.
        self._write_lock: Optional[threading.Lock] = (
            threading.Lock() if self._db_uri is not None else None
        )

        # Initialize database schema
        self._initialize_database()
    
    @property
    def _conn(self) -> sqlite3.Connection:
        """Alias for :meth:`get_connection` for test helper compatibility.

        Provides the ``db._conn`` attribute expected by test helpers that
        introspect the underlying SQLite connection (e.g. for SELECT queries
        on ``pipeline_runs`` in acceptance tests).
        """
        return self.get_connection()

    def get_connection(self) -> sqlite3.Connection:
        """Get a thread-local database connection.
        
        For :memory: databases we use a shared-cache URI so every thread sees
        the same data while keeping its own connection object.
        """
        if not hasattr(self._local, 'connection'):
            if self._db_uri is not None:
                self._local.connection = sqlite3.connect(
                    self._db_uri,
                    uri=True,
                    check_same_thread=False,
                    timeout=30.0,
                    detect_types=sqlite3.PARSE_DECLTYPES,
                )
            else:
                self._local.connection = sqlite3.connect(
                    str(self.db_path),
                    check_same_thread=False,
                    timeout=30.0,
                    detect_types=sqlite3.PARSE_DECLTYPES,
                )
            self._configure_connection(self._local.connection)
        
        return self._local.connection
    
    @contextmanager
    def _locked(self):
        """Acquire the write lock (if any) for shared-cache in-memory DBs.

        Use this to serialise read operations that would otherwise raise
        ``OperationalError: database table is locked`` when another thread
        holds a write lock on the same shared-cache database.
        For file-based DBs this is a no-op (WAL handles it).
        """
        if self._write_lock is not None:
            with self._write_lock:
                yield
        else:
            yield

    @contextmanager
    def transaction(self):
        """Context manager for database transactions.

        For shared-cache in-memory databases (used in tests / dry-run) the
        lock serialises writes across threads so that table-level locking
        does not raise ``OperationalError: database table is locked``.
        File-based databases with WAL mode handle concurrency natively and
        do not need the extra lock.
        """
        conn = self.get_connection()
        if self._write_lock is not None:
            with self._write_lock:
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        else:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    
    def _configure_connection(self, conn: sqlite3.Connection) -> None:
        """Configure SQLite connection with optimal settings."""
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = NORMAL") 
        conn.execute("PRAGMA cache_size = 10000")
        conn.execute("PRAGMA temp_store = memory")
        conn.execute("PRAGMA foreign_keys = ON")
        
        # Set row factory for dict-like access
        conn.row_factory = sqlite3.Row
    
    def _initialize_database(self) -> None:
        """Initialize database schema with all tables and indexes."""
        with self.transaction() as conn:
            # Create tables
            self._create_tables(conn)
            self._create_tables_pipeline_run_events(conn)
            self._create_table_routing_decisions(conn)
            self._create_table_failure_patterns(conn)
            self._create_table_regressions(conn)      # Issue #3.3a.1
            self._create_table_ci_green_shas(conn)    # Issue #3.3a.3
            self._create_table_review_outcomes(conn)  # Issue #4.1.2
            self._create_table_reviewer_calibration(conn)  # Issue #4.1.5
            self._create_table_trust_profiles(conn)        # Issue #4.2.1
            self._create_table_trust_adjustments(conn)     # Issue #4.2.1
            self._create_table_issue_pipeline_map(conn)    # Issue #5.1.1
            self._create_table_cost_tracking(conn)         # Issue #5.2.1
            self._create_table_sprint_chain_state(conn)    # Issue #514
            self._create_table_admin_audit_log(conn)       # Issue #838
            self._create_indexes(conn)
            
            # Run any pending migrations
            self._run_migrations(conn)
    
    def _create_tables(self, conn: sqlite3.Connection) -> None:
        """Create all database tables."""

        # Async pipeline runs table (Issue #267)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_runs (
                run_id TEXT PRIMARY KEY,
                template_path TEXT NOT NULL,
                template_id TEXT NOT NULL,
                input_json TEXT NOT NULL,
                mode TEXT NOT NULL,
                output_dir TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                current_phase TEXT,
                completed_phases TEXT DEFAULT '[]',
                phase_outputs TEXT DEFAULT '{}',
                pid INTEGER,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                error_message TEXT,
                gateway_url TEXT,
                skip_scoring INTEGER DEFAULT 0,
                scoring_status TEXT DEFAULT NULL,
                scoring_score REAL DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                review_reason TEXT DEFAULT NULL,
                reviewed_at TIMESTAMP DEFAULT NULL,
                reviewed_by TEXT DEFAULT NULL
            )
        """)

        # Main tasks table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                priority INTEGER DEFAULT 3,
                status TEXT DEFAULT 'queued',
                payload JSON NOT NULL,
                
                -- Timestamps
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                next_retry_at TIMESTAMP,
                
                -- Retry management
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3,
                
                -- Orchestra integration
                orchestra_id TEXT,
                orchestra_phase TEXT,
                
                -- Quality & routing
                min_confidence REAL DEFAULT 0.7,
                preferred_model TEXT,
                
                -- Constraints
                timeout_seconds INTEGER DEFAULT 3600,
                cost_limit_usd DECIMAL(10,4),
                
                -- Metadata
                created_by TEXT,
                tags JSON DEFAULT '[]',
                metadata JSON DEFAULT '{}',
                
                FOREIGN KEY(orchestra_id) REFERENCES orchestras(id)
            )
        """)
        
        # Individual execution attempts
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_runs (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                
                -- Execution context
                model TEXT NOT NULL,
                thinking_level TEXT,
                session_id TEXT,
                worker_id TEXT,
                
                -- Timing
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                
                -- Results
                status TEXT NOT NULL,
                result JSON,
                confidence REAL,
                error_message TEXT,
                error_type TEXT,
                
                -- Resource usage
                tokens_used INTEGER DEFAULT 0,
                cost_usd DECIMAL(10,4),
                peak_memory_mb INTEGER,

                FOREIGN KEY(task_id) REFERENCES tasks(id),
                UNIQUE(task_id, attempt_number)
            )
        """)
        
        # Multi-task workflows (orchestras)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orchestras (
                id TEXT PRIMARY KEY,
                template TEXT NOT NULL,
                name TEXT,
                status TEXT DEFAULT 'running',
                
                -- Configuration
                config JSON NOT NULL,
                priority INTEGER DEFAULT 3,
                
                -- Progress tracking
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                total_tasks INTEGER DEFAULT 0,
                completed_tasks INTEGER DEFAULT 0,
                failed_tasks INTEGER DEFAULT 0,
                cancelled_tasks INTEGER DEFAULT 0,
                
                -- Resource limits
                cost_budget_usd DECIMAL(10,4),
                time_budget_hours INTEGER,
                cost_spent_usd DECIMAL(10,4) DEFAULT 0.0,
                
                -- Metadata
                created_by TEXT,
                tags JSON DEFAULT '[]',
                current_phase TEXT
            )
        """)
        
        # Dead letter queue for permanently failed tasks
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dead_letter_queue (
                id TEXT PRIMARY KEY,
                original_task_id TEXT NOT NULL,
                task_type TEXT NOT NULL,
                failure_reason TEXT NOT NULL,
                failure_count INTEGER NOT NULL,
                payload JSON NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                
                -- Analysis metadata
                error_patterns JSON DEFAULT '[]',
                suggested_fixes JSON DEFAULT '[]',
                
                FOREIGN KEY(original_task_id) REFERENCES tasks(id)
            )
        """)

        # Webhook trigger configuration (Issue #329.1)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS triggers (
                id TEXT PRIMARY KEY,
                template_id TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'async',
                secret TEXT,
                rate_limit INTEGER NOT NULL DEFAULT 0,
                input_map TEXT NOT NULL DEFAULT '{}',
                filters TEXT NOT NULL DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                enabled INTEGER NOT NULL DEFAULT 1
            )
        """)

        # Webhook invocation log for rate-limit enforcement (Issue #329.2)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS webhook_invocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_id TEXT NOT NULL,
                invoked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Diagnosis results for failure-diagnosis subsystem (Issue #3.1.1)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS diagnosis_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                failure_class TEXT NOT NULL,
                remediation TEXT NOT NULL,
                confidence REAL NOT NULL,
                explanation TEXT,
                model_used TEXT,
                tokens_consumed INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(run_id) REFERENCES pipeline_runs(run_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_diagnosis_results_run_id
            ON diagnosis_results(run_id)
        """)

    def _create_tables_pipeline_run_events(self, conn: sqlite3.Connection) -> None:
        """Create pipeline_run_events table for SSE live-progress streaming (Issue #258)."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                phase_id TEXT,
                tokens_consumed INTEGER,
                cost_usd REAL,
                state TEXT,
                metadata_json TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(run_id) REFERENCES pipeline_runs(run_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pipeline_run_events_run_id
            ON pipeline_run_events(run_id, id)
        """)

    def _create_table_routing_decisions(self, conn: sqlite3.Connection) -> None:
        """Create routing_decisions table for confidence-based routing outcomes (Issue #331.3)."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS routing_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                confidence_score REAL NOT NULL,
                tier_name TEXT NOT NULL,
                action TEXT NOT NULL,
                justification TEXT,
                signals_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(run_id) REFERENCES pipeline_runs(run_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_routing_decisions_run_id
            ON routing_decisions(run_id)
        """)

    def _create_table_failure_patterns(self, conn: sqlite3.Connection) -> None:
        """Create failure_patterns table for systemic failure detection (Issue #3.1.3).

        Tracks recurring failure signatures per template and marks patterns as
        *systemic* when the same error recurs more than ``SYSTEMIC_THRESHOLD``
        times within ``SYSTEMIC_WINDOW_DAYS`` days.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS failure_patterns (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_hash     TEXT NOT NULL,
                template_id      TEXT NOT NULL,
                failure_class    TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                is_systemic      INTEGER NOT NULL DEFAULT 0,
                first_seen_at    TEXT NOT NULL,
                last_seen_at     TEXT NOT NULL,
                UNIQUE(pattern_hash, template_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_failure_patterns_template
            ON failure_patterns (template_id, last_seen_at)
        """)

    def _create_table_regressions(self, conn: sqlite3.Connection) -> None:
        """Create regressions table for regression tracking (Issue #3.3a.1).

        Called from _initialize_database so fresh databases get the table
        without requiring a migration run.  Idempotent via
        ``CREATE TABLE IF NOT EXISTS``.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS regressions (
                id                TEXT PRIMARY KEY,
                commit_sha        TEXT NOT NULL,
                ci_run_url        TEXT NOT NULL,
                failure_type      TEXT NOT NULL,
                affected_files    TEXT NOT NULL DEFAULT '[]',
                diagnosis         TEXT,
                fix_run_id        TEXT,
                status            TEXT NOT NULL DEFAULT 'detected',
                fix_attempt_count INTEGER NOT NULL DEFAULT 0,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_regressions_status_created
            ON regressions(status, created_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_regressions_commit_sha
            ON regressions(commit_sha)
        """)

    def _create_table_ci_green_shas(self, conn: sqlite3.Connection) -> None:
        """Create ci_green_shas table for tracking last-known-green CI SHA (Issue #3.3a.3).

        Called from _initialize_database so fresh databases get the table
        without requiring a migration run.  Idempotent via
        ``CREATE TABLE IF NOT EXISTS``.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ci_green_shas (
                repo_slug  TEXT PRIMARY KEY,
                sha        TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

    def _create_table_review_outcomes(self, conn: sqlite3.Connection) -> None:
        """Create review_outcomes table for durable review result storage (Issue #4.1.2).

        Stores one row per review phase execution.  Idempotent via
        ``CREATE TABLE IF NOT EXISTS``.

        Columns:
            review_id:      UUID primary key.
            run_id:         Foreign key to ``pipeline_runs.run_id``.
            phase_id:       Phase identifier within the run (e.g. ``"review"``).
            reviewer_model: Model tier/name used for the review.
            verdict:        ``"APPROVE"``, ``"REQUEST_CHANGES"``, or ``NULL``.
            issues_found:   JSON-encoded list of issue dicts.
            fix_verified:   Boolean (0/1) — set to 1 when a subsequent fix
                            run verified the issues were resolved.
            created_at:     Timestamp (UTC).
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_outcomes (
                review_id      TEXT PRIMARY KEY,
                run_id         TEXT NOT NULL,
                phase_id       TEXT NOT NULL,
                reviewer_model TEXT,
                verdict        TEXT,
                issues_found   TEXT NOT NULL DEFAULT '[]',
                fix_verified   INTEGER NOT NULL DEFAULT 0,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(run_id) REFERENCES pipeline_runs(run_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_review_outcomes_run_id
            ON review_outcomes(run_id, created_at)
        """)

    def _create_table_reviewer_calibration(self, conn: sqlite3.Connection) -> None:
        """Create reviewer_calibration table for longitudinal accuracy tracking (Issue #4.1.5).

        Stores one calibration snapshot per ``(reviewer_model, computed_at)``
        pair.  Idempotent via ``CREATE TABLE IF NOT EXISTS``.

        Columns:
            id:                          Auto-increment primary key.
            reviewer_model:              Model tier/name (e.g. ``"opus"``).
            total_reviews:               Total outcomes observed.
            approve_count:               Number of APPROVE verdicts.
            request_changes_count:       Number of REQUEST_CHANGES verdicts.
            approve_held_up_count:       APPROVEs where no fix was needed.
            request_changes_valid_count: REQUEST_CHANGES confirmed by a
                                         verified fix.
            approve_accuracy:            ``approve_held_up / approve_count``
                                         (NULL when no APPROVEs observed).
            request_changes_accuracy:    ``rc_valid / rc_count``
                                         (NULL when no RC verdicts observed).
            overall_accuracy:            Combined accuracy (NULL when empty).
            computed_at:                 UTC timestamp of snapshot creation.
            aggregation_window:          Optional label for the time window
                                         (e.g. ``"30d"``, ``"all-time"``).
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reviewer_calibration (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                reviewer_model              TEXT NOT NULL,
                total_reviews               INTEGER NOT NULL DEFAULT 0,
                approve_count               INTEGER NOT NULL DEFAULT 0,
                request_changes_count       INTEGER NOT NULL DEFAULT 0,
                approve_held_up_count       INTEGER NOT NULL DEFAULT 0,
                request_changes_valid_count INTEGER NOT NULL DEFAULT 0,
                approve_accuracy            REAL,
                request_changes_accuracy    REAL,
                overall_accuracy            REAL,
                computed_at                 TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                aggregation_window          TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_reviewer_calibration_model
            ON reviewer_calibration(reviewer_model, computed_at DESC)
        """)

    def _create_indexes(self, conn: sqlite3.Connection) -> None:
        """Create performance indexes."""

        # pipeline_runs index (Issue #267)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status
            ON pipeline_runs(status, created_at)
        """)

        # Core query patterns for tasks
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_status_priority 
            ON tasks(status, priority DESC)
        """)
        
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_orchestra 
            ON tasks(orchestra_id, orchestra_phase)
        """)
        
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_retry 
            ON tasks(status, next_retry_at) 
            WHERE status = 'retry'
        """)
        
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_created_at 
            ON tasks(created_at)
        """)
        
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_type_status 
            ON tasks(type, status)
        """)
        
        # Task runs indexes
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_task_runs_task 
            ON task_runs(task_id, attempt_number)
        """)
        
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_task_runs_model_metrics 
            ON task_runs(model, status, completed_at)
        """)
        
        # Orchestra indexes
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_orchestras_status 
            ON orchestras(status, created_at)
        """)
        
        # Analytics indexes
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_cost_tracking 
            ON tasks(type, created_at, cost_limit_usd)
        """)
        
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_dead_letter_analysis
            ON dead_letter_queue(task_type, created_at)
        """)

        # Trigger indexes (Issue #329.1)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_triggers_template_id
            ON triggers(template_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_triggers_mode_created
            ON triggers(mode, created_at)
        """)

        # Webhook invocation index for rate-limit queries (Issue #329.2)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_webhook_invocations_trigger_time
            ON webhook_invocations(trigger_id, invoked_at)
        """)
    
    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """Run any pending database migrations."""
        # Create migrations table if it doesn't exist
        conn.execute("""
            CREATE TABLE IF NOT EXISTS migrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Get applied migrations
        cursor = conn.execute("SELECT name FROM migrations")
        applied_migrations = {row[0] for row in cursor.fetchall()}
        
        # Define migrations
        migrations = [
            ("001_add_scoring_status", self._migration_001_add_scoring_status),
            ("002_add_pipeline_run_events", self._migration_002_add_pipeline_run_events),
            ("003_add_triggers_table", self._migration_003_add_triggers_table),   # Issue #329.1
            ("004_add_webhook_invocations", self._migration_004_add_webhook_invocations),   # Issue #329.2
            ("005_add_trigger_enabled", self._migration_005_add_trigger_enabled),           # Issue #329.2
            ("006_add_chain_columns", self._migration_006_add_chain_columns),               # Issue #330.1
            ("007_add_routing_decisions", self._migration_007_add_routing_decisions),       # Issue #331.3
            ("008_add_review_columns", self._migration_008_add_review_columns),             # Issue #331.4
            ("009_add_diagnosis_tables", self._migration_009_add_diagnosis_tables),         # Issue #3.1.1
            ("010_add_failure_patterns_table", self._migration_010_add_failure_patterns_table),  # Issue #3.1.3
            ("011_add_retry_columns", self._migration_011_add_retry_columns),               # Issue #3.2.1
            ("012_add_regressions_table", self._migration_012_add_regressions_table),      # Issue #3.3a.1
            ("013_add_ci_green_shas_table", self._migration_013_add_ci_green_shas_table),  # Issue #3.3a.3
            ("014_add_review_outcomes_table", self._migration_014_add_review_outcomes_table),  # Issue #4.1.2
            ("015_add_reviewer_calibration_table", self._migration_015_add_reviewer_calibration_table),  # Issue #4.1.5
            ("016_add_trust_tables", self._migration_016_add_trust_tables),                              # Issue #4.2.1
            ("017_add_issue_pipeline_map", self._migration_017_add_issue_pipeline_map),               # Issue #5.1.1
            ("018_add_cost_tracking_table", self._migration_018_add_cost_tracking_table),             # Issue #5.2.1
            ("019_add_parent_run_id_index", self._migration_019_add_parent_run_id_index),             # Issue #508
            ("020_add_sprint_chain_state_table", self._migration_020_add_sprint_chain_state_table),  # Issue #514
        ]
        
        # Apply pending migrations
        for name, migration_func in migrations:
            if name not in applied_migrations:
                migration_func(conn)
                conn.execute("INSERT INTO migrations (name) VALUES (?)", (name,))
    
    def _migration_001_add_scoring_status(self, conn: sqlite3.Connection) -> None:
        """Add scoring_status and scoring_score columns to pipeline_runs (Issue #287).

        Idempotent: silently ignores OperationalError if the columns already
        exist (e.g. fresh databases created with the updated DDL).
        """
        try:
            conn.execute(
                "ALTER TABLE pipeline_runs ADD COLUMN scoring_status TEXT DEFAULT NULL"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

        try:
            conn.execute(
                "ALTER TABLE pipeline_runs ADD COLUMN scoring_score REAL DEFAULT NULL"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

    def _migration_002_add_pipeline_run_events(self, conn: sqlite3.Connection) -> None:
        """Add pipeline_run_events table for SSE live-progress streaming (Issue #258).

        Idempotent: uses CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS.
        Safe to run on both fresh and existing databases.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                phase_id TEXT,
                tokens_consumed INTEGER,
                cost_usd REAL,
                state TEXT,
                metadata_json TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(run_id) REFERENCES pipeline_runs(run_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pipeline_run_events_run_id
            ON pipeline_run_events(run_id, id)
        """)

    def _migration_003_add_triggers_table(self, conn: sqlite3.Connection) -> None:
        """Add triggers table for webhook trigger configuration (Issue #329.1).

        Idempotent: uses CREATE TABLE IF NOT EXISTS and
        CREATE INDEX IF NOT EXISTS so it is safe to run on both fresh and
        existing databases without data loss.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS triggers (
                id TEXT PRIMARY KEY,
                template_id TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'async',
                secret TEXT,
                rate_limit INTEGER NOT NULL DEFAULT 0,
                input_map TEXT NOT NULL DEFAULT '{}',
                filters TEXT NOT NULL DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_triggers_template_id
            ON triggers(template_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_triggers_mode_created
            ON triggers(mode, created_at)
        """)

    def _migration_004_add_webhook_invocations(self, conn: sqlite3.Connection) -> None:
        """Add webhook_invocations table for per-trigger rate-limit enforcement (Issue #329.2).

        Idempotent: uses CREATE TABLE IF NOT EXISTS so it is safe to run on
        both fresh and existing databases.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS webhook_invocations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_id TEXT NOT NULL,
                invoked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_webhook_invocations_trigger_time
            ON webhook_invocations(trigger_id, invoked_at)
        """)

    def _migration_005_add_trigger_enabled(self, conn: sqlite3.Connection) -> None:
        """Add enabled column to triggers table (Issue #329.2).

        Idempotent: silently ignores OperationalError if the column already
        exists (e.g. fresh databases created with the updated DDL).
        """
        try:
            conn.execute(
                "ALTER TABLE triggers ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

    def _migration_006_add_chain_columns(self, conn: sqlite3.Connection) -> None:
        """Add parent_run_id and chain_depth columns to pipeline_runs (Issue #330.1).

        These columns support pipeline chaining: child runs record their
        parent run's ID and their depth in the chain (to enforce
        ``max_chain_depth`` and prevent infinite loops).

        Idempotent: silently ignores OperationalError if the columns already
        exist (e.g. fresh databases created with the updated DDL).
        """
        try:
            conn.execute(
                "ALTER TABLE pipeline_runs ADD COLUMN parent_run_id TEXT DEFAULT NULL"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

        try:
            conn.execute(
                "ALTER TABLE pipeline_runs ADD COLUMN chain_depth INTEGER DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

    def _migration_007_add_routing_decisions(self, conn: sqlite3.Connection) -> None:
        """Add routing_decisions table for confidence-based routing outcomes (Issue #331.3).

        Idempotent: uses CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS.
        Safe to run on both fresh and existing databases.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS routing_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                confidence_score REAL NOT NULL,
                tier_name TEXT NOT NULL,
                action TEXT NOT NULL,
                justification TEXT,
                signals_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(run_id) REFERENCES pipeline_runs(run_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_routing_decisions_run_id
            ON routing_decisions(run_id)
        """)

    # Task Operations
    
    def insert_task(self, task_data: Dict[str, Any]) -> str:
        """Insert a new task into the database."""
        with self.transaction() as conn:
            conn.execute("""
                INSERT INTO tasks (
                    id, type, priority, status, payload, max_retries,
                    orchestra_id, orchestra_phase, min_confidence, preferred_model,
                    timeout_seconds, cost_limit_usd, created_by, tags, metadata
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (
                task_data['id'],
                task_data['type'],
                task_data.get('priority', 3),
                task_data.get('status', 'queued'),
                json.dumps(task_data['payload']),
                task_data.get('max_retries', 3),
                task_data.get('orchestra_id'),
                task_data.get('orchestra_phase'),
                task_data.get('min_confidence', 0.7),
                task_data.get('preferred_model'),
                task_data.get('timeout_seconds', 3600),
                task_data.get('cost_limit_usd'),
                task_data.get('created_by'),
                json.dumps(task_data.get('tags', [])),
                json.dumps(task_data.get('metadata', {}))
            ))
        
        return task_data['id']
    
    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get a task by ID."""
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
        
        if row is None:
            return None
        
        return self._row_to_dict(row)
    
    def update_task_status(self, task_id: str, status: str, **kwargs) -> bool:
        """Update task status and related fields."""
        updates = ['status = ?']
        values = [status]
        
        # Handle status-specific updates
        if status == 'running' and 'started_at' not in kwargs:
            kwargs['started_at'] = now_utc()
        elif status in ['success', 'failed', 'permanently_failed'] and 'completed_at' not in kwargs:
            kwargs['completed_at'] = now_utc()
        
        # Add additional updates
        for key, value in kwargs.items():
            if key in ['started_at', 'completed_at', 'next_retry_at']:
                updates.append(f"{key} = ?")
                values.append(value)
            elif key == 'retry_count':
                updates.append("retry_count = retry_count + 1")
            elif key == 'metadata':
                updates.append("metadata = ?")
                values.append(json.dumps(value, default=str))
        
        values.append(task_id)
        
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?",
                values
            )
            return cursor.rowcount > 0
    
    def get_next_task(self, worker_id: str) -> Optional[Dict[str, Any]]:
        """Get the next available task for execution."""
        with self.transaction() as conn:
            # Find next task using priority and retry logic
            cursor = conn.execute("""
                SELECT * FROM tasks 
                WHERE (status = 'queued' OR (status = 'retry' AND next_retry_at <= CURRENT_TIMESTAMP))
                ORDER BY 
                    CASE 
                        WHEN status = 'retry' THEN priority - 0.5 
                        ELSE priority 
                    END ASC,
                    created_at ASC
                LIMIT 1
            """)
            
            row = cursor.fetchone()
            if row is None:
                return None
            
            # Mark task as running
            task_id = row['id']
            conn.execute("""
                UPDATE tasks 
                SET status = 'running', started_at = CURRENT_TIMESTAMP 
                WHERE id = ?
            """, (task_id,))
            
            return self._row_to_dict(row)
    
    def list_tasks(
        self,
        states: Optional[List[str]] = None,
        types: Optional[List[str]] = None,
        orchestra_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """List tasks with optional filtering."""
        query = "SELECT * FROM tasks WHERE 1=1"
        params = []
        
        if states:
            placeholders = ','.join('?' * len(states))
            query += f" AND status IN ({placeholders})"
            params.extend(states)
        
        if types:
            placeholders = ','.join('?' * len(types))
            query += f" AND type IN ({placeholders})"
            params.extend(types)
        
        if orchestra_id:
            query += " AND orchestra_id = ?"
            params.append(orchestra_id)
        
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        conn = self.get_connection()
        cursor = conn.execute(query, params)
        
        return [self._row_to_dict(row) for row in cursor.fetchall()]
    
    def cancel_task(self, task_id: str) -> bool:
        """Cancel a queued or running task."""
        with self.transaction() as conn:
            cursor = conn.execute("""
                UPDATE tasks 
                SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP 
                WHERE id = ? AND status IN ('queued', 'running', 'retry')
            """, (task_id,))
            
            return cursor.rowcount > 0
    
    # Task Run Operations
    
    def insert_task_run(self, run_data: Dict[str, Any]) -> str:
        """Insert a new task run record."""
        with self.transaction() as conn:
            conn.execute("""
                INSERT INTO task_runs (
                    id, task_id, attempt_number, model, thinking_level,
                    session_id, worker_id, status, result, confidence,
                    error_message, error_type, tokens_used, cost_usd, peak_memory_mb
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (
                run_data['id'],
                run_data['task_id'],
                run_data['attempt_number'],
                run_data['model'],
                run_data.get('thinking_level'),
                run_data.get('session_id'),
                run_data.get('worker_id'),
                run_data['status'],
                json.dumps(run_data.get('result')) if run_data.get('result') else None,
                run_data.get('confidence'),
                run_data.get('error_message'),
                run_data.get('error_type'),
                run_data.get('tokens_used', 0),
                run_data.get('cost_usd'),
                run_data.get('peak_memory_mb')
            ))
        
        return run_data['id']
    
    def update_task_run(self, run_id: str, **kwargs) -> bool:
        """Update task run with completion data."""
        updates = []
        values = []
        
        for key, value in kwargs.items():
            if key == 'result':
                updates.append("result = ?")
                values.append(json.dumps(value) if value else None)
            elif key in ['completed_at', 'status', 'confidence', 'error_message', 'error_type', 
                        'tokens_used', 'cost_usd', 'peak_memory_mb']:
                updates.append(f"{key} = ?")
                values.append(value)
        
        if not updates:
            return False
        
        values.append(run_id)
        
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE task_runs SET {', '.join(updates)} WHERE id = ?",
                values
            )
            return cursor.rowcount > 0

    # Task Run Aggregation Readers (Issue #932 item 1)
    #
    # These roll up the EXISTING task_runs rows (and, for staleness, the tasks
    # table) for the queue-health surface in queue.py. They author the SQL the
    # data layer previously lacked (task_runs had writers but no aggregation
    # readers). Every aggregate column is aliased so _row_to_dict keys it
    # addressably; every SUM is COALESCE-guarded so empty/all-NULL data yields
    # a clean zero instead of NULL/raise.

    def get_total_tokens_consumed(self) -> int:
        """Total tokens used across all task_runs rows (all time).

        Sums task_runs.tokens_used over every attempt record. Returns 0 when
        there are no rows or all values are NULL (COALESCE guard).
        """
        row = self.fetch_one(
            "SELECT COALESCE(SUM(tokens_used), 0) AS total FROM task_runs"
        )
        return int(row["total"]) if row else 0

    def get_task_tokens_consumed(self, task_id: str) -> int:
        """Total tokens used across all attempts (task_runs) for one task.

        Sums task_runs.tokens_used WHERE task_id = ?. Returns 0 for an unknown
        task id or when all matching values are NULL.
        """
        row = self.fetch_one(
            "SELECT COALESCE(SUM(tokens_used), 0) AS total "
            "FROM task_runs WHERE task_id = ?",
            (task_id,),
        )
        return int(row["total"]) if row else 0

    def get_total_cost_today(self) -> Decimal:
        """Sum of task_runs.cost_usd for runs completed today (UTC).

        Cost is realized at attempt completion, so only rows with a non-NULL
        completed_at on today's UTC calendar date contribute. Returns
        Decimal('0.00') when no matching rows exist.
        """
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.get_total_cost_for_date(today_str)

    def get_total_cost_for_date(self, date_str: str) -> Decimal:
        """Sum of task_runs.cost_usd for runs whose completed_at date == date_str (UTC).

        SQLite DATE() extracts the calendar date regardless of whether
        completed_at was stored space-separated (CURRENT_TIMESTAMP) or
        T-separated (datetime.isoformat()), so the comparison is format-robust.

        The summation is performed in Decimal space rather than via SQL SUM():
        SQLite's DECIMAL(10,4) column has NUMERIC affinity backed by IEEE-754
        floats, so an in-engine SUM(cost_usd) drifts (0.10 + 0.20 ->
        0.30000000000000004). Pulling each value and folding it through
        Decimal(str(value)) yields the exact, drift-free total the Decimal
        contract requires.

        Args:
            date_str: 'YYYY-MM-DD' (UTC).

        Returns:
            Decimal sum of matching cost_usd, or Decimal('0.00') if none.
        """
        rows = self.fetch_all(
            "SELECT cost_usd FROM task_runs "
            "WHERE DATE(completed_at) = ? AND cost_usd IS NOT NULL",
            (date_str,),
        )
        total = Decimal('0')
        for row in rows:
            # str() first, never Decimal(float): str(0.1) is '0.1', whereas
            # Decimal(0.1) would be 0.1000000000000000055...
            total += Decimal(str(row["cost_usd"]))
        return total

    def has_stale_running_tasks(
        self, threshold_minutes: int = STALE_TASK_THRESHOLD_MINUTES
    ) -> bool:
        """True iff any task has been in 'running' state longer than threshold_minutes.

        Uses julianday() on BOTH sides so the comparison is correct regardless
        of whether tasks.started_at was written as SQLite CURRENT_TIMESTAMP
        (UTC, space-separated) or as a Python datetime.now().isoformat()
        (T-separated) — a raw string '<' is wrong because 'T' (0x54) sorts above
        ' ' (0x20), silently missing T-form stale rows. Compares against
        SQLite's own clock ('now', UTC), matching CURRENT_TIMESTAMP. Strict '<'
        on started_at means a task running exactly threshold_minutes is NOT
        stale; one running threshold_minutes + a moment IS.
        """
        row = self.fetch_one(
            "SELECT COUNT(*) AS n FROM tasks "
            "WHERE status = 'running' "
            "AND started_at IS NOT NULL "
            "AND julianday(started_at) < julianday('now', ?)",
            (f'-{int(threshold_minutes)} minutes',),
        )
        return bool(row["n"]) if row else False

    # Orchestra Operations
    
    def insert_orchestra(self, orchestra_data: Dict[str, Any]) -> str:
        """Insert a new orchestra workflow."""
        with self.transaction() as conn:
            conn.execute("""
                INSERT INTO orchestras (
                    id, template, name, status, config, priority,
                    cost_budget_usd, time_budget_hours, created_by, tags
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (
                orchestra_data['id'],
                orchestra_data['template'],
                orchestra_data.get('name'),
                orchestra_data.get('status', 'running'),
                json.dumps(orchestra_data['config']),
                orchestra_data.get('priority', 3),
                orchestra_data.get('cost_budget_usd'),
                orchestra_data.get('time_budget_hours'),
                orchestra_data.get('created_by'),
                json.dumps(orchestra_data.get('tags', []))
            ))
        
        return orchestra_data['id']
    
    def get_orchestra(self, orchestra_id: str) -> Optional[Dict[str, Any]]:
        """Get orchestra by ID."""
        conn = self.get_connection()
        cursor = conn.execute("SELECT * FROM orchestras WHERE id = ?", (orchestra_id,))
        row = cursor.fetchone()
        
        if row is None:
            return None
        
        return self._row_to_dict(row)
    
    def update_orchestra_stats(self, orchestra_id: str) -> bool:
        """Update orchestra task counts based on current task states."""
        with self.transaction() as conn:
            cursor = conn.execute("""
                UPDATE orchestras 
                SET 
                    total_tasks = (
                        SELECT COUNT(*) FROM tasks WHERE orchestra_id = ?
                    ),
                    completed_tasks = (
                        SELECT COUNT(*) FROM tasks 
                        WHERE orchestra_id = ? AND status = 'success'
                    ),
                    failed_tasks = (
                        SELECT COUNT(*) FROM tasks 
                        WHERE orchestra_id = ? AND status IN ('failed', 'permanently_failed')
                    ),
                    cancelled_tasks = (
                        SELECT COUNT(*) FROM tasks 
                        WHERE orchestra_id = ? AND status = 'cancelled'
                    )
                WHERE id = ?
            """, (orchestra_id, orchestra_id, orchestra_id, orchestra_id, orchestra_id))
            
            return cursor.rowcount > 0
    
    # Dead Letter Queue Operations
    
    def move_to_dead_letter(self, task_id: str, failure_reason: str) -> bool:
        """Move a permanently failed task to dead letter queue."""
        with self.transaction() as conn:
            # Get task data
            cursor = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            task_row = cursor.fetchone()
            
            if task_row is None:
                return False
            
            # Insert into dead letter queue
            conn.execute("""
                INSERT INTO dead_letter_queue (
                    id, original_task_id, task_type, failure_reason,
                    failure_count, payload, error_patterns, suggested_fixes
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (
                f"dl_{task_id}",
                task_id,
                task_row['type'],
                failure_reason,
                task_row['retry_count'],
                task_row['payload'],
                json.dumps([]),  # TODO: Analyze error patterns
                json.dumps([])   # TODO: Generate suggested fixes
            ))
            
            # Update original task status
            conn.execute("""
                UPDATE tasks 
                SET status = 'permanently_failed', completed_at = CURRENT_TIMESTAMP 
                WHERE id = ?
            """, (task_id,))
            
            return True
    
    # Statistics and Analytics
    
    def get_queue_stats(self) -> Dict[str, Any]:
        """Get comprehensive queue statistics."""
        conn = self.get_connection()
        
        # Basic counts by status
        cursor = conn.execute("""
            SELECT status, COUNT(*) as count
            FROM tasks 
            GROUP BY status
        """)
        status_counts = {row[0]: row[1] for row in cursor.fetchall()}
        
        # Priority breakdown
        cursor = conn.execute("""
            SELECT priority, COUNT(*) as count
            FROM tasks 
            WHERE status IN ('queued', 'running', 'retry')
            GROUP BY priority
        """)
        priority_counts = {f"priority_{row[0]}": row[1] for row in cursor.fetchall()}
        
        # Type breakdown
        cursor = conn.execute("""
            SELECT type, COUNT(*) as count
            FROM tasks 
            WHERE status IN ('queued', 'running', 'retry')
            GROUP BY type
        """)
        type_counts = {row[0]: row[1] for row in cursor.fetchall()}
        
        # Average execution time
        cursor = conn.execute("""
            SELECT AVG(
                (julianday(completed_at) - julianday(started_at)) * 86400
            ) as avg_seconds
            FROM tasks 
            WHERE started_at IS NOT NULL AND completed_at IS NOT NULL
        """)
        avg_execution_time = cursor.fetchone()[0]
        
        # Dead letter count
        cursor = conn.execute("SELECT COUNT(*) FROM dead_letter_queue")
        dead_letter_count = cursor.fetchone()[0]
        
        return {
            'timestamp': now_utc(),
            'queued': status_counts.get('queued', 0),
            'running': status_counts.get('running', 0),
            'completed': status_counts.get('success', 0),
            'failed': status_counts.get('failed', 0),
            'retrying': status_counts.get('retry', 0),
            'cancelled': status_counts.get('cancelled', 0),
            'priority_breakdown': priority_counts,
            'type_breakdown': type_counts,
            'avg_execution_time_seconds': avg_execution_time,
            'dead_letter_count': dead_letter_count,
            'active_workers': 0,  # TODO: Track active workers
            'max_workers': 8,
        }
    
    # Generic Query Methods

    def execute(self, query: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a SQL query and return the cursor.

        Auto-commits after execution so callers (concurrency.py, progress.py,
        recovery.py) don't have to manage transactions explicitly.  DDL
        statements (CREATE TABLE, etc.) already have implicit commit semantics
        in SQLite; DML (INSERT/UPDATE/DELETE) is committed here so the write
        is durable from the caller's perspective.
        """
        conn = self.get_connection()
        cursor = conn.execute(query, params)
        conn.commit()
        return cursor

    def fetch_all(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """Execute query and return all rows as dicts (no commit — read-only)."""
        conn = self.get_connection()
        cursor = conn.execute(query, params)
        return [self._row_to_dict(row) for row in cursor.fetchall()]

    def fetch_one(self, query: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        """Execute query and return first row as dict, or None (no commit — read-only)."""
        conn = self.get_connection()
        cursor = conn.execute(query, params)
        row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    # Utility Methods
    
    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Convert SQLite row to dictionary with JSON parsing."""
        data = dict(row)
        
        # Parse JSON fields
        json_fields = [
            'payload', 'tags', 'metadata', 'config', 'result',
            'error_patterns', 'suggested_fixes',
            'input_map', 'filters',   # trigger fields (Issue #329.1)
            'signals_json',           # routing_decisions (Issue #331.3)
            'affected_files',         # regressions (Issue #3.3a.1)
            'issues_found',           # review_outcomes (Issue #4.1.2)
        ]
        for field in json_fields:
            if field in data and data[field] is not None:
                try:
                    data[field] = json.loads(data[field])
                except (json.JSONDecodeError, TypeError):
                    pass  # Keep original value if JSON parsing fails

        # Normalise datetime objects → ISO strings so callers (queue.py etc.)
        # that do datetime.fromisoformat(value) continue to work unchanged.
        # PARSE_DECLTYPES converts TIMESTAMP columns to datetime objects; we
        # convert them back to strings here to preserve the public contract.
        for key, value in data.items():
            if isinstance(value, datetime):
                data[key] = value.isoformat()

        # Cast SQLite INTEGER columns that represent booleans to Python bool
        if "enabled" in data and data["enabled"] is not None:
            data["enabled"] = bool(data["enabled"])
        
        return data
    
    # ------------------------------------------------------------------
    # Pipeline Run Operations (Issue #267 — async daemon)
    # ------------------------------------------------------------------

    def insert_pipeline_run(self, run_data: Dict[str, Any]) -> str:
        """Insert a new async pipeline run record."""
        with self.transaction() as conn:
            conn.execute("""
                INSERT INTO pipeline_runs (
                    run_id, template_path, template_id, input_json, mode,
                    output_dir, status, gateway_url, skip_scoring,
                    parent_run_id, chain_depth,
                    retry_of_run_id, retry_strategy
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_data['run_id'],
                run_data['template_path'],
                run_data['template_id'],
                run_data['input_json'],
                run_data['mode'],
                run_data['output_dir'],
                run_data.get('status', 'pending'),
                run_data.get('gateway_url'),
                int(run_data.get('skip_scoring', 0)),
                run_data.get('parent_run_id'),         # Issue #330.1: chaining parent
                int(run_data.get('chain_depth', 0)),   # Issue #330.1: chaining depth
                run_data.get('retry_of_run_id'),       # Issue #3.2.1: retry linkage
                run_data.get('retry_strategy'),        # Issue #3.2.1: retry strategy applied
            ))
        return run_data['run_id']

    def update_pipeline_run(self, run_id: str, **kwargs) -> bool:
        """Update fields on a pipeline_runs row."""
        if not kwargs:
            return False
        allowed = {
            'status', 'current_phase', 'completed_phases', 'phase_outputs',
            'pid', 'started_at', 'completed_at', 'error_message', 'gateway_url',
            'skip_scoring', 'scoring_status', 'scoring_score',
            'retry_of_run_id', 'retry_strategy',       # Issue #3.2.1: retry linkage
        }
        updates = []
        values = []
        for key, value in kwargs.items():
            if key not in allowed:
                continue
            updates.append(f"{key} = ?")
            values.append(value)
        if not updates:
            return False
        values.append(run_id)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE pipeline_runs SET {', '.join(updates)} WHERE run_id = ?",
                values,
            )
            return cursor.rowcount > 0

    def get_pipeline_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Return a pipeline_runs row as a dict, or None."""
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def list_pipeline_runs(
        self,
        status: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """List pipeline runs ordered by created_at DESC."""
        query = "SELECT * FROM pipeline_runs"
        params: list = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def sweep_zombie_runs(self, now: Optional[str] = None) -> int:
        """Sweep zombie pipeline runs whose daemons have died (Issue #754).

        Scans rows whose ``status`` is in the non-terminal set
        (``{'pending', 'running', 'pending_review'}``) and verifies
        each daemon's PID via :func:`~orchestration_engine.daemon.is_process_alive`.
        Rows whose recorded PID is no longer alive (or whose PID file is
        missing/empty/non-numeric AND whose ``pid`` column is ``NULL``)
        are transitioned to ``status='crashed'`` with a diagnostic
        ``error_message`` and a ``completed_at`` timestamp.

        Without this sweep, daemons killed by SIGKILL / OOM / host
        reboot leave their rows stuck in ``'running'`` forever — they
        consume slots in the :data:`ORCH_MAX_DAEMONS` backpressure cap
        (#839) and surface as 70-134+ hour "ghost" runs in
        ``orch status`` output.

        PID detection order per row:
          1. Use the ``pid`` column if non-NULL and > 0.
          2. Else read ``<output_dir>/.orch-daemon.pid`` (the path
             written by :func:`~orchestration_engine.daemon._write_pid_file`).
          3. Else mark the row as crashed with ``error_message`` containing
             ``'no PID recorded'``.

        The UPDATE uses ``WHERE status IN (...)`` guard so the sweep is
        idempotent AND safe against concurrent daemon state changes
        (mirrors the canonical pattern from :meth:`cancel_pipeline_run`).

        Terminal-status rows are NEVER scanned or modified, even if
        their recorded PID happens to be dead.

        **PID reuse caveat:** if the OS has recycled a dead daemon's
        PID for an unrelated live process, the liveness probe returns
        True and the row is left untouched. This is an accepted
        false-negative on detection (NOT a false-positive on action —
        the sweep never signals or kills any process; it uses
        ``os.kill(pid, 0)`` which is POSIX-defined as a permissions /
        existence check only). The blast radius is bounded by
        ``ORCH_MAX_DAEMONS`` and the residue is cleaned on the next
        engine restart. A fully race-free fix would require capturing
        the daemon's process_create_time at launch and comparing on
        sweep — out of scope for #754.

        Per-row exceptions are caught, logged at WARNING, and counted
        as "not swept" — the sweep ALWAYS returns a non-negative integer
        regardless of per-row failures.

        Args:
            now: Optional ISO-8601 timestamp string to use as
                ``completed_at`` for swept rows. Defaults to
                ``datetime.now().isoformat()`` when omitted (injectable
                for deterministic tests).

        Returns:
            Integer count of rows transitioned to ``'crashed'`` by
            this invocation. ``0`` on any top-level error or when no
            zombies are present.
        """
        # Lazy import: db.py is imported by daemon.py (transitively via
        # run_daemon), so a top-level `from .daemon import` would deadlock
        # at module load time.
        try:
            from .daemon import is_process_alive
        except Exception as exc:  # pragma: no cover — defensive
            logger.error("sweep_zombie_runs: cannot import is_process_alive: %s", exc)
            return 0

        if now is None:
            now = now_utc().isoformat()

        # Step 1 — snapshot the candidate rows under a read lock.
        try:
            with self._locked():
                conn = self.get_connection()
                cur = conn.execute(
                    "SELECT run_id, pid, output_dir, status FROM pipeline_runs "
                    "WHERE status IN ('pending', 'running', 'pending_review')"
                )
                rows = cur.fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("sweep_zombie_runs: SELECT failed: %s", exc)
            return 0

        swept = 0
        for row in rows:
            try:
                run_id = row["run_id"]
                pid_raw = row["pid"]
                output_dir = row["output_dir"]

                # Resolve effective PID + sweep reason.
                effective_pid: Optional[int] = None
                no_pid_reason: Optional[str] = None

                if pid_raw is not None and int(pid_raw) > 0:
                    effective_pid = int(pid_raw)
                else:
                    # Try the on-disk PID file written by daemon._write_pid_file.
                    pid_file = Path(output_dir) / ".orch-daemon.pid"
                    try:
                        text = pid_file.read_text().strip()
                    except (FileNotFoundError, OSError):
                        text = ""

                    if not text:
                        no_pid_reason = "no PID recorded (pid column NULL and PID file missing/empty)"
                    else:
                        try:
                            parsed = int(text)
                            if parsed > 0:
                                effective_pid = parsed
                            else:
                                no_pid_reason = "no PID recorded (PID file contains non-positive value)"
                        except ValueError:
                            no_pid_reason = "no PID recorded (PID file contains non-numeric value)"

                # Decide whether to sweep this row.
                if no_pid_reason is not None:
                    # No usable PID anywhere — sweep with explicit reason.
                    error_message = (
                        "daemon process exited without updating status: "
                        + no_pid_reason
                    )
                    self._mark_crashed(run_id, error_message, now)
                    swept += 1
                    continue

                # We have an effective PID — check liveness.
                if is_process_alive(effective_pid):
                    continue  # live daemon, leave row alone

                # Dead PID — sweep.
                error_message = (
                    f"daemon process exited without updating status "
                    f"(pid {effective_pid})"
                )
                if self._mark_crashed(run_id, error_message, now):
                    swept += 1
            except Exception as exc:
                # Per-row containment — log and move on.
                run_id_str = (
                    row["run_id"] if hasattr(row, "__getitem__") else "<unknown>"
                )
                logger.warning(
                    "sweep_zombie_runs: per-row error on run_id=%s: %s",
                    run_id_str, exc,
                )
                continue

        return swept

    def _mark_crashed(self, run_id: str, error_message: str, now: str) -> bool:
        """Atomically transition a non-terminal row to status='crashed'.

        Uses the canonical ``WHERE status IN (...)`` idempotency guard
        from :meth:`cancel_pipeline_run` so a concurrent daemon that
        races to ``'success'`` between our SELECT and our UPDATE wins
        the race (our UPDATE matches zero rows and we return False).

        Returns ``True`` iff exactly one row was updated.
        """
        try:
            with self.transaction() as conn:
                cur = conn.execute(
                    """
                    UPDATE pipeline_runs
                    SET status = 'crashed',
                        error_message = ?,
                        completed_at = ?
                    WHERE run_id = ?
                      AND status IN ('pending', 'running', 'pending_review')
                    """,
                    (error_message, now, run_id),
                )
                return cur.rowcount > 0
        except sqlite3.OperationalError as exc:
            logger.warning(
                "_mark_crashed: UPDATE failed for run_id=%s: %s",
                run_id, exc,
            )
            return False

    def count_active_pipeline_runs(self) -> int:
        """Count pipeline_runs in non-terminal states (Issue #839).

        Active = ``status`` in {``"pending"``, ``"running"``,
        ``"pending_review"``}. Used by the API launch path to enforce
        a backpressure cap (``ORCH_MAX_DAEMONS``, default 8) before
        spawning another daemon process. Without backpressure,
        unbounded concurrent daemons trip SQLite WAL contention
        (``SQLITE_BUSY``) and manifest as zombie runs (#754).

        **Side effect (#754):** invokes :meth:`sweep_zombie_runs` before
        counting so dead-daemon rows are transitioned to ``'crashed'``
        and excluded from the returned count. This keeps the
        backpressure cap accurate even when daemons have died without
        updating their status (the original zombie-run bug).

        Returns:
            Integer count of active runs. Returns 0 on any
            ``OperationalError`` (defensive — a backpressure check
            should never raise from a launch-path code path).
        """
        # Sweep first (#754) so zombies don't count against the cap.
        # Sweep failures are non-fatal: per-row exceptions are contained
        # inside sweep_zombie_runs, and top-level errors return 0 (no rows
        # swept) without raising — the count below proceeds either way.
        try:
            self.sweep_zombie_runs()
        except Exception as exc:  # pragma: no cover — sweep is defensive
            logger.warning(
                "count_active_pipeline_runs: sweep raised unexpectedly: %s", exc,
            )

        try:
            with self._locked():
                conn = self.get_connection()
                cur = conn.execute(
                    "SELECT COUNT(*) AS n FROM pipeline_runs "
                    "WHERE status IN ('pending', 'running', 'pending_review')"
                )
                row = cur.fetchone()
                return int(row["n"]) if row else 0
        except sqlite3.OperationalError:
            return 0

    def list_pipeline_runs_filtered(
        self,
        status: Optional[str] = None,
        template_id: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List pipeline runs with filtering and pagination support.

        Extends ``list_pipeline_runs`` with ``offset`` and ``template_id``
        parameters for use by the REST API (Issue #257).

        Args:
            status: Optional status filter (e.g. ``'running'``, ``'success'``).
            template_id: Optional template_id filter.
            limit: Maximum number of rows to return.
            offset: Number of rows to skip (for pagination).

        Returns:
            List of pipeline run dicts ordered by ``created_at DESC``.
        """
        query = "SELECT * FROM pipeline_runs WHERE 1=1"
        params: list = []

        if status:
            query += " AND status = ?"
            params.append(status)

        if template_id:
            query += " AND template_id = ?"
            params.append(template_id)

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

        return [self._row_to_dict(row) for row in rows]

    def list_pipeline_run_children(self, parent_run_id: str) -> List[Dict[str, Any]]:
        """Return all child pipeline runs for a given parent run.

        Queries ``pipeline_runs WHERE parent_run_id = ?`` ordered by
        ``created_at ASC`` so callers see children in spawn order.

        Args:
            parent_run_id: The run ID of the parent pipeline run.

        Returns:
            List of pipeline run dicts (same shape as
            :meth:`list_pipeline_runs`) ordered by ``created_at ASC``.
            Returns an empty list when no children exist.
        """  # Issue #330.3: children REST API
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT * FROM pipeline_runs WHERE parent_run_id = ? ORDER BY created_at ASC",
                (parent_run_id,),
            )
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def count_retries_for_run(self, original_run_id: str) -> int:
        """Return the number of retry runs spawned for *original_run_id*.

        Counts all rows in ``pipeline_runs`` where ``retry_of_run_id`` matches
        the given *original_run_id*, regardless of their current status.

        Args:
            original_run_id: The run ID of the first-attempt (original) run.

        Returns:
            Integer count of retry runs.  Returns ``0`` when no retries have
            been spawned yet or when *original_run_id* does not exist.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT COUNT(*) FROM pipeline_runs WHERE retry_of_run_id = ?",
                (original_run_id,),
            )
            row = cursor.fetchone()
        return row[0] if row else 0

    def count_pipeline_runs(
        self,
        status: Optional[str] = None,
        template_id: Optional[str] = None,
    ) -> int:
        """Return the total count of pipeline runs matching the given filters.

        Used by the REST API to return pagination metadata (Issue #257).

        Args:
            status: Optional status filter.
            template_id: Optional template_id filter.

        Returns:
            Integer row count.
        """
        query = "SELECT COUNT(*) FROM pipeline_runs WHERE 1=1"
        params: list = []

        if status:
            query += " AND status = ?"
            params.append(status)

        if template_id:
            query += " AND template_id = ?"
            params.append(template_id)

        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(query, params)
            row = cursor.fetchone()

        return row[0] if row else 0

    def cancel_pipeline_run(self, run_id: str) -> bool:
        """Cancel a pipeline run by sending SIGTERM to its daemon process.

        Sends ``SIGTERM`` to the daemon PID (if any) and unconditionally
        updates the run status to ``'cancelled'`` in the DB.

        Only runs in non-terminal states (``pending``, ``running``) are
        affected.  Runs already in a terminal state are left unchanged and
        this method returns ``False``.

        Args:
            run_id: The run identifier to cancel.

        Returns:
            ``True`` if the run was cancelled, ``False`` if the run was
            already in a terminal state or not found.
        """
        import os as _os
        import signal as _signal

        terminal_states = TERMINAL_STATUSES

        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT status, pid FROM pipeline_runs WHERE run_id = ?",
                (run_id,),
            )
            row = cursor.fetchone()

        if row is None:
            return False

        current_status = row["status"] if hasattr(row, "__getitem__") else row[0]
        pid = row["pid"] if hasattr(row, "__getitem__") else row[1]

        if current_status in terminal_states:
            return False

        # NOTE: TOCTOU — the status check above and the SIGTERM below are outside
        # a single DB transaction.  A concurrent caller could cancel the same run
        # between the SELECT and the UPDATE.  The UPDATE's WHERE guard
        # (status NOT IN terminal_states) prevents double-updates to the DB, so
        # there is no data corruption.  The only risk is that SIGTERM is sent to
        # a recycled PID if the OS reuses the process ID in the window between
        # the SELECT and the kill(); this window is tiny and the kill is
        # best-effort, so the risk is acceptable for now.
        if pid:
            try:
                _os.kill(int(pid), _signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                # Process already gone or we lack permission — still mark cancelled
                pass

        # Update the DB row
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE pipeline_runs
                SET status = 'cancelled', completed_at = ?
                WHERE run_id = ?
                  AND status NOT IN ('success', 'failed', 'cancelled', 'crashed', 'scoring_failed', 'pending_review', 'rejected')
                """,
                (now_utc().isoformat(), run_id),
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Pipeline Run Events (Issue #258 — SSE live-progress streaming)
    # ------------------------------------------------------------------

    def insert_pipeline_run_event(
        self,
        run_id: str,
        event_type: str,
        phase_id: Optional[str] = None,
        tokens_consumed: Optional[int] = None,
        cost_usd: Optional[float] = None,
        state: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Insert a pipeline run event and return its auto-incremented id.

        Args:
            run_id: The pipeline run identifier.
            event_type: One of ``'phase_started'``, ``'phase_completed'``,
                or ``'status_changed'``.
            phase_id: Phase identifier (``None`` for run-level events).
            tokens_consumed: Token count from the phase result, if available.
            cost_usd: Cost in USD from the phase result, if available.
            state: Serialised ``TaskState`` value (e.g. ``'success'``,
                ``'failed'``), if available.
            metadata: Arbitrary JSON-serialisable dict stored as
                ``metadata_json``.  Defaults to ``{}``.

        Returns:
            The ``id`` of the newly inserted event row.
        """
        metadata_json = json.dumps(metadata or {})
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO pipeline_run_events
                    (run_id, event_type, phase_id, tokens_consumed,
                     cost_usd, state, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, event_type, phase_id, tokens_consumed,
                 cost_usd, state, metadata_json),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def list_pipeline_run_events(
        self,
        run_id: str,
        after_id: int = 0,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Return pipeline run events newer than *after_id* for a given run.

        Used by the SSE endpoint to page through events in a tail loop:
        the caller passes the ``id`` of the last received event so that
        only fresh rows are returned on each poll iteration.

        Args:
            run_id: Filter by pipeline run identifier.
            after_id: Return only rows with ``id > after_id``.  Pass ``0``
                (default) to retrieve all events from the beginning.
            limit: Maximum number of rows to return per call.

        Returns:
            List of event dicts ordered by ``id ASC``.  Each dict includes
            ``id``, ``run_id``, ``event_type``, ``phase_id``,
            ``tokens_consumed``, ``cost_usd``, ``state``,
            ``metadata_json`` (raw string), and ``created_at``.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT id, run_id, event_type, phase_id,
                       tokens_consumed, cost_usd, state,
                       metadata_json, created_at
                FROM pipeline_run_events
                WHERE run_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (run_id, after_id, limit),
            )
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Diagnosis Operations (Issue #3.1.1)
    # ------------------------------------------------------------------

    def insert_diagnosis(self, diagnosis_data: Dict[str, Any]) -> int:
        """Insert a DiagnosisResult record.

        Args:
            diagnosis_data: Dict with keys: run_id, failure_class, remediation,
                confidence, explanation, model_used, tokens_consumed.
                ``failure_class`` and ``remediation`` should be the .value of
                their respective enums (strings).

        Returns:
            The auto-incremented ``id`` of the inserted row.
        """
        with self.transaction() as conn:
            cursor = conn.execute("""
                INSERT INTO diagnosis_results
                    (run_id, failure_class, remediation, confidence,
                     explanation, model_used, tokens_consumed)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                diagnosis_data["run_id"],
                diagnosis_data["failure_class"],
                diagnosis_data["remediation"],
                diagnosis_data["confidence"],
                diagnosis_data.get("explanation"),
                diagnosis_data.get("model_used"),
                diagnosis_data.get("tokens_consumed", 0),
            ))
            return cursor.lastrowid

    def get_diagnosis_by_run_id(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Return the most recent diagnosis for a run, or None.

        If multiple diagnoses exist for a run (e.g. re-diagnoses after retry),
        the most recently created one is returned.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute("""
                SELECT * FROM diagnosis_results
                WHERE run_id = ?
                ORDER BY id DESC
                LIMIT 1
            """, (run_id,))
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def list_diagnoses(
        self,
        failure_class: Optional[str] = None,
        remediation: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List diagnosis results with optional filtering and pagination.

        Args:
            failure_class: Optional string value of FailureClass enum to filter by.
            remediation:   Optional string value of Remediation enum to filter by.
            limit:         Max rows to return (default 100).
            offset:        Rows to skip for pagination (default 0).

        Returns:
            List of diagnosis dicts ordered by ``id DESC`` (newest first).
        """
        query = "SELECT * FROM diagnosis_results WHERE 1=1"
        params: list = []

        if failure_class:
            query += " AND failure_class = ?"
            params.append(failure_class)

        if remediation:
            query += " AND remediation = ?"
            params.append(remediation)

        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    # --- Trigger CRUD Operations (Issue #329.1) ---

    def create_trigger(self, trigger_data: Dict[str, Any]) -> str:
        """Insert a new trigger configuration row.

        Args:
            trigger_data: A plain dict as returned by
                ``TriggerConfig.to_dict()``.  Must contain ``'id'`` and
                ``'template_id'``.  ``input_map`` and ``filters`` must be
                Python dict/list (not pre-serialised JSON strings) — this
                method performs the JSON serialisation.

        Returns:
            The trigger ``id``.

        Raises:
            sqlite3.IntegrityError: If a trigger with the same ``id`` already
                exists.
        """
        with self.transaction() as conn:
            conn.execute("""
                INSERT INTO triggers
                    (id, template_id, mode, secret, rate_limit, input_map, filters, created_at, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trigger_data["id"],
                trigger_data["template_id"],
                trigger_data.get("mode", "async"),
                trigger_data.get("secret"),
                trigger_data.get("rate_limit", 0),
                json.dumps(trigger_data.get("input_map") or {}),
                json.dumps(trigger_data.get("filters") or []),
                trigger_data.get("created_at") or now_utc().isoformat(),
                int(trigger_data.get("enabled", True)),
            ))
        return trigger_data["id"]

    def get_trigger(self, trigger_id: str) -> Optional[Dict[str, Any]]:
        """Return a trigger config row by id, or None if not found.

        Args:
            trigger_id: The trigger identifier to look up.

        Returns:
            A dict with all trigger fields (JSON columns parsed to Python
            objects), or ``None`` if no matching row exists.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT * FROM triggers WHERE id = ?", (trigger_id,)
            )
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def list_triggers(
        self,
        template_id: Optional[str] = None,
        mode: Optional[str] = None,
        enabled: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List trigger configs with optional filtering and pagination.

        Args:
            template_id: Filter by template id.
            mode: Filter by execution mode (``'sync'``, ``'async'``,
                ``'fire_and_forget'``).
            enabled: When provided, filters to only enabled (``True``) or
                disabled (``False``) triggers.
            limit: Maximum rows to return (default 100).
            offset: Rows to skip for pagination (default 0).

        Returns:
            List of trigger dicts ordered by ``created_at DESC``.
        """
        query = "SELECT * FROM triggers WHERE 1=1"
        params: list = []

        if template_id:
            query += " AND template_id = ?"
            params.append(template_id)

        if mode:
            query += " AND mode = ?"
            params.append(mode)

        if enabled is not None:
            query += " AND enabled = ?"
            params.append(int(enabled))

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()

        return [self._row_to_dict(row) for row in rows]

    def update_trigger(self, trigger_id: str, **kwargs) -> bool:
        """Update whitelisted fields on a trigger config row.

        ``updated_at`` is always refreshed when at least one valid field is
        supplied.  Unknown kwargs are silently ignored.

        Allowed kwargs: ``mode``, ``secret``, ``rate_limit``,
        ``input_map``, ``filters``.

        Args:
            trigger_id: The trigger identifier to update.
            **kwargs: Field name → new value pairs.

        Returns:
            ``True`` if a DB row was modified, ``False`` if the trigger was
            not found **or** no valid fields were supplied.

        Note:
            A return value of ``False`` does not distinguish "trigger not
            found" from "no valid kwargs".  Callers that need to distinguish
            these cases should call ``get_trigger`` first.
        """
        allowed = {"mode", "secret", "rate_limit", "input_map", "filters", "enabled"}
        updates = ["updated_at = ?"]
        values = [now_utc().isoformat()]

        for key, value in kwargs.items():
            if key not in allowed:
                continue
            if key in ("input_map", "filters"):
                updates.append(f"{key} = ?")
                values.append(json.dumps(value))
            elif key == "enabled":
                updates.append(f"{key} = ?")
                values.append(int(value))
            else:
                updates.append(f"{key} = ?")
                values.append(value)

        # Only updated_at — no valid fields were provided
        if len(updates) == 1:
            return False

        values.append(trigger_id)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE triggers SET {', '.join(updates)} WHERE id = ?",
                values,
            )
            return cursor.rowcount > 0

    def delete_trigger(self, trigger_id: str) -> bool:
        """Delete a trigger config by id.

        Args:
            trigger_id: The trigger identifier to delete.

        Returns:
            ``True`` if a row was deleted, ``False`` if no matching row
            was found.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM triggers WHERE id = ?", (trigger_id,)
            )
            return cursor.rowcount > 0

    def record_webhook_invocation(self, trigger_id: str) -> None:
        """Record a webhook invocation timestamp for rate-limit tracking.

        Args:
            trigger_id: The ID of the trigger that was invoked.
        """
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO webhook_invocations (trigger_id, invoked_at) VALUES (?, ?)",
                (trigger_id, now_utc().isoformat()),
            )

    def count_webhook_invocations_since(self, trigger_id: str, since_dt: datetime) -> int:
        """Count webhook invocations for a trigger since a given datetime.

        Used for per-trigger rate-limit enforcement.

        Args:
            trigger_id: The trigger identifier to count invocations for.
            since_dt: Datetime lower bound (inclusive).

        Returns:
            Number of invocation rows with ``invoked_at >= since_dt``.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT COUNT(*) FROM webhook_invocations "
                "WHERE trigger_id = ? AND invoked_at >= ?",
                (trigger_id, since_dt.isoformat()),
            )
            return cursor.fetchone()[0]

    # ------------------------------------------------------------------
    # Routing Decision Operations (Issue #331.3)
    # ------------------------------------------------------------------

    def insert_routing_decision(self, decision_data: dict) -> int:
        """Insert a routing decision record and return the auto-incremented id.

        Args:
            decision_data: Dict with keys:
                - run_id (str): The pipeline run identifier.
                - confidence_score (float): Composite confidence score in [0, 1].
                - tier_name (str): Matched routing tier name (e.g. "auto_merge").
                - action (str): Dispatched action (e.g. "auto_merge", "human_review").
                - justification (str, optional): Human-readable explanation.
                - signals_json (str): JSON-serialised signal dict.

        Returns:
            The ``id`` of the newly inserted row.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO routing_decisions
                    (run_id, confidence_score, tier_name, action, justification, signals_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_data["run_id"],
                    float(decision_data["confidence_score"]),
                    decision_data["tier_name"],
                    decision_data["action"],
                    decision_data.get("justification"),
                    decision_data.get("signals_json", "{}"),
                ),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def _migration_008_add_review_columns(self, conn: sqlite3.Connection) -> None:
        """Add review_reason, reviewed_at, reviewed_by columns to pipeline_runs (Issue #331.4).

        Idempotent: silently ignores errors if columns already exist.
        """
        for col in [
            ("review_reason", "TEXT DEFAULT NULL"),
            ("reviewed_at", "TIMESTAMP DEFAULT NULL"),
            ("reviewed_by", "TEXT DEFAULT NULL"),
        ]:
            try:
                conn.execute(f"ALTER TABLE pipeline_runs ADD COLUMN {col[0]} {col[1]}")
            except Exception:
                pass  # column already exists

    def _migration_009_add_diagnosis_tables(self, conn: sqlite3.Connection) -> None:
        """Add diagnosis_results table (Issue #3.1.1).

        Idempotent: uses CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS diagnosis_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                failure_class TEXT NOT NULL,
                remediation TEXT NOT NULL,
                confidence REAL NOT NULL,
                explanation TEXT,
                model_used TEXT,
                tokens_consumed INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(run_id) REFERENCES pipeline_runs(run_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_diagnosis_results_run_id
            ON diagnosis_results(run_id)
        """)

    def _migration_010_add_failure_patterns_table(self, conn: sqlite3.Connection) -> None:
        """Add failure_patterns table for systemic failure detection (Issue #3.1.3).

        Idempotent: uses CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS.
        Safe to run on both fresh and existing databases.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS failure_patterns (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_hash     TEXT NOT NULL,
                template_id      TEXT NOT NULL,
                failure_class    TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                is_systemic      INTEGER NOT NULL DEFAULT 0,
                first_seen_at    TEXT NOT NULL,
                last_seen_at     TEXT NOT NULL,
                UNIQUE(pattern_hash, template_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_failure_patterns_template
            ON failure_patterns (template_id, last_seen_at)
        """)

    def _migration_011_add_retry_columns(self, conn: sqlite3.Connection) -> None:
        """Add retry linkage columns to pipeline_runs (Issue #3.2.1).

        Adds two nullable columns:

        * ``retry_of_run_id`` — foreign key reference to the original run that
          this run is retrying.  ``NULL`` for first-attempt runs.
        * ``retry_strategy`` — string value of the :class:`RetryStrategy` enum
          applied when this retry run was launched.  ``NULL`` for first attempts.

        Also creates an index on ``retry_of_run_id`` so that all retries for a
        given original run can be retrieved efficiently.

        Idempotent: uses ``ALTER TABLE … ADD COLUMN IF NOT EXISTS``-equivalent
        guard and ``CREATE INDEX IF NOT EXISTS``.
        Safe to run on both fresh and existing databases.
        """
        # SQLite does not support IF NOT EXISTS for ALTER TABLE ADD COLUMN.
        # We guard each column addition by checking PRAGMA table_info first.
        existing_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(pipeline_runs)").fetchall()
        }
        if "retry_of_run_id" not in existing_cols:
            conn.execute(
                "ALTER TABLE pipeline_runs ADD COLUMN retry_of_run_id TEXT DEFAULT NULL"
            )
        if "retry_strategy" not in existing_cols:
            conn.execute(
                "ALTER TABLE pipeline_runs ADD COLUMN retry_strategy TEXT DEFAULT NULL"
            )
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pipeline_runs_retry_of
            ON pipeline_runs (retry_of_run_id)
        """)

    def _migration_012_add_regressions_table(self, conn: sqlite3.Connection) -> None:
        """Add regressions table for regression event tracking (Issue #3.3a.1).

        Idempotent: uses CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS.
        Safe to run on both fresh and existing databases.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS regressions (
                id                TEXT PRIMARY KEY,
                commit_sha        TEXT NOT NULL,
                ci_run_url        TEXT NOT NULL,
                failure_type      TEXT NOT NULL,
                affected_files    TEXT NOT NULL DEFAULT '[]',
                diagnosis         TEXT,
                fix_run_id        TEXT,
                status            TEXT NOT NULL DEFAULT 'detected',
                fix_attempt_count INTEGER NOT NULL DEFAULT 0,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_regressions_status_created
            ON regressions(status, created_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_regressions_commit_sha
            ON regressions(commit_sha)
        """)

    def _migration_013_add_ci_green_shas_table(self, conn: sqlite3.Connection) -> None:
        """Add ci_green_shas table for last-known-green SHA tracking (Issue #3.3a.3).

        Idempotent: uses CREATE TABLE IF NOT EXISTS.
        Safe to run on both fresh and existing databases.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ci_green_shas (
                repo_slug  TEXT PRIMARY KEY,
                sha        TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

    def _migration_014_add_review_outcomes_table(self, conn: sqlite3.Connection) -> None:
        """Add review_outcomes table for durable review result storage (Issue #4.1.2).

        Idempotent: uses CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS.
        Safe to run on both fresh and existing databases.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_outcomes (
                review_id      TEXT PRIMARY KEY,
                run_id         TEXT NOT NULL,
                phase_id       TEXT NOT NULL,
                reviewer_model TEXT,
                verdict        TEXT,
                issues_found   TEXT NOT NULL DEFAULT '[]',
                fix_verified   INTEGER NOT NULL DEFAULT 0,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(run_id) REFERENCES pipeline_runs(run_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_review_outcomes_run_id
            ON review_outcomes(run_id, created_at)
        """)

    def _migration_015_add_reviewer_calibration_table(self, conn: sqlite3.Connection) -> None:
        """Add reviewer_calibration table for longitudinal accuracy tracking (Issue #4.1.5).

        Idempotent: uses CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS.
        Safe to run on both fresh and existing databases.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reviewer_calibration (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                reviewer_model              TEXT NOT NULL,
                total_reviews               INTEGER NOT NULL DEFAULT 0,
                approve_count               INTEGER NOT NULL DEFAULT 0,
                request_changes_count       INTEGER NOT NULL DEFAULT 0,
                approve_held_up_count       INTEGER NOT NULL DEFAULT 0,
                request_changes_valid_count INTEGER NOT NULL DEFAULT 0,
                approve_accuracy            REAL,
                request_changes_accuracy    REAL,
                overall_accuracy            REAL,
                computed_at                 TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                aggregation_window          TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_reviewer_calibration_model
            ON reviewer_calibration(reviewer_model, computed_at DESC)
        """)

    def _create_table_trust_profiles(self, conn: sqlite3.Connection) -> None:
        """Create trust_profiles table for per-(repo, template, task_type) trust state (Issue #4.2.1).

        Called from _initialize_database so fresh databases get the table
        without requiring a migration run.  Idempotent via
        ``CREATE TABLE IF NOT EXISTS``.

        Columns:
            id:                     Auto-increment primary key.
            repo:                   Git repository slug (e.g. "owner/repo").
            template_id:            Pipeline template identifier.
            task_type:              Task type string (e.g. "bugfix", "feature").
            auto_merge_threshold:   Confidence score required for auto-merge.
            human_review_threshold: Confidence score required to skip human review.
            trust_score:            Current trust score in [0.0, 1.0].
            total_runs:             Total pipeline runs attributed to this profile.
            successful_merges:      Runs auto-merged without revert.
            regressions:            Regressions detected after auto-merge.
            reverted_prs:           PRs reverted after auto-merge.
            last_run_at:            UTC ISO-8601 timestamp of the most-recent run.
            created_at:             Row creation timestamp.
            updated_at:             Last-update timestamp.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trust_profiles (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                repo                   TEXT    NOT NULL,
                template_id            TEXT    NOT NULL,
                task_type              TEXT    NOT NULL,
                auto_merge_threshold   REAL    NOT NULL DEFAULT 0.85,
                human_review_threshold REAL    NOT NULL DEFAULT 0.70,
                trust_score            REAL    NOT NULL DEFAULT 0.5,
                total_runs             INTEGER NOT NULL DEFAULT 0,
                successful_merges      INTEGER NOT NULL DEFAULT 0,
                regressions            INTEGER NOT NULL DEFAULT 0,
                reverted_prs           INTEGER NOT NULL DEFAULT 0,
                last_run_at            TEXT,
                created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(repo, template_id, task_type)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_trust_profiles_repo_template
            ON trust_profiles(repo, template_id)
        """)

    def _create_table_trust_adjustments(self, conn: sqlite3.Connection) -> None:
        """Create trust_adjustments table for trust-score history (Issue #4.2.1).

        Called from _initialize_database so fresh databases get the table
        without requiring a migration run.  Idempotent via
        ``CREATE TABLE IF NOT EXISTS``.

        Columns:
            id:             Auto-increment primary key.
            profile_id:     Foreign key to trust_profiles.id.
            delta:          Score change applied (positive = increase,
                            negative = decrease).
            reason:         Human-readable reason string (e.g. "successful_merge",
                            "regression_detected", "pr_reverted").
            run_id:         Optional pipeline run_id that triggered this adjustment.
            score_before:   Trust score before the adjustment.
            score_after:    Trust score after the adjustment.
            created_at:     UTC timestamp of this event.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trust_adjustments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id   INTEGER NOT NULL,
                delta        REAL    NOT NULL,
                reason       TEXT    NOT NULL,
                run_id       TEXT,
                score_before REAL    NOT NULL,
                score_after  REAL    NOT NULL,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(profile_id) REFERENCES trust_profiles(id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_trust_adjustments_profile_id
            ON trust_adjustments(profile_id, created_at DESC)
        """)

    def _migration_016_add_trust_tables(self, conn: sqlite3.Connection) -> None:
        """Add trust_profiles and trust_adjustments tables (Issue #4.2.1).

        Idempotent: uses CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS.
        Safe to run on both fresh and existing databases.
        """
        self._create_table_trust_profiles(conn)
        self._create_table_trust_adjustments(conn)

    def _create_table_issue_pipeline_map(self, conn: sqlite3.Connection) -> None:
        """Create issue_pipeline_map table for LLM-based issue classification (Issue #5.1.1).

        Called from ``_initialize_database`` so fresh databases get the table
        without requiring a migration run.  Idempotent via
        ``CREATE TABLE IF NOT EXISTS``.

        Columns:
            id:                     Auto-increment primary key.
            issue_number:           GitHub issue number.
            repo:                   Repository slug (e.g. ``"owner/repo"``).
            classification_type:    One of ``bug``, ``feature``, ``docs``,
                                    ``refactor``, ``research``, ``content``.
            confidence:             LLM confidence score in ``[0.0, 1.0]``.
            template_id:            Recommended pipeline template identifier.
            run_id:                 Optional pipeline run_id linked after launch.
            status:                 Lifecycle status (default ``'classified'``).
            created_at:             UTC timestamp when row was created.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS issue_pipeline_map (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_number      INTEGER NOT NULL,
                repo              TEXT NOT NULL,
                classification_type TEXT NOT NULL,
                confidence        REAL NOT NULL,
                template_id       TEXT,
                run_id            TEXT,
                status            TEXT NOT NULL DEFAULT 'classified',
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_issue_pipeline_map_issue_repo
            ON issue_pipeline_map(issue_number, repo)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_issue_pipeline_map_repo_created
            ON issue_pipeline_map(repo, created_at)
        """)

    def _migration_017_add_issue_pipeline_map(self, conn: sqlite3.Connection) -> None:
        """Add issue_pipeline_map table for issue classification (Issue #5.1.1).

        Idempotent: uses CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS.
        Safe to run on both fresh and existing databases.
        """
        self._create_table_issue_pipeline_map(conn)

    def _create_table_cost_tracking(self, conn: sqlite3.Connection) -> None:
        """Create cost_tracking table for per-phase cost recording (Issue #5.2.1).

        Called from ``_initialize_database`` so fresh databases get the table
        without requiring a migration run.  Idempotent via
        ``CREATE TABLE IF NOT EXISTS``.

        Columns:
            id:            Auto-increment primary key.
            run_id:        Foreign key to ``pipeline_runs.run_id``.
            phase_id:      Phase identifier within the run (e.g. ``"spec"``).
            model:         Model identifier used for the phase.
            input_tokens:  Number of input/prompt tokens consumed.
            output_tokens: Number of output/completion tokens generated.
            cost_usd:      Computed USD cost for this phase execution.
            created_at:    UTC timestamp when the record was inserted.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cost_tracking (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id        TEXT NOT NULL,
                phase_id      TEXT NOT NULL,
                model         TEXT NOT NULL,
                input_tokens  INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd      REAL NOT NULL DEFAULT 0.0,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(run_id) REFERENCES pipeline_runs(run_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_cost_tracking_run_id
            ON cost_tracking(run_id, created_at)
        """)

    def _migration_018_add_cost_tracking_table(self, conn: sqlite3.Connection) -> None:
        """Add cost_tracking table for per-phase cost recording (Issue #5.2.1).

        Idempotent: uses CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS.
        Safe to run on both fresh and existing databases.
        """
        self._create_table_cost_tracking(conn)

    def _migration_019_add_parent_run_id_index(self, conn: sqlite3.Connection) -> None:
        """Add index on pipeline_runs(parent_run_id) for chain traversal (Issue #508).

        Idempotent: uses CREATE INDEX IF NOT EXISTS.
        """
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pipeline_runs_parent_run_id "
            "ON pipeline_runs(parent_run_id)"
        )

    # ------------------------------------------------------------------
    # Sprint chain state table (Issue #514)
    # ------------------------------------------------------------------

    def _create_table_sprint_chain_state(self, conn: sqlite3.Connection) -> None:
        """Create sprint_chain_state table for post-merge chain automation (Issue #514).

        Called from ``_initialize_database`` so fresh databases get the table
        without requiring a migration run.  Idempotent via
        ``CREATE TABLE IF NOT EXISTS``.

        Columns:
            id:            Auto-increment primary key.
            repo:          Repository slug (e.g. ``"owner/repo"``).
            issue_number:  GitHub issue number.
            status:        Processing status: ``"processed"`` or ``"paused"``.
            run_id:        Pipeline run_id that triggered the processing.
            score:         Confidence score at the time of processing.
            processed_at:  UTC timestamp when the record was inserted/updated.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sprint_chain_state (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                repo         TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                status       TEXT NOT NULL DEFAULT 'processed',
                run_id       TEXT,
                score        REAL,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(repo, issue_number)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sprint_chain_repo
            ON sprint_chain_state(repo, processed_at)
        """)

    def _migration_020_add_sprint_chain_state_table(
        self, conn: sqlite3.Connection
    ) -> None:
        """Add sprint_chain_state table for post-merge chain automation (Issue #514).

        Idempotent: uses CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS.
        Safe to run on both fresh and existing databases.
        """
        self._create_table_sprint_chain_state(conn)

    def _create_table_admin_audit_log(self, conn: sqlite3.Connection) -> None:
        """Append-only audit log for admin-state mutations (Issue #838).

        Records every mutation made via the admin API (PUT
        /api/v1/admin/feature-flags and any other admin write endpoints).
        Each row captures the before/after JSON, the timestamp, the action
        kind, and the OS-level process id of the FastAPI worker that
        served the request (best-effort attribution — the engine has no
        per-user auth today, so source_pid is the most we can record).

        Schema is intentionally narrow + append-only — no UPDATE / DELETE
        from application code. Operators querying for "who changed
        admin.json on day X" use this table.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                source_pid INTEGER,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_admin_audit_log_created_at
            ON admin_audit_log(created_at DESC)
        """)

    def append_admin_audit(
        self,
        action: str,
        target: str,
        before: Optional[Dict[str, Any]] = None,
        after: Optional[Dict[str, Any]] = None,
        source_pid: Optional[int] = None,
    ) -> int:
        """Append a row to ``admin_audit_log``. Returns the new row id.

        Args:
            action: Short verb describing what changed (e.g.
                ``"update_feature_flags"``, ``"reset_admin_state"``).
            target: Which surface was mutated (e.g.
                ``"feature_flags"``, ``"autonomy_level"``, ``"modes"``).
                Multiple targets per action are concatenated comma-separated.
            before: Pre-mutation value (dict or None when first write).
            after: Post-mutation value.
            source_pid: OS pid of the FastAPI worker process. Default
                ``os.getpid()`` if not supplied.
        """
        import json as _json
        import os as _os
        pid = source_pid if source_pid is not None else _os.getpid()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO admin_audit_log
                    (action, target, before_json, after_json, source_pid)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    action,
                    target,
                    _json.dumps(before) if before is not None else None,
                    _json.dumps(after) if after is not None else None,
                    pid,
                ),
            )
            return int(cur.lastrowid or 0)

    def list_admin_audit(
        self, limit: int = 100, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Return up to ``limit`` recent admin audit rows, newest first.

        Each row is a dict with the columns of ``admin_audit_log``;
        ``before_json``/``after_json`` are parsed back into dicts (or None).
        """
        import json as _json
        with self.transaction() as conn:
            cur = conn.execute(
                """
                SELECT id, action, target, before_json, after_json,
                       source_pid, created_at
                  FROM admin_audit_log
                 ORDER BY created_at DESC, id DESC
                 LIMIT ? OFFSET ?
                """,
                (int(limit), int(offset)),
            )
            rows: List[Dict[str, Any]] = []
            for r in cur.fetchall():
                before = _json.loads(r["before_json"]) if r["before_json"] else None
                after = _json.loads(r["after_json"]) if r["after_json"] else None
                # Normalise created_at — SQLite returns a datetime object
                # when PARSE_DECLTYPES is set (see get_connection), but the
                # API surface is JSON so we need a string. ``normalize_ts``
                # (from ``.timestamps``) handles datetime -> isoformat and
                # Z-suffixes naive UTC strings so JS clients don't
                # misinterpret them as local time. (#876)
                created_str = normalize_ts(r["created_at"])
                rows.append({
                    "id": r["id"],
                    "action": r["action"],
                    "target": r["target"],
                    "before": before,
                    "after": after,
                    "source_pid": r["source_pid"],
                    "created_at": created_str,
                })
            return rows

    def upsert_sprint_chain_state(
        self,
        repo: str,
        issue_number: int,
        status: str,
        run_id: Optional[str] = None,
        score: Optional[float] = None,
    ) -> None:
        """Insert or update a sprint_chain_state row for ``(repo, issue_number)``.

        Uses ``INSERT OR REPLACE`` for idempotent upsert; updates
        ``processed_at`` to the current timestamp on each call.

        Args:
            repo:         Repository slug.
            issue_number: GitHub issue number.
            status:       ``"processed"`` or ``"paused"``.
            run_id:       Pipeline run_id (optional).
            score:        Confidence score (optional).
        """
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sprint_chain_state
                    (repo, issue_number, status, run_id, score, processed_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(repo, issue_number)
                DO UPDATE SET
                    status       = excluded.status,
                    run_id       = excluded.run_id,
                    score        = excluded.score,
                    processed_at = CURRENT_TIMESTAMP
                """,
                (repo, issue_number, status, run_id, score),
            )

    def get_sprint_chain_state(
        self, repo: str, issue_number: int
    ) -> Optional[Dict[str, Any]]:
        """Return the sprint_chain_state row for ``(repo, issue_number)``, or ``None``.

        Args:
            repo:         Repository slug.
            issue_number: GitHub issue number.

        Returns:
            Row dict with keys ``id``, ``repo``, ``issue_number``, ``status``,
            ``run_id``, ``score``, ``processed_at``, or ``None`` if not found.
        """
        conn = self.get_connection()
        cursor = conn.execute(
            "SELECT * FROM sprint_chain_state WHERE repo = ? AND issue_number = ?",
            (repo, issue_number),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))

    def get_sprint_processed_issues(self, repo: str) -> List[int]:
        """Return issue numbers marked ``"processed"`` for the given repo.

        Args:
            repo: Repository slug.

        Returns:
            List of issue numbers ordered by ``processed_at`` ascending.
        """
        conn = self.get_connection()
        cursor = conn.execute(
            """
            SELECT issue_number FROM sprint_chain_state
            WHERE repo = ? AND status = 'processed'
            ORDER BY processed_at ASC
            """,
            (repo,),
        )
        return [row[0] for row in cursor.fetchall()]

    def get_sprint_chain_states(self, repo: str) -> List[Dict[str, Any]]:
        """Return all sprint_chain_state rows for the given repo.

        Args:
            repo: Repository slug.

        Returns:
            List of row dicts ordered by ``processed_at`` ascending.
        """
        conn = self.get_connection()
        cursor = conn.execute(
            """
            SELECT * FROM sprint_chain_state
            WHERE repo = ?
            ORDER BY processed_at ASC
            """,
            (repo,),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Chain query methods (Issue #508)
    # ------------------------------------------------------------------

    def get_full_chain(self, root_run_id: str) -> List[Dict[str, Any]]:
        """Return all runs in a chain starting from *root_run_id* (inclusive).

        Uses a recursive CTE to walk *down* the parent→child tree.  The root
        run is returned first (depth 0), then children ordered by created_at.

        Args:
            root_run_id: The run_id of the chain root.

        Returns:
            Ordered list of pipeline_run dicts (root first, then descendants).
        """
        query = """
            WITH RECURSIVE chain(run_id, depth) AS (
                SELECT run_id, 0 FROM pipeline_runs WHERE run_id = ?
                UNION ALL
                SELECT pr.run_id, chain.depth + 1
                FROM pipeline_runs pr
                JOIN chain ON pr.parent_run_id = chain.run_id
                WHERE chain.depth < 50
            )
            SELECT pr.*
            FROM pipeline_runs pr
            JOIN chain ON pr.run_id = chain.run_id
            ORDER BY chain.depth ASC, pr.created_at ASC
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(query, (root_run_id,))
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_active_chain_roots(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return all root runs that have at least one non-terminal descendant.

        A *root* is a run with no parent (parent_run_id IS NULL).  A chain is
        *active* when any run in the chain is not in TERMINAL_STATUSES.

        Args:
            limit: Optional maximum number of roots to return.

        Returns:
            List of root pipeline_run dicts, ordered by created_at DESC.
        """
        terminal_list = list(TERMINAL_STATUSES)
        placeholders = ",".join("?" * len(terminal_list))
        query = f"""
            WITH RECURSIVE chain(root_id, run_id) AS (
                SELECT run_id, run_id
                FROM pipeline_runs
                WHERE parent_run_id IS NULL
                UNION ALL
                SELECT chain.root_id, pr.run_id
                FROM pipeline_runs pr
                JOIN chain ON pr.parent_run_id = chain.run_id
            )
            SELECT DISTINCT pr.*
            FROM pipeline_runs pr
            WHERE pr.parent_run_id IS NULL
              AND EXISTS (
                  SELECT 1 FROM chain c
                  JOIN pipeline_runs pr2 ON c.run_id = pr2.run_id
                  WHERE c.root_id = pr.run_id
                    AND pr2.status NOT IN ({placeholders})
              )
            ORDER BY pr.created_at DESC
        """
        params: List[Any] = terminal_list
        if limit is not None:
            query += " LIMIT ?"
            params = terminal_list + [limit]
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Cost API query methods (Issue #5.2.3)
    # ------------------------------------------------------------------

    def get_cost_summary(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        group_by: str = "day",
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Return aggregated cost data grouped by day, template, or model.

        Args:
            start_date: Optional ISO date string ``YYYY-MM-DD`` (inclusive lower bound).
            end_date:   Optional ISO date string ``YYYY-MM-DD`` (inclusive upper bound).
            group_by:   One of ``"day"``, ``"template"``, or ``"model"``.
            limit:      Maximum number of rows to return.
            offset:     Number of rows to skip (pagination).

        Returns:
            List of dicts with aggregated cost statistics.  Each dict contains
            ``total_cost``, ``total_input_tokens``, ``total_output_tokens``,
            ``phase_count``, and a group key (``day``, ``template_id``, or
            ``model`` depending on ``group_by``).
        """
        params: List[Any] = []
        where_clauses: List[str] = []

        if start_date is not None:
            where_clauses.append("DATE(ct.created_at) >= ?")
            params.append(start_date)
        if end_date is not None:
            where_clauses.append("DATE(ct.created_at) <= ?")
            params.append(end_date)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        if group_by == "day":
            select_col = "DATE(ct.created_at) AS day"
            group_col = "DATE(ct.created_at)"
            order_sql = "ORDER BY day DESC"
            from_join = "FROM cost_tracking ct"
        elif group_by == "template":
            select_col = "pr.template_id"
            group_col = "pr.template_id"
            order_sql = "ORDER BY total_cost DESC"
            from_join = (
                "FROM cost_tracking ct "
                "JOIN pipeline_runs pr ON ct.run_id = pr.run_id"
            )
        else:  # group_by == "model"
            select_col = "ct.model"
            group_col = "ct.model"
            order_sql = "ORDER BY total_cost DESC"
            from_join = "FROM cost_tracking ct"

        sql = f"""
            SELECT
                {select_col},
                SUM(ct.cost_usd)      AS total_cost,
                SUM(ct.input_tokens)  AS total_input_tokens,
                SUM(ct.output_tokens) AS total_output_tokens,
                COUNT(*)              AS phase_count
            {from_join}
            {where_sql}
            GROUP BY {group_col}
            {order_sql}
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def count_cost_summary(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        group_by: str = "day",
    ) -> int:
        """Return the total number of groups for a cost summary query.

        Uses a subquery so the pagination metadata (``total``) can be
        computed without fetching all rows.

        Args:
            start_date: Optional ISO date string ``YYYY-MM-DD``.
            end_date:   Optional ISO date string ``YYYY-MM-DD``.
            group_by:   One of ``"day"``, ``"template"``, or ``"model"``.

        Returns:
            Integer count of distinct group values.
        """
        params: List[Any] = []
        where_clauses: List[str] = []

        if start_date is not None:
            where_clauses.append("DATE(ct.created_at) >= ?")
            params.append(start_date)
        if end_date is not None:
            where_clauses.append("DATE(ct.created_at) <= ?")
            params.append(end_date)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        if group_by == "day":
            group_col = "DATE(ct.created_at)"
            from_join = "FROM cost_tracking ct"
        elif group_by == "template":
            group_col = "pr.template_id"
            from_join = (
                "FROM cost_tracking ct "
                "JOIN pipeline_runs pr ON ct.run_id = pr.run_id"
            )
        else:  # group_by == "model"
            group_col = "ct.model"
            from_join = "FROM cost_tracking ct"

        sql = f"""
            SELECT COUNT(*) FROM (
                SELECT {group_col}
                {from_join}
                {where_sql}
                GROUP BY {group_col}
            )
        """

        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(sql, params)
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    def get_run_costs(self, run_id: str) -> List[Dict[str, Any]]:
        """Return all per-phase cost records for a specific pipeline run.

        Args:
            run_id: The pipeline run identifier.

        Returns:
            List of dicts from the ``cost_tracking`` table, ordered by
            ``created_at ASC``.  Empty list when no records exist for the run.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT *
                FROM cost_tracking
                WHERE run_id = ?
                ORDER BY created_at ASC
                """,
                (run_id,),
            )
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Issue Pipeline Map CRUD (Issue #5.1.1)
    # ------------------------------------------------------------------

    def insert_issue_classification(self, data: Dict[str, Any]) -> int:
        """Insert a new issue classification row and return the primary key.

        Args:
            data: Dict with keys matching the ``issue_pipeline_map`` table.
                  Required keys: ``issue_number``, ``repo``,
                  ``classification_type``, ``confidence``.
                  Optional: ``template_id``, ``run_id``, ``status``,
                  ``created_at``.

        Returns:
            The integer ``id`` (primary key) of the newly inserted row.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO issue_pipeline_map
                    (issue_number, repo, classification_type, confidence,
                     template_id, run_id, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(data["issue_number"]),
                    data["repo"],
                    data["classification_type"],
                    float(data["confidence"]),
                    data.get("template_id"),
                    data.get("run_id"),
                    data.get("status", "classified"),
                    data.get("created_at"),
                ),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def get_issue_classification(
        self,
        issue_number: int,
        repo: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the most recent classification for an issue, or None.

        When the same issue has been classified multiple times (e.g. after a
        re-triage), the most recently inserted row is returned.

        Args:
            issue_number: GitHub issue number.
            repo:         Repository slug (e.g. ``"owner/repo"``).

        Returns:
            Dict with all ``issue_pipeline_map`` columns, or ``None`` when no
            matching row exists.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT * FROM issue_pipeline_map
                WHERE issue_number = ? AND repo = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (issue_number, repo),
            )
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def get_issue_classification_by_run_id(
        self,
        run_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the issue_pipeline_map row associated with *run_id*, or ``None``.

        Queries ``issue_pipeline_map`` by ``run_id`` and returns the most
        recently inserted matching row.  Used by the daemon's result-posting
        hook to resolve the triggering issue context when only the run ID is
        known.

        Args:
            run_id: Pipeline run ID (UUID string).

        Returns:
            Dict with all ``issue_pipeline_map`` columns, or ``None`` when no
            matching row exists.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT * FROM issue_pipeline_map
                WHERE run_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (run_id,),
            )
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def get_issue_pipeline_map_by_run_id(
        self,
        run_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the issue_pipeline_map row for *run_id* (Issue #5.1.4 public API).

        Thin wrapper around :meth:`get_issue_classification_by_run_id` providing
        the canonical name mandated by the spec.

        Args:
            run_id: Pipeline run ID (UUID string).

        Returns:
            Dict with all ``issue_pipeline_map`` columns, or ``None`` when no
            matching row exists.
        """
        return self.get_issue_classification_by_run_id(run_id)

    def list_issue_classifications(
        self,
        repo: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """List issue classification rows, newest first.

        Args:
            repo:  Optional repository slug filter.  When ``None`` all repos
                   are included.
            limit: Maximum rows to return (default ``100``).

        Returns:
            List of classification dicts ordered by ``id DESC``.
        """
        query = "SELECT * FROM issue_pipeline_map WHERE 1=1"
        params: List[Any] = []

        if repo is not None:
            query += " AND repo = ?"
            params.append(repo)

        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def update_issue_classification_status(
        self,
        row_id: int,
        status: str,
    ) -> bool:
        """Update the ``status`` of an issue classification row.

        Args:
            row_id: Integer primary key of the row to update.
            status: New status string (e.g. ``"launched"``, ``"skipped"``).

        Returns:
            ``True`` if a row was found and updated, ``False`` otherwise.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE issue_pipeline_map SET status = ? WHERE id = ?",
                (status, row_id),
            )
            return cursor.rowcount > 0

    def get_active_issue_run(
        self,
        issue_number: int,
        repo: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the first active ``issue_pipeline_map`` row for *(issue_number, repo)*.

        An "active" row is one whose linked ``pipeline_run.status`` is **not**
        in :data:`TERMINAL_STATUSES`.  Rows with ``run_id IS NULL`` (classified
        but not yet launched) are excluded — they do not constitute an active
        run and should not block deduplication.

        This is used by the GitHub issues webhook handler to prevent launching
        a duplicate pipeline when one is already running for the same issue.

        Args:
            issue_number: GitHub issue number.
            repo:         Repository slug (e.g. ``"owner/repo"``).

        Returns:
            Dict with all ``issue_pipeline_map`` columns for the first matching
            row, or ``None`` when no active run exists.
        """
        terminal_list = list(TERMINAL_STATUSES)
        placeholders = ",".join("?" * len(terminal_list))
        sql = f"""
            SELECT ipm.*
            FROM issue_pipeline_map ipm
            INNER JOIN pipeline_runs pr ON ipm.run_id = pr.run_id
            WHERE ipm.issue_number = ?
              AND ipm.repo = ?
              AND pr.status NOT IN ({placeholders})
            LIMIT 1
        """
        params: List[Any] = [issue_number, repo] + terminal_list

        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(sql, params)
            row = cursor.fetchone()

        return self._row_to_dict(row) if row else None

    # ------------------------------------------------------------------
    # Failure pattern CRUD (Issue #3.1.3)
    # ------------------------------------------------------------------

    def insert_or_update_failure_pattern(
        self,
        pattern_hash: str,
        template_id: str,
        failure_class: str,
        now_iso: str,
        systemic_threshold: int = 3,
        systemic_window_days: int = 7,
    ) -> Dict[str, Any]:
        """Upsert a failure pattern record and mark as systemic when threshold exceeded.

        Inserts a new row on the first occurrence of *pattern_hash* + *template_id*.
        On subsequent occurrences the ``occurrence_count`` and ``last_seen_at``
        columns are updated atomically.  The ``is_systemic`` flag is set to
        ``1`` when ``occurrence_count`` reaches *systemic_threshold* **and** the
        elapsed time between ``first_seen_at`` and *now_iso* does not exceed
        *systemic_window_days*.

        Args:
            pattern_hash:        SHA-256 hex digest of the normalised error message.
            template_id:         Template identifier the failure belongs to.
            failure_class:       String value of the :class:`FailureClass` enum.
            now_iso:             Current timestamp in ISO-8601 format.
            systemic_threshold:  Minimum occurrences to be considered systemic
                                 (default ``3``).
            systemic_window_days: Maximum age (in days) of the first occurrence
                                  for the pattern to still be considered systemic
                                  (default ``7``).

        Returns:
            The upserted row as a ``dict``, including the updated
            ``occurrence_count`` and ``is_systemic`` flag.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO failure_patterns
                    (pattern_hash, template_id, failure_class, occurrence_count,
                     is_systemic, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, 1, 0, ?, ?)
                ON CONFLICT(pattern_hash, template_id) DO UPDATE SET
                    occurrence_count = occurrence_count + 1,
                    last_seen_at = excluded.last_seen_at,
                    is_systemic = CASE
                        WHEN (occurrence_count + 1) >= ?
                             AND (julianday(excluded.last_seen_at)
                                  - julianday(first_seen_at)) <= ?
                        THEN 1
                        ELSE is_systemic
                    END
                """,
                (
                    pattern_hash, template_id, failure_class, now_iso, now_iso,
                    systemic_threshold, systemic_window_days,
                ),
            )
            cursor = conn.execute(
                "SELECT * FROM failure_patterns WHERE pattern_hash = ? AND template_id = ?",
                (pattern_hash, template_id),
            )
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else {}

    def get_failure_patterns(
        self,
        template_id: Optional[str] = None,
        systemic_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List failure patterns with optional filtering and pagination.

        Args:
            template_id:   If set, return only patterns for this template.
            systemic_only: If ``True``, return only systemic patterns
                           (``is_systemic = 1``).
            limit:         Maximum rows to return (default ``100``).
            offset:        Rows to skip for pagination (default ``0``).

        Returns:
            List of failure pattern dicts ordered by ``last_seen_at DESC``.
        """
        clauses: List[str] = []
        params: List[Any] = []

        if template_id is not None:
            clauses.append("template_id = ?")
            params.append(template_id)
        if systemic_only:
            clauses.append("is_systemic = 1")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])

        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                f"SELECT * FROM failure_patterns {where} "
                f"ORDER BY last_seen_at DESC LIMIT ? OFFSET ?",
                params,
            )
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_routing_decisions(self, run_id: str) -> List[Dict]:
        """Return all routing decision rows for a given pipeline run.

        Args:
            run_id: The pipeline run identifier to look up.

        Returns:
            List of routing decision dicts ordered by ``id ASC``.
            Returns an empty list when no decisions exist for the run.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT * FROM routing_decisions WHERE run_id = ? ORDER BY id ASC",
                (run_id,),
            )
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_routing_decision(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Return the most recent routing decision row for a given pipeline run.

        Convenience method that returns a single dict (the latest decision)
        rather than the full list returned by :meth:`get_routing_decisions`.

        Args:
            run_id: The pipeline run identifier to look up.

        Returns:
            The most recent routing decision dict (``signals_json`` parsed to a
            Python dict), or ``None`` when no decision has been recorded for
            the run.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT * FROM routing_decisions WHERE run_id = ? ORDER BY id DESC LIMIT 1",
                (run_id,),
            )
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    # ------------------------------------------------------------------
    # Review Queue Operations (Issue #331.4)
    # ------------------------------------------------------------------

    def list_pending_reviews(
        self,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Return pipeline runs with status='pending_review', enriched with routing decision data.

        Performs a LEFT JOIN against ``routing_decisions`` to include the
        latest confidence score and tier for each pending run.

        Args:
            limit: Maximum number of rows to return (default 20).
            offset: Number of rows to skip for pagination (default 0).

        Returns:
            List of dicts, each containing all pipeline_runs columns plus
            ``confidence_score`` and ``tier_name`` from the most recent
            routing decision (or ``None`` when no decision exists).
        """
        query = """
            SELECT pr.*,
                   rd.confidence_score,
                   rd.tier_name,
                   rd.action,
                   rd.justification
            FROM pipeline_runs pr
            LEFT JOIN (
                SELECT run_id,
                       confidence_score,
                       tier_name,
                       action,
                       justification,
                       MAX(id) AS max_id
                FROM routing_decisions
                GROUP BY run_id
            ) rd ON pr.run_id = rd.run_id
            WHERE pr.status = 'pending_review'
            ORDER BY pr.created_at DESC
            LIMIT ? OFFSET ?
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(query, (limit, offset))
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def count_pending_reviews(self) -> int:
        """Return the total count of pipeline runs with status='pending_review'.

        Returns:
            Integer count of pending review runs.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT COUNT(*) FROM pipeline_runs WHERE status = 'pending_review'"
            )
            row = cursor.fetchone()
        return row[0] if row else 0

    def approve_pipeline_run(
        self,
        run_id: str,
        reviewed_by: Optional[str] = None,
        note: Optional[str] = None,
    ) -> bool:
        """Approve a pending_review pipeline run, setting status to 'success'.

        Args:
            run_id: The pipeline run identifier to approve.
            reviewed_by: Optional identifier of the reviewer (user/system).
            note: Optional review note stored in review_reason.

        Returns:
            ``True`` if a row was updated, ``False`` if no matching
            pending_review run was found.
        """
        now = now_utc().isoformat()
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE pipeline_runs
                SET status = 'success',
                    review_reason = ?,
                    reviewed_at = ?,
                    reviewed_by = ?,
                    completed_at = COALESCE(completed_at, ?)
                WHERE run_id = ? AND status = 'pending_review'
                """,
                (note, now, reviewed_by, now, run_id),
            )
            return cursor.rowcount > 0

    def reject_pipeline_run(
        self,
        run_id: str,
        reason: str,
        reviewed_by: Optional[str] = None,
    ) -> bool:
        """Reject a pending_review pipeline run, setting status to 'rejected'.

        Args:
            run_id: The pipeline run identifier to reject.
            reason: Human-readable rejection reason stored in review_reason.
            reviewed_by: Optional identifier of the reviewer.

        Returns:
            ``True`` if a row was updated, ``False`` if no matching
            pending_review run was found.
        """
        now = now_utc().isoformat()
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE pipeline_runs
                SET status = 'rejected',
                    review_reason = ?,
                    reviewed_at = ?,
                    reviewed_by = ?,
                    completed_at = COALESCE(completed_at, ?)
                WHERE run_id = ? AND status = 'pending_review'
                """,
                (reason, now, reviewed_by, now, run_id),
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Regression CRUD (Issue #3.3a.1)
    # ------------------------------------------------------------------

    def insert_regression(self, regression_data: Dict[str, Any]) -> str:
        """Insert a new regression record.

        Args:
            regression_data: Dict matching the Regression dataclass fields.
                ``affected_files`` may be a Python list or an already
                JSON-serialised string (use ``Regression.to_dict()`` for
                the canonical format).

        Returns:
            The ``id`` of the inserted row.
        """
        import json as _json
        # Normalise affected_files: accept both list and pre-serialised string.
        af = regression_data.get("affected_files", [])
        if isinstance(af, str):
            af_serialised = af
        else:
            af_serialised = _json.dumps(af)

        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO regressions
                    (id, commit_sha, ci_run_url, failure_type, affected_files,
                     diagnosis, fix_run_id, status, fix_attempt_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    regression_data["id"],
                    regression_data["commit_sha"],
                    regression_data["ci_run_url"],
                    regression_data["failure_type"],
                    af_serialised,
                    regression_data.get("diagnosis"),
                    regression_data.get("fix_run_id"),
                    regression_data.get("status", "detected"),
                    regression_data.get("fix_attempt_count", 0),
                    regression_data.get("created_at"),
                ),
            )
        return regression_data["id"]

    def get_regression(self, regression_id: str) -> Optional[Dict[str, Any]]:
        """Return a regression record by id, or None if not found.

        Args:
            regression_id: UUID of the regression to retrieve.

        Returns:
            Dict with all regression fields (``affected_files`` deserialised
            to a Python list), or ``None`` if no matching row exists.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT * FROM regressions WHERE id = ?", (regression_id,)
            )
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def update_regression(self, regression_id: str, **kwargs: Any) -> bool:
        """Update fields on a regressions row.

        Only the following fields may be updated:
        ``status``, ``diagnosis``, ``fix_run_id``, ``fix_attempt_count``.
        Unrecognised kwargs are silently ignored.

        Args:
            regression_id: UUID of the row to update.
            **kwargs:      Field-value pairs to update.

        Returns:
            ``True`` if the row was found and at least one column updated,
            ``False`` if no matching row exists or no valid kwargs were given.
        """
        allowed = {"status", "diagnosis", "fix_run_id", "fix_attempt_count"}
        updates: List[str] = []
        values: List[Any] = []
        for key, value in kwargs.items():
            if key not in allowed:
                continue
            updates.append(f"{key} = ?")
            values.append(value)
        if not updates:
            return False
        values.append(regression_id)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE regressions SET {', '.join(updates)} WHERE id = ?",
                values,
            )
            return cursor.rowcount > 0

    def list_regressions(
        self,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List regression records, newest first.

        Args:
            status: Optional status filter (e.g. ``'detected'``, ``'fixed'``).
            limit:  Maximum rows to return (default ``100``).
            offset: Rows to skip for pagination (default ``0``).

        Returns:
            List of regression dicts ordered by ``created_at DESC``.
            ``affected_files`` is deserialised to a Python list.
        """
        query = "SELECT * FROM regressions WHERE 1=1"
        params: List[Any] = []
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # CI Green SHA Tracking (Issue #3.3a.3)
    # ------------------------------------------------------------------

    def store_green_sha(self, repo_slug: str, sha: str) -> None:
        """Upsert the last-known-green CI SHA for a repository.

        Uses an INSERT OR REPLACE so this is safe to call on first write
        (insert) and on every subsequent CI pass (update).

        Args:
            repo_slug: Repository identifier in ``owner/repo`` format.
            sha:       The green commit SHA to persist.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO ci_green_shas (repo_slug, sha, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(repo_slug) DO UPDATE SET
                    sha        = excluded.sha,
                    updated_at = excluded.updated_at
                """,
                (repo_slug, sha, now),
            )

    def get_last_green_sha(self, repo_slug: str) -> Optional[str]:
        """Return the most recent green CI SHA for a repository, or None.

        Args:
            repo_slug: Repository identifier in ``owner/repo`` format.

        Returns:
            The green SHA string, or ``None`` if no record exists yet.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT sha FROM ci_green_shas WHERE repo_slug = ?",
                (repo_slug,),
            )
            row = cursor.fetchone()
        return row["sha"] if row else None

    # ------------------------------------------------------------------
    # Review Outcome Operations (Issue #4.1.2)
    # ------------------------------------------------------------------

    def insert_review_outcome(self, data: Dict[str, Any]) -> int:
        """Insert a review outcome record and return the rowid.

        Args:
            data: Dict with keys matching the ``review_outcomes`` table columns:
                - ``review_id`` (str): UUID primary key.
                - ``run_id`` (str): Pipeline run identifier.
                - ``phase_id`` (str): Phase identifier (e.g. ``"review"``).
                - ``reviewer_model`` (str, optional): Model tier/name.
                - ``verdict`` (str, optional): ``"APPROVE"`` or ``"REQUEST_CHANGES"``.
                - ``issues_found`` (list): List of issue dicts — serialised to
                  JSON by this method.
                - ``fix_verified`` (bool, optional): Defaults to ``False``.
                - ``created_at`` (str, optional): ISO-8601 timestamp; defaults
                  to the current DB timestamp when omitted.

        Returns:
            The ``rowid`` of the newly inserted row (integer).
        """
        issues_json = json.dumps(data.get("issues_found", []))
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO review_outcomes
                    (review_id, run_id, phase_id, reviewer_model,
                     verdict, issues_found, fix_verified, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["review_id"],
                    data["run_id"],
                    data["phase_id"],
                    data.get("reviewer_model"),
                    data.get("verdict"),
                    issues_json,
                    int(bool(data.get("fix_verified") or False)),
                    data.get("created_at"),
                ),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def get_review_outcomes_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        """Return all review outcome rows for a given pipeline run.

        Rows are ordered by ``created_at ASC`` so the caller sees outcomes
        in chronological order (relevant when a run has multiple review
        phases).

        Args:
            run_id: The pipeline run identifier to look up.

        Returns:
            List of review outcome dicts (``issues_found`` deserialised to a
            Python list).  Returns an empty list when no outcomes exist.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT * FROM review_outcomes
                WHERE run_id = ?
                ORDER BY created_at ASC
                """,
                (run_id,),
            )
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_review_outcomes(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Return a paginated global listing of all review outcomes.

        Rows are ordered by ``created_at DESC`` (newest first).  Use
        ``limit`` and ``offset`` for cursor-based pagination.

        Args:
            limit:  Maximum number of rows to return (default ``50``).
            offset: Number of rows to skip for pagination (default ``0``).

        Returns:
            List of review outcome dicts ordered by ``created_at DESC``.
            ``issues_found`` is deserialised to a Python list.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT * FROM review_outcomes
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Reviewer Calibration Operations (Issue #4.1.5)
    # ------------------------------------------------------------------

    def insert_calibration_snapshot(self, data: Dict[str, Any]) -> int:
        """Insert a reviewer calibration snapshot and return the rowid.

        Args:
            data: Dict with keys matching the ``reviewer_calibration`` table
                  columns (as produced by
                  :meth:`~reviewer_calibration.CalibrationMetrics.to_dict`):

                  - ``reviewer_model`` (str): Model tier/name.
                  - ``total_reviews`` (int): Total outcomes observed.
                  - ``approve_count`` (int): Number of APPROVE verdicts.
                  - ``request_changes_count`` (int): Number of RC verdicts.
                  - ``approve_held_up_count`` (int): APPROVEs with no fix.
                  - ``request_changes_valid_count`` (int): Verified RCs.
                  - ``approve_accuracy`` (float | None): APPROVE accuracy.
                  - ``request_changes_accuracy`` (float | None): RC accuracy.
                  - ``overall_accuracy`` (float | None): Combined accuracy.
                  - ``computed_at`` (str, optional): ISO-8601 timestamp.
                  - ``aggregation_window`` (str, optional): Time window label.

        Returns:
            The ``rowid`` of the newly inserted row (integer).
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO reviewer_calibration
                    (reviewer_model, total_reviews, approve_count,
                     request_changes_count, approve_held_up_count,
                     request_changes_valid_count, approve_accuracy,
                     request_changes_accuracy, overall_accuracy,
                     computed_at, aggregation_window)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["reviewer_model"],
                    int(data.get("total_reviews", 0)),
                    int(data.get("approve_count", 0)),
                    int(data.get("request_changes_count", 0)),
                    int(data.get("approve_held_up_count", 0)),
                    int(data.get("request_changes_valid_count", 0)),
                    data.get("approve_accuracy"),
                    data.get("request_changes_accuracy"),
                    data.get("overall_accuracy"),
                    data.get("computed_at"),
                    data.get("aggregation_window"),
                ),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def get_calibration_for_model(
        self,
        reviewer_model: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the most recent calibration snapshot for a given model.

        Args:
            reviewer_model: The model name/tier to look up (e.g. ``"opus"``).

        Returns:
            A calibration snapshot dict (most recent by ``computed_at``), or
            ``None`` when no snapshots exist for the model.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT * FROM reviewer_calibration
                WHERE reviewer_model = ?
                ORDER BY computed_at DESC
                LIMIT 1
                """,
                (reviewer_model,),
            )
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def list_calibration_snapshots(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Return a paginated global listing of all calibration snapshots.

        Rows are ordered by ``computed_at DESC`` (newest first).  Use
        ``limit`` and ``offset`` for cursor-based pagination.

        Args:
            limit:  Maximum number of rows to return (default ``50``).
            offset: Number of rows to skip for pagination (default ``0``).

        Returns:
            List of calibration snapshot dicts ordered by ``computed_at DESC``.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT * FROM reviewer_calibration
                ORDER BY computed_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Trust Profile CRUD (Issue #4.2.1)
    # ------------------------------------------------------------------

    def upsert_trust_profile(self, profile_data: Dict[str, Any]) -> int:
        """Insert or update a trust profile row and return the row id.

        Uses an ``INSERT … ON CONFLICT(repo, template_id, task_type) DO UPDATE``
        strategy so this is safe to call on both first write (insert) and
        subsequent updates.

        On conflict all mutable columns are overwritten with the supplied
        values; ``created_at`` is left unchanged (set only at initial insert).

        Args:
            profile_data: Dict matching the ``TrustProfile`` dataclass fields.
                          Required keys: ``repo``, ``template_id``,
                          ``task_type``.  Optional keys default to their DB
                          column defaults when omitted.

        Returns:
            The integer ``id`` (primary key) of the inserted or updated row.
        """
        now = profile_data.get("updated_at") or datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO trust_profiles
                    (repo, template_id, task_type,
                     auto_merge_threshold, human_review_threshold,
                     trust_score, total_runs, successful_merges,
                     regressions, reverted_prs, last_run_at,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo, template_id, task_type) DO UPDATE SET
                    auto_merge_threshold   = excluded.auto_merge_threshold,
                    human_review_threshold = excluded.human_review_threshold,
                    trust_score            = excluded.trust_score,
                    total_runs             = excluded.total_runs,
                    successful_merges      = excluded.successful_merges,
                    regressions            = excluded.regressions,
                    reverted_prs           = excluded.reverted_prs,
                    last_run_at            = excluded.last_run_at,
                    updated_at             = excluded.updated_at
                """,
                (
                    profile_data["repo"],
                    profile_data["template_id"],
                    profile_data["task_type"],
                    float(profile_data.get("auto_merge_threshold", 0.85)),
                    float(profile_data.get("human_review_threshold", 0.70)),
                    float(profile_data.get("trust_score", 0.5)),
                    int(profile_data.get("total_runs", 0)),
                    int(profile_data.get("successful_merges", 0)),
                    int(profile_data.get("regressions", 0)),
                    int(profile_data.get("reverted_prs", 0)),
                    profile_data.get("last_run_at"),
                    profile_data.get("created_at") or now,
                    now,
                ),
            )
            # lastrowid works for both INSERT and the DO UPDATE path in SQLite ≥ 3.35
            rowid = cursor.lastrowid
            if rowid is None:
                # Fallback: fetch the id via the unique composite key
                row = conn.execute(
                    "SELECT id FROM trust_profiles WHERE repo=? AND template_id=? AND task_type=?",
                    (profile_data["repo"], profile_data["template_id"], profile_data["task_type"]),
                ).fetchone()
                rowid = row[0] if row else None
        return rowid  # type: ignore[return-value]

    def get_trust_profile(
        self,
        repo: str,
        template_id: str,
        task_type: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the trust profile for a (repo, template_id, task_type) triplet.

        Args:
            repo:        Git repository slug (e.g. ``"owner/repo"``).
            template_id: Pipeline template identifier.
            task_type:   Task type string (e.g. ``"bugfix"``).

        Returns:
            Dict with all ``trust_profiles`` columns, or ``None`` when no
            matching row exists.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT * FROM trust_profiles
                WHERE repo = ? AND template_id = ? AND task_type = ?
                """,
                (repo, template_id, task_type),
            )
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def insert_trust_adjustment(self, adjustment_data: Dict[str, Any]) -> int:
        """Insert a trust adjustment event and return the new row id.

        Args:
            adjustment_data: Dict matching the ``trust_adjustments`` table
                             columns.  Required keys: ``profile_id``,
                             ``delta``, ``reason``, ``score_before``,
                             ``score_after``.  Optional: ``run_id``,
                             ``created_at``.

        Returns:
            The ``id`` (integer primary key) of the newly inserted row.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO trust_adjustments
                    (profile_id, delta, reason, run_id, score_before, score_after, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(adjustment_data["profile_id"]),
                    float(adjustment_data["delta"]),
                    adjustment_data["reason"],
                    adjustment_data.get("run_id"),
                    float(adjustment_data["score_before"]),
                    float(adjustment_data["score_after"]),
                    adjustment_data.get("created_at") or datetime.now(timezone.utc).isoformat(),
                ),
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def list_trust_adjustments(
        self,
        profile_id: int,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Return trust adjustment events for a profile, newest first.

        Args:
            profile_id: Primary key of the parent ``trust_profiles`` row.
            limit:      Maximum rows to return (default ``100``).
            offset:     Rows to skip for pagination (default ``0``).

        Returns:
            List of adjustment dicts ordered by ``created_at DESC``.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                """
                SELECT * FROM trust_adjustments
                WHERE profile_id = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (profile_id, limit, offset),
            )
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_trust_profiles(self) -> List[Dict[str, Any]]:
        """Return all trust profile rows, ordered by id ASC.

        Returns:
            List of dicts, one per ``trust_profiles`` row.  Empty list when no
            profiles have been created yet.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT * FROM trust_profiles ORDER BY id ASC"
            )
            rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_trust_profile_by_id(self, profile_id: int) -> Optional[Dict[str, Any]]:
        """Return a single trust profile row by its integer primary key.

        Args:
            profile_id: Integer primary key of the ``trust_profiles`` row.

        Returns:
            Dict with all ``trust_profiles`` columns, or ``None`` when no row
            matches the given ``profile_id``.
        """
        with self._locked():
            conn = self.get_connection()
            cursor = conn.execute(
                "SELECT * FROM trust_profiles WHERE id = ?",
                (profile_id,),
            )
            row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def close(self) -> None:
        """Close database connections."""
        if hasattr(self._local, 'connection'):
            self._local.connection.close()
            delattr(self._local, 'connection')