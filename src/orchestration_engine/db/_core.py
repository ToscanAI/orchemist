"""Core connection / transaction mixin for :class:`~orchestration_engine.db.Database`.

Extracted from the original ``db.py`` (EPIC #942, sub-issue 951a) WITHOUT
behavioural change. These eleven members form the connection-management and
generic-query base that every other (still-inline) :class:`Database` method
depends on. Method bodies are byte-identical to the original; only the
import depth of intra-package references is adjusted.
"""

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._consts import default_db_path


class CoreMixin:
    """Connection management, transactions, and generic query helpers.

    Mixed into :class:`Database` (see :mod:`db.__init__`). ``__init__`` calls
    ``self._initialize_database()`` which is resolved via the MRO from the
    schema methods that remain inline on :class:`Database`.
    """

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
        if not hasattr(self._local, "connection"):
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

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Convert SQLite row to dictionary with JSON parsing."""
        data = dict(row)

        # Parse JSON fields
        json_fields = [
            "payload",
            "tags",
            "metadata",
            "config",
            "result",
            "error_patterns",
            "suggested_fixes",
            "input_map",
            "filters",  # trigger fields (Issue #329.1)
            "signals_json",  # routing_decisions (Issue #331.3)
            "affected_files",  # regressions (Issue #3.3a.1)
            "issues_found",  # review_outcomes (Issue #4.1.2)
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

    def close(self) -> None:
        """Close database connections."""
        if hasattr(self._local, "connection"):
            self._local.connection.close()
            delattr(self._local, "connection")
