"""Provider-discoverability command group for the orchestration-engine CLI.

Behavior-neutral extraction (EPIC #942 / issue #1004, 950d). The ``providers``
group (``orch providers list``) and its only helper ``_tier_defaults_for``
previously lived inline in ``cli/__init__.py``; their bodies are moved here
VERBATIM. The group and its command self-register on the shared ``main`` Click
group (imported from ``._root``) at import time via their ``@main.group`` /
``@providers_group.command`` decorators, so the facade only needs to import this
module for the registration side effect.

All external dependencies the commands touch (``PROVIDERS_INFO``, ``bare_id``,
``DEFAULT_MODEL_MAP``) were — and remain — *function-local lazy imports*, not
module-globals, so the 950b/950c ``_cli.<dep>`` call-time facade indirection is
NOT needed here (the test-suite patches ``urllib.request.urlopen`` and imports
``PROVIDERS_INFO`` from its source module, never the cli facade).
"""

import json
import os
from typing import Dict

import click

from ._helpers import print_table
from ._root import main

# ---------------------------------------------------------------------------
# orch providers — read-only provider discoverability (#970, #101 epic-closer)
# ---------------------------------------------------------------------------


@main.group("providers")
def providers_group() -> None:
    """Inspect configured model providers (read-only).

    Lists each provider, the credential env var it needs, whether that var is
    currently set, default tier->model mappings, and a maturity label. Makes no
    network calls, constructs no executors, and touches no database.

    Note: .env files are NOT auto-loaded — export vars in your shell first
    (see docs/openrouter-setup.md for the manual `set -a; source .env` recipe).

    Examples:

      orch providers list            # human-readable table
      orch providers list --json     # machine-readable JSON
    """


def _tier_defaults_for(name: str) -> Dict[str, str]:
    """Return the tier->model default map for *name* (empty for non-tiered providers).

    Derived from the LIVE registries so the displayed defaults can never drift
    from what the executors actually emit: anthropic uses the canonical bare ids
    (``model_registry.bare_id``); openrouter uses ``DEFAULT_MODEL_MAP``
    (anthropic/-prefixed ids). All other providers carry no tier map.
    """
    if name == "anthropic":
        from ..model_registry import bare_id  # noqa: PLC0415

        return {tier: bare_id(tier) for tier in ("haiku", "sonnet", "opus")}
    if name == "openrouter":
        from ..executors.openrouter_executor import DEFAULT_MODEL_MAP  # noqa: PLC0415

        return dict(DEFAULT_MODEL_MAP)
    return {}


@providers_group.command("list")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON.")
def providers_list(json_output: bool) -> None:
    """List model providers, their credential env vars, status, and maturity.

    Read-only: presence of a credential is reported as a boolean only — the raw
    env-var VALUE is never printed, masked, or partially echoed.
    """
    from ..providers_info import PROVIDERS_INFO  # noqa: PLC0415

    # Presence is computed HERE, at call time, from os.environ — never stored on
    # the registry (which is import-pure). bool("") and bool(None) are both False
    # so an unset OR empty var reads as "missing" (pipeline_runner.py:255,286).
    if json_output:
        result = [
            {
                "name": p.name,
                "mode": p.mode,
                "per_phase": p.per_phase,
                "credential_env": p.credential_env,
                "configured": bool(p.credential_env and os.environ.get(p.credential_env, "")),
                "default_models": _tier_defaults_for(p.name),
                "maturity": p.maturity,
                "notes": p.notes,
            }
            for p in PROVIDERS_INFO
        ]
        click.echo(json.dumps(result, indent=2))
        return

    headers = [
        "Provider",
        "Mode",
        "Per-phase",
        "Credential env",
        "Status",
        "Default models",
        "Maturity",
        "Notes",
    ]
    rows = []
    for p in PROVIDERS_INFO:
        if p.credential_env is None:
            cred_cell = "-"
            status_cell = "n/a"
        else:
            cred_cell = p.credential_env
            configured = bool(os.environ.get(p.credential_env, ""))
            status_cell = "set" if configured else "missing"
        defaults = _tier_defaults_for(p.name)
        models_cell = (
            ", ".join(f"{tier}={mid}" for tier, mid in defaults.items()) if defaults else "-"
        )
        rows.append(  # noqa: PERF401
            [
                p.name,
                p.mode,
                "yes" if p.per_phase else "no",
                cred_cell,
                status_cell,
                models_cell,
                p.maturity,
                p.notes,
            ]
        )
    print_table(headers, rows)
