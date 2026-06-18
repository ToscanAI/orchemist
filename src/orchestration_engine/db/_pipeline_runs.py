"""Pipeline-run domain mixin for :class:`~orchestration_engine.db.Database`.

Extracted from the original ``db.py`` (EPIC #942, sub-issue 951c) WITHOUT
behavioural change. Holds the ``pipeline_runs`` CRUD + query surface
(``insert_pipeline_run``, ``update_pipeline_run``, ``get_pipeline_run``,
``list_pipeline_runs`` / ``_filtered`` / ``_children``, ``count_*``,
``cancel_pipeline_run``), the zombie-sweep machinery (``sweep_zombie_runs`` /
``_mark_crashed`` / ``count_active_pipeline_runs``, Issue #754) and the
``pipeline_run_events`` writers/readers (Issue #258). Method bodies are
byte-identical to the original; only the import depth of intra-package
references is adjusted. Connection/transaction helpers (``self.transaction`` /
``self._locked`` / ``self.get_connection`` / ``self._row_to_dict``) resolve at
runtime via the MRO from :class:`~orchestration_engine.db._core.CoreMixin`.

The ``..daemon`` import in :meth:`PipelineRunsMixin.sweep_zombie_runs` stays
*lazy* (in-method): ``daemon`` imports ``db`` (run_daemon), so a module-level
``from ..daemon import is_process_alive`` here would form an import cycle at
load time. The dotted depth is unchanged by the move — both this module and
the former ``db.py`` sit one level under :mod:`orchestration_engine`, so
``from ..daemon import ...`` resolves identically.
"""

# Trailing/blank-line whitespace and long lines below live inside triple-quoted
# SQL DDL / docstring string literals; ruff only offers --unsafe-fixes for the
# whitespace, and a line-level E501 noqa is inert inside a string literal.
# ruff: noqa: W291, W293, E501

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..timestamps import now_utc
from ._consts import TERMINAL_STATUSES

logger = logging.getLogger(__name__)


class PipelineRunsMixin:
    """Pipeline-run CRUD/query, zombie-sweep, and run-event persistence.

    Mixed into :class:`Database` (see :mod:`db.__init__`). All connection and
    generic-query helpers (``transaction``, ``_locked``, ``get_connection``,
    ``_row_to_dict``) are resolved through the MRO from
    :class:`~orchestration_engine.db._core.CoreMixin`. The monkeypatch paths
    ``orchestration_engine.db.Database.insert_pipeline_run`` /
    ``update_pipeline_run`` / ``get_pipeline_run`` keep resolving through
    :class:`Database`'s MRO into this mixin.
    """

    # ------------------------------------------------------------------
    # Pipeline Run Operations (Issue #267 — async daemon)
    # ------------------------------------------------------------------

    def insert_pipeline_run(self, run_data: Dict[str, Any]) -> str:
        """Insert a new async pipeline run record."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO pipeline_runs (
                    run_id, template_path, template_id, input_json, mode,
                    output_dir, status, gateway_url, skip_scoring,
                    parent_run_id, chain_depth,
                    retry_of_run_id, retry_strategy
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    run_data["run_id"],
                    run_data["template_path"],
                    run_data["template_id"],
                    run_data["input_json"],
                    run_data["mode"],
                    run_data["output_dir"],
                    run_data.get("status", "pending"),
                    run_data.get("gateway_url"),
                    int(run_data.get("skip_scoring", 0)),
                    run_data.get("parent_run_id"),  # Issue #330.1: chaining parent
                    int(run_data.get("chain_depth", 0)),  # Issue #330.1: chaining depth
                    run_data.get("retry_of_run_id"),  # Issue #3.2.1: retry linkage
                    run_data.get("retry_strategy"),  # Issue #3.2.1: retry strategy applied
                ),
            )
        return run_data["run_id"]

    def update_pipeline_run(self, run_id: str, **kwargs) -> bool:
        """Update fields on a pipeline_runs row."""
        if not kwargs:
            return False
        allowed = {
            "status",
            "current_phase",
            "completed_phases",
            "phase_outputs",
            "pid",
            "started_at",
            "completed_at",
            "error_message",
            "gateway_url",
            "skip_scoring",
            "scoring_status",
            "scoring_score",
            "retry_of_run_id",
            "retry_strategy",  # Issue #3.2.1: retry linkage
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
            cursor = conn.execute("SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,))
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

    def sweep_zombie_runs(self, now: Optional[str] = None) -> int:  # noqa: C901
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
        # Lazy import: the db package is imported by daemon.py (transitively
        # via run_daemon), so a top-level `from ..daemon import` would deadlock
        # at module load time.
        try:
            from ..daemon import is_process_alive  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover — defensive  # noqa: BLE001
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
                        no_pid_reason = (
                            "no PID recorded (pid column NULL and PID file missing/empty)"
                        )
                    else:
                        try:
                            parsed = int(text)
                            if parsed > 0:
                                effective_pid = parsed
                            else:
                                no_pid_reason = (
                                    "no PID recorded (PID file contains non-positive value)"
                                )
                        except ValueError:
                            no_pid_reason = "no PID recorded (PID file contains non-numeric value)"

                # Decide whether to sweep this row.
                if no_pid_reason is not None:
                    # No usable PID anywhere — sweep with explicit reason.
                    error_message = (
                        "daemon process exited without updating status: " + no_pid_reason
                    )
                    self._mark_crashed(run_id, error_message, now)
                    swept += 1
                    continue

                # We have an effective PID — check liveness.
                if is_process_alive(effective_pid):
                    continue  # live daemon, leave row alone

                # Dead PID — sweep.
                error_message = (
                    f"daemon process exited without updating status " f"(pid {effective_pid})"
                )
                if self._mark_crashed(run_id, error_message, now):
                    swept += 1
            except Exception as exc:  # noqa: BLE001
                # Per-row containment — log and move on.
                run_id_str = row["run_id"] if hasattr(row, "__getitem__") else "<unknown>"
                logger.warning(
                    "sweep_zombie_runs: per-row error on run_id=%s: %s",
                    run_id_str,
                    exc,
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
                run_id,
                exc,
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
        except Exception as exc:  # pragma: no cover — sweep is defensive  # noqa: BLE001
            logger.warning(
                "count_active_pipeline_runs: sweep raised unexpectedly: %s",
                exc,
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
        import os as _os  # noqa: PLC0415
        import signal as _signal  # noqa: PLC0415

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
                (run_id, event_type, phase_id, tokens_consumed, cost_usd, state, metadata_json),
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
