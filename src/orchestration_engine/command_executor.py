"""CommandExecutor — executes shell commands as pipeline phases.

Issue #190: Command Execution Phase Type
-----------------------------------------
Provides a security-aware executor that runs shell commands in a controlled
environment.  Commands are validated against a configurable allowlist before
execution.  Variable interpolation allows templates to inject runtime values
like ``{output_dir}`` and ``{phase_id}``.

Security model
--------------
* ``subprocess.run`` is called with ``shell=False`` so shell metacharacters
  (``; && || | > $()`` etc.) are never interpreted by a shell — the command is
  split into tokens via ``shlex.split`` and passed directly to ``execvp``.
* Payload values are quoted with ``shlex.quote`` before substitution so
  spaces and metacharacters in variable values cannot alter the token
  structure when the interpolated string is later split by ``shlex.split``.
* Output is capped at :data:`MAX_OUTPUT_BYTES` to prevent memory exhaustion
  from runaway commands.
* :data:`DANGEROUS_PATTERNS` is a denylist checked unconditionally against the
  raw command string *before* any allowlist logic.  Patterns here block
  commands regardless of what ``allowed_commands`` a template declares.
* :data:`DEFAULT_ALLOWED_COMMANDS` is intentionally restrictive; templates
  should declare an explicit ``allowed_commands`` list per phase when they
  need additional commands (e.g. ``npm``, ``node``, ``make``).
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional

from .command_security import DANGEROUS_PATTERNS
from .schemas import TaskResult, TaskSpec, TaskState, TaskType
from .timestamps import now_utc

logger = logging.getLogger(__name__)

# Maximum bytes captured from stdout + stderr combined (prevents OOM on
# commands that produce very large output).
MAX_OUTPUT_BYTES: int = 1_048_576  # 1 MB

# Default command prefixes that are considered safe to run.
# Deliberately restrictive — individual template phases should declare an
# explicit ``allowed_commands`` list if they need additional commands.
DEFAULT_ALLOWED_COMMANDS: List[str] = ["python3", "pytest", "git"]

# ``DANGEROUS_PATTERNS`` is the single-source denylist; it now lives in
# :mod:`orchestration_engine.command_security` and is imported above so the
# OpenRouter shell path and this executor share one definition. ``_check_security``
# references it by name below, so the import is transparent.

# Default timeout in seconds for command execution
DEFAULT_TIMEOUT: int = 120


class CommandSecurityError(ValueError):
    """Raised when a command is not in the security allowlist."""


class _SafeDict(dict):
    """A dict subclass that returns ``{key}`` for missing keys.

    Used for variable interpolation so that unknown placeholders are left as-is
    rather than raising ``KeyError``.
    """

    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return f"{{{key}}}"


class CommandExecutor:
    """Executes shell commands described in a :class:`~.schemas.TaskSpec`.

    Workflow
    --------
    1. Extract the command string from ``task.payload["command"]`` or from a
       ``COMMAND:`` prefix in ``task.payload["prompt"]``.
    2. Perform variable interpolation with values from ``task.payload``,
       quoting string values with :func:`shlex.quote` to prevent injection.
    3. Validate the command against the security allowlist.
    4. Run via ``subprocess.run(shell=False, …)`` after splitting the
       interpolated string with ``shlex.split``; shell metacharacters are
       never interpreted.
    5. Return a :class:`~.schemas.TaskResult` with ``stdout`` + ``stderr``
       combined as the ``text`` field (truncated to :data:`MAX_OUTPUT_BYTES`),
       and the exit code as ``exit_code``.

    Args:
        default_allowed_commands: List of command prefixes that are permitted.
            Defaults to :data:`DEFAULT_ALLOWED_COMMANDS`.
        default_timeout:          Timeout in seconds for each command.
            Defaults to :data:`DEFAULT_TIMEOUT`.
    """

    def __init__(
        self,
        default_allowed_commands: Optional[List[str]] = None,
        default_timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.default_allowed_commands: List[str] = (
            list(default_allowed_commands)
            if default_allowed_commands is not None
            else list(DEFAULT_ALLOWED_COMMANDS)
        )
        self.default_timeout: int = default_timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, task: TaskSpec) -> TaskResult:
        """Execute the command described in *task*.

        Args:
            task: A :class:`~.schemas.TaskSpec` with ``type=TaskType.COMMAND``.
                  The payload may contain:

                  * ``command``      — the raw shell command string (preferred)
                  * ``prompt``       — fallback; if it starts with ``COMMAND:``
                    the remainder is used as the command
                  * ``output_dir``   — substituted into ``{output_dir}`` in the
                    command string
                  * ``phase_id``     — substituted into ``{phase_id}``
                  * ``allowed_commands`` — overrides ``self.default_allowed_commands``
                  * ``cwd``          — working directory for the subprocess

        Returns:
            A :class:`~.schemas.TaskResult` where:

            * ``result["text"]``      — combined stdout + stderr output
              (truncated to :data:`MAX_OUTPUT_BYTES`)
            * ``result["exit_code"]`` — process exit code (or -1 on timeout)
            * ``state``               — ``SUCCESS`` when exit_code == 0,
              ``FAILED`` otherwise (including security/timeout errors)

        Raises:
            ValueError: When no command can be resolved from the payload.
        """
        started_at = now_utc()
        payload: Dict[str, Any] = task.payload or {}

        # ── 1. Resolve command string ──────────────────────────────────
        raw_command = self._resolve_command(payload)

        # ── 2. Variable interpolation ──────────────────────────────────
        command = self._interpolate(raw_command, payload)

        # ── 3. Resolve per-task allowlist ─────────────────────────────
        task_allowed: Optional[List[str]] = payload.get("allowed_commands")
        if task_allowed is not None:
            allowed_commands = list(task_allowed)
        else:
            allowed_commands = self.default_allowed_commands

        # ── 4. Security check ─────────────────────────────────────────
        security_error = self._check_security(command, allowed_commands)
        if security_error:
            logger.warning(
                f"CommandExecutor: SECURITY — command blocked: {command!r}"
            )
            return self._make_result(
                task=task,
                text=f"[SECURITY] {security_error}",
                exit_code=-1,
                state=TaskState.FAILED,
                started_at=started_at,
            )

        # ── 5. Split into token list (shell=False) ────────────────────
        # shlex.split honours quoting introduced by _interpolate so values
        # with spaces/metacharacters remain single tokens.
        try:
            args = shlex.split(command)
        except ValueError as exc:
            return self._make_result(
                task=task,
                text=f"[SECURITY] Could not parse command: {exc}",
                exit_code=-1,
                state=TaskState.FAILED,
                started_at=started_at,
            )

        # ── 6. Execute ────────────────────────────────────────────────
        cwd: Optional[str] = payload.get("cwd")
        logger.info(f"CommandExecutor: running command: {args!r} (cwd={cwd!r})")

        try:
            proc = subprocess.run(
                args,
                shell=False,
                capture_output=True,
                text=True,
                timeout=self.default_timeout,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                f"CommandExecutor: TIMEOUT — command timed out after "
                f"{self.default_timeout}s: {args!r}"
            )
            return self._make_result(
                task=task,
                text=f"[TIMEOUT] Command exceeded {self.default_timeout}s limit",
                exit_code=-1,
                state=TaskState.FAILED,
                started_at=started_at,
            )
        except Exception as exc:
            logger.error(
                f"CommandExecutor: unexpected error running {args!r}: {exc}"
            )
            return self._make_result(
                task=task,
                text=f"[ERROR] {exc}",
                exit_code=-1,
                state=TaskState.FAILED,
                started_at=started_at,
            )

        # ── 7. Build result ───────────────────────────────────────────
        # Truncate each stream before combining to avoid memory exhaustion
        # from commands that produce very large output.
        stdout = (proc.stdout or "")[:MAX_OUTPUT_BYTES]
        stderr = (proc.stderr or "")[:MAX_OUTPUT_BYTES]
        output = stdout + stderr if stdout else stderr
        if len(output) >= MAX_OUTPUT_BYTES:
            output = output[:MAX_OUTPUT_BYTES] + "\n[OUTPUT TRUNCATED]"

        state = TaskState.SUCCESS if proc.returncode == 0 else TaskState.FAILED
        logger.info(
            f"CommandExecutor: command exited {proc.returncode} "
            f"({len(output)} chars output)"
        )

        return self._make_result(
            task=task,
            text=output,
            exit_code=proc.returncode,
            state=state,
            started_at=started_at,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_command(payload: Dict[str, Any]) -> str:
        """Extract the raw command string from the payload.

        Checks ``payload["command"]`` first; falls back to a ``COMMAND:``
        prefix in ``payload["prompt"]``.

        Raises:
            ValueError: When no command can be found.
        """
        if "command" in payload and payload["command"]:
            return str(payload["command"])

        prompt: str = payload.get("prompt", "") or ""
        stripped = prompt.strip()
        if stripped.upper().startswith("COMMAND:"):
            return stripped[len("COMMAND:"):].strip()

        raise ValueError(
            "CommandExecutor: no command found in payload. "
            "Set payload['command'] or prefix prompt with 'COMMAND: …'."
        )

    @staticmethod
    def _interpolate(command: str, payload: Dict[str, Any]) -> str:
        """Substitute ``{variable}`` placeholders with values from *payload*.

        String values are wrapped with :func:`shlex.quote` before
        substitution so that metacharacters in payload values (e.g. spaces,
        semicolons, dollar signs) cannot break token boundaries when the
        interpolated command is later split by ``shlex.split``.

        Uses :class:`_SafeDict` so that unknown placeholders are left intact
        (e.g. ``{phase_id}`` when not present in the payload) rather than
        raising ``KeyError``.

        Recognised placeholders (but any payload key is available):

        * ``{output_dir}``
        * ``{phase_id}``
        """
        quoted: Dict[str, Any] = {
            k: shlex.quote(str(v)) if isinstance(v, str) else v
            for k, v in payload.items()
        }
        safe = _SafeDict(quoted)
        return command.format_map(safe)

    @staticmethod
    def _check_security(command: str, allowed_commands: List[str]) -> Optional[str]:
        """Validate *command* against the denylist, then the allowlist.

        Denylist (:data:`DANGEROUS_PATTERNS`) is checked **first** against the
        raw command string and always wins — a match blocks the command
        regardless of what *allowed_commands* the template declares.

        The allowlist check then splits the command with ``shlex`` to extract
        the executable name (first token) and verifies it matches an entry in
        *allowed_commands*.

        Returns:
            ``None`` when the command is allowed.
            An error message string when the command is blocked.
        """
        # ── 1. Denylist check (unconditional) ────────────────────────────────
        for pattern in DANGEROUS_PATTERNS:
            if pattern.search(command):
                return (
                    f"Command matches dangerous pattern '{pattern.pattern}' "
                    f"and is unconditionally blocked."
                )

        # ── 2. Parse tokens for allowlist check ──────────────────────────────
        try:
            tokens = shlex.split(command)
        except ValueError:
            # Malformed command — block it
            return f"Could not parse command: {command!r}"

        if not tokens:
            return "Empty command"

        executable = tokens[0]

        # ── 3. Allowlist check ───────────────────────────────────────────────
        for allowed in allowed_commands:
            if executable == allowed or executable.endswith(f"/{allowed}"):
                return None

        return (
            f"Command '{executable}' is not in the security allowlist. "
            f"Allowed: {allowed_commands}"
        )

    @staticmethod
    def _make_result(
        task: TaskSpec,
        text: str,
        exit_code: int,
        state: TaskState,
        started_at: datetime,
    ) -> TaskResult:
        """Construct a :class:`~.schemas.TaskResult` from execution outputs."""
        return TaskResult(
            task_id=task.id,
            task_type=task.type,
            state=state,
            confidence=1.0 if state == TaskState.SUCCESS else 0.0,
            result={
                "text": text,
                "exit_code": exit_code,
            },
            started_at=started_at,
            completed_at=now_utc(),
        )
