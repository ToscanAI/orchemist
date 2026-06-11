"""Lightweight runner adapter for synchronous pipeline execution.

PipelineRunner is a minimal struct that satisfies the interface contract
required by PhaseSequencer (runner.queue + runner.executors) without
starting background threads or requiring a persistent database.

Used exclusively by the `orch run` CLI command.
"""

import logging
import tempfile
from pathlib import Path
from typing import Any, List, Optional

from .db import Database
from .queue import TaskQueue
from .runner import TaskExecutor  # ABC only — no heavy imports

logger = logging.getLogger(__name__)


class PipelineRunner:
    """Minimal runner adapter for PhaseSequencer.

    Does NOT start threads, does NOT use WorkerPool, RecoveryManager, or
    ProgressTracker. Just a queue + executor list, which is all PhaseSequencer
    needs.

    Args:
        executors: Ordered list of executors. PhaseSequencer picks the first
                   one where can_handle(task_type) returns True.
        db_path:   SQLite database path. Pass ":memory:" for ephemeral runs
                   (no disk footprint). Defaults to a tempfile that is deleted
                   after the context manager exits.

    Usage (as context manager — recommended):
        with PipelineRunner.standalone(api_key="sk-ant-...") as runner:
            seq = PhaseSequencer(template, runner)
            result = seq.execute(initial_input)

    Usage (manual):
        runner = PipelineRunner.standalone(api_key="sk-ant-...")
        try:
            seq = PhaseSequencer(template, runner)
            result = seq.execute(initial_input)
        finally:
            runner.close()
    """

    def __init__(
        self,
        executors: List[TaskExecutor],
        db_path: str = ":memory:",
    ) -> None:
        self._db_path = db_path
        self._tmp_dir = None  # set if we create a temp dir

        # If db_path is sentinel "temp", create a real temp file
        if db_path == "temp":
            self._tmp_dir = tempfile.TemporaryDirectory(prefix="orch-run-")
            db_file = Path(self._tmp_dir.name) / "pipeline.db"
            self._db_path = str(db_file)

        self._db = Database(self._db_path)
        self.queue: TaskQueue = TaskQueue(self._db)
        self.executors: List[TaskExecutor] = executors

    # ------------------------------------------------------------------
    # Factory class methods
    # ------------------------------------------------------------------

    @classmethod
    def standalone(
        cls,
        api_key: Optional[str] = None,
        max_tokens: int = 4096,
        db_path: str = ":memory:",
        executor_type: Optional[str] = None,  # noqa: ARG003
    ) -> "PipelineRunner":
        """Create a PipelineRunner using AnthropicExecutor (direct API calls).

        Args:
            api_key:        Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
            max_tokens:     Maximum output tokens per API call.
            db_path:        SQLite path (":memory:" for no-disk, "temp" for temp file).
            executor_type:  Forwarded from --executor CLI flag. Stored for future use
                            when ClaudeCodeExecutor is wired (see Issue #635 parent epic).

        Raises:
            ValueError: If no API key is found anywhere.
        """
        return cls(
            executors=[cls._build_anthropic_executor(api_key, max_tokens)],
            db_path=db_path,
        )

    @classmethod
    def from_template(
        cls,
        template,  # noqa: ARG003
        api_key: Optional[str] = None,
        max_tokens: int = 4096,
        db_path: str = ":memory:",
    ) -> "PipelineRunner":
        """Create a PipelineRunner pre-configured from a :class:`~templates.PipelineTemplate`.

        Args:
            template:   Loaded :class:`~templates.PipelineTemplate` instance.
            api_key:    Anthropic API key (or ``ANTHROPIC_API_KEY`` env var).
            max_tokens: Maximum output tokens per API call.
            db_path:    SQLite path.

        Returns:
            :class:`PipelineRunner` configured with an AnthropicExecutor.
        """
        return cls.standalone(
            api_key=api_key,
            max_tokens=max_tokens,
            db_path=db_path,
        )

    @classmethod
    def openclaw(
        cls,
        gateway_url: Optional[str] = None,
        gateway_token: Optional[str] = None,
        timeout_seconds: int = 600,
        dry_run: bool = False,
        db_path: str = ":memory:",
    ) -> "PipelineRunner":
        """Create a PipelineRunner using OpenClawExecutor (sub-agent spawning).

        Args:
            gateway_url:       OpenClaw gateway URL (default http://localhost:4444,
                               or ``OPENCLAW_GATEWAY_URL`` env var).
            gateway_token:     Optional bearer token (or ``OPENCLAW_GATEWAY_TOKEN``
                               env var).
            timeout_seconds:   Max seconds per phase session (default 600).
            dry_run:           Skip real HTTP calls and return mock output.
            db_path:           SQLite path.
        """
        from .openclaw_executor import OpenClawExecutor  # noqa: PLC0415

        executor = OpenClawExecutor(
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            timeout_seconds=timeout_seconds,
            dry_run=dry_run,
        )
        return cls(executors=[executor], db_path=db_path)

    @classmethod
    def openrouter(
        cls,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_map: Optional[dict] = None,
        timeout_seconds: int = 300,
        max_tokens: int = 16384,
        db_path: str = ":memory:",
    ) -> "PipelineRunner":
        """Create a PipelineRunner using OpenRouterExecutor (multi-provider routing).

        Args:
            api_key:          OpenRouter API key (or ``OPENROUTER_API_KEY`` env var).
            base_url:         API base URL (for proxies or self-hosted routers).
            model_map:        Custom model tier → model ID overrides.
            timeout_seconds:  HTTP request timeout per call (default 300s).
            max_tokens:       Maximum output tokens per request.
            db_path:          SQLite path.

        Raises:
            ValueError: If no API key is found anywhere.
        """
        executor = cls._build_openrouter_executor(
            api_key,
            base_url,
            model_map,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        )
        return cls(executors=[executor], db_path=db_path)

    @classmethod
    def dry_run(
        cls,
        delay_seconds: float = 0.0,
        failure_rate: float = 0.0,
        db_path: str = ":memory:",
    ) -> "PipelineRunner":
        """Create a PipelineRunner using DryRunExecutor (testing/CI).

        Args:
            delay_seconds: Simulated execution delay per phase.
            failure_rate:  Probability [0.0-1.0] of simulated phase failure.
            db_path:       SQLite path.
        """
        from .runner import DryRunExecutor  # noqa: PLC0415

        executor = DryRunExecutor(
            delay_seconds=delay_seconds,
            failure_rate=failure_rate,
        )
        return cls(executors=[executor], db_path=db_path)

    @classmethod
    def claudecode(
        cls,
        mcp_server: Any,
        db_path: str = ":memory:",
    ) -> "PipelineRunner":
        """Create a PipelineRunner using ClaudeCodeExecutor (MCP session routing).

        Routes task execution through the active Claude Code MCP session using
        the sampling capability. No Anthropic API key required — uses the user's
        Claude Code subscription.

        Args:
            mcp_server:      A FastMCP server instance with an active session.
                             Must not be None and must expose get_context().
            db_path:         SQLite path (":memory:" for no-disk, "temp" for temp file).

        Raises:
            ValueError: If mcp_server is None or lacks get_context.
                        Propagated directly from ClaudeCodeExecutor.__init__.
        """
        from .executors.claudecode_executor import ClaudeCodeExecutor  # noqa: PLC0415

        executor = ClaudeCodeExecutor(mcp_server=mcp_server)
        return cls(executors=[executor], db_path=db_path)

    # ------------------------------------------------------------------
    # Shared per-provider executor builders (#969)
    # ------------------------------------------------------------------
    # These centralise credential resolution + the eager missing-credential
    # raise so the single-provider factories AND from_providers inherit ONE
    # message per provider. The messages are kept BYTE-IDENTICAL to the
    # historical single-factory raises (preserving existing equality pins, e.g.
    # test_openrouter_executor's _CLOUD_GUARD_MESSAGE and the match="API key"
    # pins): each already names the provider (case-insensitively) AND its env
    # var, which is exactly what INV-3 / contract #5 require for the multi-
    # provider path too.

    @staticmethod
    def _build_anthropic_executor(api_key, max_tokens=4096):
        """Construct an AnthropicExecutor, raising if no credential is found.

        Raises:
            ValueError: naming provider 'anthropic' (ci) + ANTHROPIC_API_KEY.
        """
        import os  # noqa: PLC0415

        from .executors.anthropic_executor import AnthropicExecutor  # noqa: PLC0415

        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not resolved_key:
            raise ValueError(
                "Anthropic API key required for standalone mode.\n"
                "  Option 1: orch run --api-key sk-ant-...\n"
                "  Option 2: export ANTHROPIC_API_KEY=sk-ant-..."
            )
        return AnthropicExecutor(api_key=resolved_key, max_tokens=max_tokens)

    @staticmethod
    def _build_openrouter_executor(
        api_key,
        base_url=None,
        model_map=None,
        timeout_seconds: int = 300,
        max_tokens: int = 16384,
    ):
        """Construct an OpenRouterExecutor, raising if no credential is found.

        A custom (non-default) ``base_url`` targets a self-hosted / local
        OpenAI-compatible server (Ollama, LM Studio, vLLM) that needs no
        OpenRouter key; in that case a harmless placeholder bearer is supplied.

        Raises:
            ValueError: naming provider 'openrouter' + OPENROUTER_API_KEY (only
                        when the default cloud endpoint is targeted without a key).
        """
        import os  # noqa: PLC0415

        from .executors.openrouter_executor import OpenRouterExecutor  # noqa: PLC0415

        resolved_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        # Normalize with the same .rstrip("/") the executor applies
        # (openrouter_executor.py:153) before comparing.
        _is_custom_endpoint = bool(base_url) and (
            base_url.rstrip("/") != OpenRouterExecutor.DEFAULT_BASE_URL
        )
        if not resolved_key and not _is_custom_endpoint:
            # Cloud OpenRouter (default base_url) still requires a key — guard UNWEAKENED.
            # Byte-identical to the historical message (test_openrouter_executor's
            # _CLOUD_GUARD_MESSAGE pins equality); "OpenRouter" (ci) + the env var
            # already satisfy contract #5 for the multi-provider path.
            raise ValueError(
                "OpenRouter API key required.\n"
                "  Option 1: orch run --api-key sk-or-...\n"
                "  Option 2: export OPENROUTER_API_KEY=sk-or-..."
            )
        if not resolved_key:
            # Keyless local endpoint: supply a harmless placeholder bearer so the
            # Authorization header is well-formed. Local servers ignore the token;
            # a real proxy that validates returns a clear 401.
            resolved_key = "ollama"
        return OpenRouterExecutor(
            api_key=resolved_key,
            base_url=base_url,
            model_map=model_map,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        )

    @classmethod
    def from_providers(
        cls,
        template: Any,
        anthropic_api_key: Optional[str] = None,
        openrouter_api_key: Optional[str] = None,
        openrouter_base_url: Optional[str] = None,
        openrouter_model_map: Optional[dict] = None,
        max_tokens: int = 4096,
        default_provider: str = "anthropic",
        db_path: str = ":memory:",
    ) -> "PipelineRunner":
        """Build a multi-executor runner: one executor per provider the template
        references (via per-phase ``provider:``), plus the ``default_provider``.

        Construction is EAGER and credential-checked: a referenced provider whose
        credential is absent raises :class:`ValueError` naming the provider + env
        var (mirrors the single-provider factories). The ``default_provider``
        executor is placed FIRST so no-``provider`` phases keep hitting
        ``executors[0]`` (backward compat — every ``can_handle`` is
        unconditionally True, so list ORDER is the backward-compat contract).

        Args:
            template:             Loaded :class:`~templates.PipelineTemplate`.
            anthropic_api_key:    Anthropic key (or ``ANTHROPIC_API_KEY`` env var).
            openrouter_api_key:   OpenRouter key (or ``OPENROUTER_API_KEY`` env var).
            openrouter_base_url:  Run-level OpenRouter base URL (``--base-url``).
            openrouter_model_map: Run-level OpenRouter tier→id map (``--model-map``).
            max_tokens:           Max output tokens for the Anthropic executor.
            default_provider:     The run's primary provider — placed FIRST.
            db_path:              SQLite path.

        Raises:
            ValueError: unknown provider in ``provider:`` (not in
                        ``KNOWN_PROVIDERS``), or a referenced provider's
                        credential is missing.
        """
        from .templates import TemplateEngine  # noqa: PLC0415

        # 1. Scan: union of declared per-phase providers + the default
        #    (default FIRST so executors[0] is the no-provider fallback).
        referenced: List[str] = [default_provider]
        for phase in getattr(template, "phases", []) or []:
            prov = getattr(phase, "provider", None)
            if prov and prov not in referenced:
                referenced.append(prov)

        # 2. Runtime guard (INV-2): reject unknown providers BEFORE building.
        unknown = [p for p in referenced if p not in TemplateEngine.KNOWN_PROVIDERS]
        if unknown:
            raise ValueError(
                f"Unknown provider(s) {sorted(set(unknown))} in template phases. "
                f"Known per-phase providers: {sorted(TemplateEngine.KNOWN_PROVIDERS)}. "
                "gemini/claudecode/openclaw are not per-phase providers in v1.1 "
                "(see docs/template-authoring.md#provider)."
            )

        # 3. Build one executor per referenced provider, DEFAULT FIRST (the
        #    list comprehension preserves the insertion order of `referenced`).
        builders = {
            "anthropic": lambda: cls._build_anthropic_executor(anthropic_api_key, max_tokens),
            "openrouter": lambda: cls._build_openrouter_executor(
                openrouter_api_key, openrouter_base_url, openrouter_model_map
            ),
        }
        executors: List[TaskExecutor] = [builders[p]() for p in referenced]
        return cls(executors=executors, db_path=db_path)

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "PipelineRunner":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def close(self) -> None:
        """Release database connections and clean up temp files."""
        try:
            self._db.close()
        except Exception:  # noqa: BLE001
            pass
        if self._tmp_dir is not None:
            self._tmp_dir.cleanup()
            self._tmp_dir = None
