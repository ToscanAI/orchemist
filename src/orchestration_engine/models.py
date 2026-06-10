"""Compatibility shim for pipeline model classes.

This module re-exports ``PhaseDefinition`` and ``PipelineTemplate`` from
``orchestration_engine.templates`` with additional keyword argument aliases
for test / external-consumer compatibility.

Issue #651: acceptance tests use ``model=``, ``next_phases=``, and
``entry_point=`` kwargs which differ from the canonical field names.
"""

from __future__ import annotations

from typing import Any, List, Optional

from .templates import (
    PhaseDefinition as _PhaseDefinition,
)
from .templates import (
    PipelineTemplate as _PipelineTemplate,
)


def PhaseDefinition(  # noqa: N802 — intentionally matches class-like name
    id: str,
    name: str = "",
    *,
    model: str = "sonnet",
    model_tier: str = "",
    prompt_template: str = "",
    max_iterations: int = 0,
    next_phases: Any = None,  # ignored — transitions dict is used instead
    transitions: Optional[dict] = None,
    **kwargs: Any,
) -> _PhaseDefinition:
    """Create a :class:`~orchestration_engine.templates.PhaseDefinition`.

    Accepts both canonical field names and compatibility aliases:

    * ``model`` → ``model_tier`` (if ``model_tier`` is not given)
    * ``next_phases`` → accepted and ignored (use ``transitions`` instead)
    * ``name`` defaults to ``id`` when omitted
    * When ``max_iterations > 1`` and no ``transitions`` are specified,
      a self-loop via ``request_changes`` is added automatically so that
      the phase can loop until exhaustion.

    All other keyword arguments are forwarded to :class:`PhaseDefinition`.
    """
    effective_model_tier = model_tier or model or "sonnet"
    effective_name = name or id
    effective_transitions = dict(transitions) if transitions else {}

    # Auto-wire self-loop: when max_iterations > 1 and no transitions,
    # add request_changes → self so the phase can loop until exhausted.
    if max_iterations > 1 and not effective_transitions:
        effective_transitions = {"request_changes": id}

    return _PhaseDefinition(
        id=id,
        name=effective_name,
        model_tier=effective_model_tier,
        prompt_template=prompt_template,
        max_iterations=max_iterations,
        transitions=effective_transitions,
        **{k: v for k, v in kwargs.items() if k in _PhaseDefinition.__dataclass_fields__},
    )


def PipelineTemplate(  # noqa: N802 — intentionally matches class-like name
    id: str,
    name: str = "",
    *,
    phases: Optional[List[_PhaseDefinition]] = None,
    entry_point: Optional[str] = None,  # accepted, ignored (first phase is entry point)
    **kwargs: Any,
) -> _PipelineTemplate:
    """Create a :class:`~orchestration_engine.templates.PipelineTemplate`.

    Accepts both canonical field names and compatibility aliases:

    * ``entry_point`` → accepted and ignored (the sequencer always uses the
      first phase as the entry point)
    * ``name`` defaults to ``id`` when omitted

    All other keyword arguments are forwarded to :class:`PipelineTemplate`.
    """
    effective_name = name or id
    effective_phases = phases or []

    return _PipelineTemplate(
        id=id,
        name=effective_name,
        phases=effective_phases,
        **{k: v for k, v in kwargs.items() if k in _PipelineTemplate.__dataclass_fields__},
    )
