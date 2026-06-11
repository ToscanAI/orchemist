"""providers_info.py — frozen, import-safe registry of the engine's model providers.

The single in-code source the read-only ``orch providers list`` command renders
(#970, the #101 epic-closer). It enumerates the SIX providers shipped by the
engine and, per provider, pins the facts that are *static* (provider identity,
run mode, per-phase eligibility, credential env-var NAME, maturity label, a short
human note).

Design constraints (all load-bearing):

* **Pure data, zero side effects, import-safe.** This module performs NO
  environment reads, NO I/O, and imports NO executor at module top. The
  registry stores the credential env-var *name* as a string; whether that var is
  *present* in the environment is computed by the CLI command at call time
  (``bool(os.environ.get(name, ""))`` — the factories' own idiom,
  ``pipeline_runner.py:255,286``). Storing the boolean here would freeze it at
  import and is forbidden.
* **Tier→model defaults are NOT stored here.** They are derived from the live
  registries (``model_registry.bare_id`` for anthropic;
  ``executors.openrouter_executor.DEFAULT_MODEL_MAP`` for openrouter) by the CLI
  command, so the displayed defaults can never drift from what the executors
  actually emit. Duplicating them here would invite drift.
* **Single source for maturity.** The ``maturity`` strings byte-match the
  unwrapped ``docs/CURRENT-STATE.md`` "Executor maturity" table labels
  (Production / Production / Limited / Experimental / Deprecated / Stable). A
  drift-guard test binds these to the doc region so a renamed label fails CI.
* **per_phase** mirrors ``TemplateEngine.KNOWN_PROVIDERS = ["anthropic",
  "openrouter"]`` (``templates.py``). The literals here are bound to that live
  constant by a drift-guard test, so they cannot silently diverge.

The six provider identities are grounded directly in the ``provider_name`` class
attributes (the authoritative per-phase identity, #969):

* ``anthropic``  — ``executors/anthropic_executor.py``  (``provider_name``)
* ``openrouter`` — ``executors/openrouter_executor.py`` (``provider_name``)
* ``claudecode`` — ``executors/claudecode_executor.py`` (``provider_name``)
* ``gemini``     — ``executors/gemini_cli_executor.py`` (``provider_name``)
* ``openclaw``   — ``openclaw_executor.py``             (``provider_name``)
* ``dryrun``     — ``runner.py``                        (``provider_name``)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class ProviderInfo:
    """Static facts about one model provider (rendered by ``orch providers list``).

    Env-var *presence* is intentionally absent: it is computed at call time from
    :pyattr:`credential_env`, never stored (see module docstring).
    """

    #: The ``provider_name`` identity (the per-phase ``provider:`` value, #969).
    name: str
    #: The ``orch run --mode`` value, or a parenthetical for non-mode executors
    #: (mirrors the CURRENT-STATE "Executor maturity" Mode column).
    mode: str
    #: True iff ``name`` is a valid per-phase ``provider:`` value — i.e. it is in
    #: ``TemplateEngine.KNOWN_PROVIDERS`` (``["anthropic", "openrouter"]``). Bound
    #: to that live constant by a drift-guard test.
    per_phase: bool
    #: Credential env-var NAME, or ``None`` for keyless / mock / subscription
    #: providers. Only the NAME is stored — never the value.
    credential_env: Optional[str]
    #: Maturity label, byte-matching the unwrapped ``docs/CURRENT-STATE.md``
    #: "Executor maturity" table (no ``**bold**`` markup). Single source of truth
    #: for the command; drift-guarded against the doc.
    maturity: str
    #: Short human-facing note (base-url override, gateway state, prototype flag,
    #: subscription model, etc.). Free-form; carries no behavioral contract.
    notes: str


#: The single source the ``orch providers list`` command renders. One frozen
#: record per provider; order is the rendering order (credentialed-first reads
#: naturally, mock last). Every literal is grounded in the citations in the
#: module docstring and ``docs/CURRENT-STATE.md`` (modes + maturity columns).
PROVIDERS_INFO: Tuple[ProviderInfo, ...] = (
    ProviderInfo(
        name="anthropic",
        mode="standalone",
        per_phase=True,  # in KNOWN_PROVIDERS (templates.py)
        credential_env="ANTHROPIC_API_KEY",  # pipeline_runner.py:255
        maturity="Production",  # CURRENT-STATE.md "Executor maturity": AnthropicExecutor
        notes="Direct Anthropic Messages API; primary BYO-key path.",
    ),
    ProviderInfo(
        name="openrouter",
        mode="openrouter",
        per_phase=True,  # in KNOWN_PROVIDERS (templates.py)
        credential_env="OPENROUTER_API_KEY",  # pipeline_runner.py:286
        maturity="Production",  # CURRENT-STATE.md "Executor maturity": OpenRouterExecutor
        notes=(
            "Default base-url https://openrouter.ai/api/v1; base-url/model-map "
            "overridable on `orch run` for local OpenAI-compatible endpoints (#968)."
        ),
    ),
    ProviderInfo(
        name="claudecode",
        mode="(MCP session)",
        per_phase=False,
        credential_env=None,  # keyless — Claude Code subscription, MCP tool handler only
        maturity="Limited",  # CURRENT-STATE.md "Executor maturity": ClaudeCodeExecutor
        notes="Uses your Claude Code subscription; only inside an MCP session.",
    ),
    ProviderInfo(
        name="gemini",
        mode="(dialogue phase)",
        per_phase=False,
        credential_env=None,  # keyless — dialogue-phase prototype (#677); no API-key read
        maturity="Experimental",  # CURRENT-STATE.md "Executor maturity": GeminiCliExecutor
        notes="Dialogue-phase prototype (#677); no tool-calling/streaming/retry.",
    ),
    ProviderInfo(
        name="openclaw",
        mode="openclaw",
        per_phase=False,
        credential_env="OPENCLAW_GATEWAY_TOKEN",  # cli.py:1070/1284/1431/2097
        maturity="Deprecated",  # CURRENT-STATE.md "Executor maturity": OpenClawExecutor
        notes="Gateway no longer active; kept for historical runs.",
    ),
    ProviderInfo(
        name="dryrun",
        mode="dry-run",
        per_phase=False,
        credential_env=None,  # mock executor — no network, no key
        maturity="Stable",  # CURRENT-STATE.md "Executor maturity": DryRunExecutor
        notes="Mock executor; validates structure/interpolation, no network calls.",
    ),
)
