"""Database layer for the Orchestration Engine.

Provides SQLite-backed persistent storage with WAL mode, proper indexing,
connection management, and schema migrations.
"""

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from contextlib import contextmanager

# Explicit datetime adapters — required for Python 3.12+ which deprecated
# the built-in sqlite3 datetime adapter/converter.
sqlite3.register_adapter(datetime, lambda val: val.isoformat())
sqlite3.register_converter(
    "timestamp", lambda val: datetime.fromisoformat(val.decode())
)

from .schemas import TaskState, OrchestraState, Priority, TaskType


class Database:
    """SQLite database manager with connection pooling and migrations."""
    
    def __init__(self, db_path: Optional[Path] = None):
        """Initialize database connection.
        
        Args:
            db_path: Path to SQLite database file. Defaults to ~/.orchestration-engine/engine.db
        """
        if db_path is None:
            default_dir = Path.home() / ".orchestration-engine"
            default_dir.mkdir(exist_ok=True)
            db_path = default_dir / "engine.db"
        
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
            self._create_indexes(conn)
            
            # Run any pending migrations
            self._run_migrations(conn)
    
    def _create_tables(self, conn: sqlite3.Connection) -> None:
        """Create all database tables."""
        
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
    
    def _create_indexes(self, conn: sqlite3.Connection) -> None:
        """Create performance indexes."""
        
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
            # Add migrations here as needed
            # ("001_add_progress_tracking", self._migration_001_add_progress_tracking),
        ]
        
        # Apply pending migrations
        for name, migration_func in migrations:
            if name not in applied_migrations:
                migration_func(conn)
                conn.execute("INSERT INTO migrations (name) VALUES (?)", (name,))
    
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
            kwargs['started_at'] = datetime.now()
        elif status in ['success', 'failed', 'permanently_failed'] and 'completed_at' not in kwargs:
            kwargs['completed_at'] = datetime.now()
        
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
            'timestamp': datetime.now(),
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
        json_fields = ['payload', 'tags', 'metadata', 'config', 'result', 'error_patterns', 'suggested_fixes']
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
        
        return data
    
    def close(self) -> None:
        """Close database connections."""
        if hasattr(self._local, 'connection'):
            self._local.connection.close()
            delattr(self._local, 'connection')