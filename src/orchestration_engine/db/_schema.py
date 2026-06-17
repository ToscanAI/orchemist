"""Schema / DDL mixin for :class:`~orchestration_engine.db.Database`.

Extracted from the original ``db.py`` (EPIC #942, sub-issue 951b) WITHOUT
behavioural change. Holds ``_initialize_database`` (the bootstrap entry point
``CoreMixin.__init__`` calls via the MRO), ``_create_tables``,
``_create_indexes`` and every ``_create_table*`` DDL helper. Method bodies are
byte-identical to the original; only the import depth of intra-package
references is adjusted. These methods reference connection/transaction helpers
(``self.transaction``) resolved at runtime via the MRO from
:class:`~orchestration_engine.db._core.CoreMixin`.
"""

# Trailing/blank-line whitespace and long lines below live inside triple-quoted
# SQL DDL / docstring string literals; ruff only offers --unsafe-fixes for the
# whitespace, and a line-level E501 noqa is inert inside a string literal.
# ruff: noqa: W291, W293, E501

import sqlite3


class SchemaMixin:
    """Table-creation and index DDL for :class:`Database`.

    Mixed into :class:`Database` (see :mod:`db.__init__`).
    ``_initialize_database`` is invoked by ``CoreMixin.__init__`` and resolved
    through the MRO. Migration application is delegated to ``_run_migrations``
    (:class:`~orchestration_engine.db._migrations.MigrationsMixin`).
    """

    def _initialize_database(self) -> None:
        """Initialize database schema with all tables and indexes."""
        with self.transaction() as conn:
            # Create tables
            self._create_tables(conn)
            self._create_tables_pipeline_run_events(conn)
            self._create_table_routing_decisions(conn)
            self._create_table_failure_patterns(conn)
            self._create_table_regressions(conn)  # Issue #3.3a.1
            self._create_table_ci_green_shas(conn)  # Issue #3.3a.3
            self._create_table_review_outcomes(conn)  # Issue #4.1.2
            self._create_table_reviewer_calibration(conn)  # Issue #4.1.5
            self._create_table_trust_profiles(conn)  # Issue #4.2.1
            self._create_table_trust_adjustments(conn)  # Issue #4.2.1
            self._create_table_issue_pipeline_map(conn)  # Issue #5.1.1
            self._create_table_cost_tracking(conn)  # Issue #5.2.1
            self._create_table_sprint_chain_state(conn)  # Issue #514
            self._create_table_admin_audit_log(conn)  # Issue #838
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
