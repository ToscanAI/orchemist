"""Shared output-text helpers used across the engine (Issue #860).

Canonical home for the two output-handling helpers that were previously
duplicated across :mod:`daemon` and :mod:`cli`:

  - :func:`extract_output_text` — pull human-readable text out of a phase
    output dict produced by any executor (Anthropic, DryRun, ...).
  - :func:`safe_write_phase_output` — write a phase's captured output to
    disk unless the agent has already written a *larger* file there
    (Issue #210 — never clobber agent-authored content).

Both helpers preserve the production behaviour they had at their original
sites byte-for-byte.  The canonical :func:`extract_output_text` carries the
``_builtins`` defensive hardening that the cli.py copy gained because Click
shadows ``list`` and ``dict`` in some namespaces.  Without that hardening
``isinstance(val, list)`` raises ``TypeError`` whenever the helper is
called from inside a Click command group — see the docstring of
:func:`extract_output_text` for the full rationale.

Test parity with the deprecated call sites is enforced by
:mod:`tests.test_output_utils` (regression tests for both helpers) and the
existing :mod:`tests.test_cli_overwrite` which is updated to import from
this module.
"""

from __future__ import annotations

import builtins
import json
import logging
from pathlib import Path
from typing import Any, Dict


__all__ = ["extract_output_text", "safe_write_phase_output"]


logger = logging.getLogger(__name__)


def extract_output_text(phase_out: Dict[str, Any]) -> str:
    """Extract human-readable text from a serialised phase output dict.

    Tries common keys used by different executors:
      - ``result.output``  — explicit output key (future executors)
      - ``result.text``    — AnthropicExecutor plain-text response
      - ``result.content`` — alternative content key
      - ``result.message`` — DryRunExecutor mock message
    Falls back to a JSON representation of the ``result`` sub-dict.

    When a value is a list (e.g. Anthropic content-block arrays), text blocks
    of type ``{"type": "text", "text": "..."}`` are joined with double newlines.
    Plain string items in the list are also included.  Blocks with type
    ``tool_use`` or ``thinking`` are intentionally skipped — they carry tool
    parameters or internal reasoning, not human-readable output.

    The ``isinstance`` checks deliberately reference ``builtins.list`` and
    ``builtins.dict`` rather than the bare names.  Click commands shadow
    ``list`` and ``dict`` inside command-group namespaces, so the bare
    ``isinstance(val, list)`` form raised ``TypeError: isinstance() arg 2
    must be a type, a tuple of types, or a union`` whenever this helper was
    called from a Click context — silent silent-drift surface in the daemon
    copy that is paid off here.
    """
    inner = phase_out.get('result', {})
    if not isinstance(inner, dict):
        return str(inner)
    for key in ('output', 'text', 'content', 'message'):
        if key in inner:
            val = inner[key]
            if isinstance(val, builtins.list):
                # Extract text from Anthropic-style content block arrays.
                # Blocks with type "tool_use" or "thinking" are intentionally
                # skipped — they carry tool parameters / internal reasoning,
                # not human-readable output.
                texts = []
                for block in val:
                    if isinstance(block, builtins.dict):
                        if block.get('type') == 'text':
                            texts.append(block.get('text', ''))
                    elif isinstance(block, str):
                        texts.append(block)
                return '\n\n'.join(t for t in texts if t)
            return str(val)
    if inner:
        return json.dumps(inner, indent=2, default=str)
    return ""


def safe_write_phase_output(out_path: Path, new_content: str, phase_id: str) -> None:
    """Write *new_content* to *out_path* unless an agent-written file is larger.

    The size guard compares file bytes against the UTF-8 encoded length of
    *new_content* so that multi-byte characters (em-dashes, emoji, accented
    letters) are counted consistently.  The strictly-greater-than comparison
    means equal-sized files are always (over)written with the fresh capture.

    Args:
        out_path:    Destination path.  Parent directory must already exist.
        new_content: Text to write (UTF-8 encoded on disk).
        phase_id:    Phase identifier used in log messages only.
    """
    if out_path.exists() and out_path.stat().st_size > len(new_content.encode('utf-8')):
        # Agent already wrote a larger file — keep the agent's version.
        logger.info(
            "Phase '%s': keeping agent-written file (%d bytes) over captured "
            "output (%d bytes)",
            phase_id,
            out_path.stat().st_size,
            len(new_content.encode('utf-8')),
        )
    else:
        out_path.write_text(new_content)
