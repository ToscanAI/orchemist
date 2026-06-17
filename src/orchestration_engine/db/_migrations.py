"""Schema-migration mixin for :class:`~orchestration_engine.db.Database`.

Extracted from the original ``db.py`` (EPIC #942, sub-issue 951b) WITHOUT
behavioural change. Holds ``_run_migrations`` plus the numbered
``_migration_001`` .. ``_migration_020`` steps. Method bodies are
byte-identical to the original; only the import depth of intra-package
references is adjusted. The migration steps call DDL helpers
(:class:`~orchestration_engine.db._schema.SchemaMixin`) and
connection/transaction helpers
(:class:`~orchestration_engine.db._core.CoreMixin`) via ``self``, resolved at
runtime through the MRO.
"""

# Trailing/blank-line whitespace and long lines below live inside triple-quoted
# SQL DDL / docstring string literals; ruff only offers --unsafe-fixes for the
# whitespace, and a line-level E501 noqa is inert inside a string literal.
# ruff: noqa: W291, W293, E501

import sqlite3


class MigrationsMixin:
    """Versioned schema migrations for :class:`Database`.

    Mixed into :class:`Database` (see :mod:`db.__init__`). ``_run_migrations``
    is called by ``_initialize_database``
    (:class:`~orchestration_engine.db._schema.SchemaMixin`) within the bootstrap
    transaction.
    """

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
            ("003_add_triggers_table", self._migration_003_add_triggers_table),  # Issue #329.1
            (
                "004_add_webhook_invocations",
                self._migration_004_add_webhook_invocations,
            ),  # Issue #329.2
            ("005_add_trigger_enabled", self._migration_005_add_trigger_enabled),  # Issue #329.2
            ("006_add_chain_columns", self._migration_006_add_chain_columns),  # Issue #330.1
            (
                "007_add_routing_decisions",
                self._migration_007_add_routing_decisions,
            ),  # Issue #331.3
            ("008_add_review_columns", self._migration_008_add_review_columns),  # Issue #331.4
            ("009_add_diagnosis_tables", self._migration_009_add_diagnosis_tables),  # Issue #3.1.1
            (
                "010_add_failure_patterns_table",
                self._migration_010_add_failure_patterns_table,
            ),  # Issue #3.1.3
            ("011_add_retry_columns", self._migration_011_add_retry_columns),  # Issue #3.2.1
            (
                "012_add_regressions_table",
                self._migration_012_add_regressions_table,
            ),  # Issue #3.3a.1
            (
                "013_add_ci_green_shas_table",
                self._migration_013_add_ci_green_shas_table,
            ),  # Issue #3.3a.3
            (
                "014_add_review_outcomes_table",
                self._migration_014_add_review_outcomes_table,
            ),  # Issue #4.1.2
            (
                "015_add_reviewer_calibration_table",
                self._migration_015_add_reviewer_calibration_table,
            ),  # Issue #4.1.5
            ("016_add_trust_tables", self._migration_016_add_trust_tables),  # Issue #4.2.1
            (
                "017_add_issue_pipeline_map",
                self._migration_017_add_issue_pipeline_map,
            ),  # Issue #5.1.1
            (
                "018_add_cost_tracking_table",
                self._migration_018_add_cost_tracking_table,
            ),  # Issue #5.2.1
            (
                "019_add_parent_run_id_index",
                self._migration_019_add_parent_run_id_index,
            ),  # Issue #508
            (
                "020_add_sprint_chain_state_table",
                self._migration_020_add_sprint_chain_state_table,
            ),  # Issue #514
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
            conn.execute("ALTER TABLE pipeline_runs ADD COLUMN scoring_status TEXT DEFAULT NULL")
        except sqlite3.OperationalError:
            pass  # column already exists

        try:
            conn.execute("ALTER TABLE pipeline_runs ADD COLUMN scoring_score REAL DEFAULT NULL")
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
            conn.execute("ALTER TABLE triggers ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
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
            conn.execute("ALTER TABLE pipeline_runs ADD COLUMN parent_run_id TEXT DEFAULT NULL")
        except sqlite3.OperationalError:
            pass  # column already exists

        try:
            conn.execute("ALTER TABLE pipeline_runs ADD COLUMN chain_depth INTEGER DEFAULT 0")
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
            except Exception:  # noqa: BLE001, PERF203
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
            row[1] for row in conn.execute("PRAGMA table_info(pipeline_runs)").fetchall()
        }
        if "retry_of_run_id" not in existing_cols:
            conn.execute("ALTER TABLE pipeline_runs ADD COLUMN retry_of_run_id TEXT DEFAULT NULL")
        if "retry_strategy" not in existing_cols:
            conn.execute("ALTER TABLE pipeline_runs ADD COLUMN retry_strategy TEXT DEFAULT NULL")
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

    def _migration_016_add_trust_tables(self, conn: sqlite3.Connection) -> None:
        """Add trust_profiles and trust_adjustments tables (Issue #4.2.1).

        Idempotent: uses CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS.
        Safe to run on both fresh and existing databases.
        """
        self._create_table_trust_profiles(conn)
        self._create_table_trust_adjustments(conn)

    def _migration_017_add_issue_pipeline_map(self, conn: sqlite3.Connection) -> None:
        """Add issue_pipeline_map table for issue classification (Issue #5.1.1).

        Idempotent: uses CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS.
        Safe to run on both fresh and existing databases.
        """
        self._create_table_issue_pipeline_map(conn)

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

    def _migration_020_add_sprint_chain_state_table(self, conn: sqlite3.Connection) -> None:
        """Add sprint_chain_state table for post-merge chain automation (Issue #514).

        Idempotent: uses CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS.
        Safe to run on both fresh and existing databases.
        """
        self._create_table_sprint_chain_state(conn)
