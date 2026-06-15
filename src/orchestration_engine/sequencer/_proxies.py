"""Prompt-rendering proxy classes for the phase sequencer.

EPIC #942, sub-issue 953a — these classes were extracted VERBATIM from the
former single-file ``sequencer.py``. They are pure (no reference to
:class:`PhaseSequencer` / :class:`StateMachineSequencer`); they only depend on
the :func:`._helpers._extract_phase_text` free function. Re-exported by the
package facade and imported into the inline class bodies by bare name.
"""

import logging
from typing import Any, List, Optional

from ._helpers import _extract_phase_text

logger = logging.getLogger(__name__)


class _SafeDict(dict):
    """A dict subclass that returns a placeholder string for missing keys.

    This prevents ``str.format()`` calls from raising ``KeyError`` when the
    template references a phase output that has not yet been produced (e.g.
    due to template authoring errors).

    When a ``missing_sink`` set is supplied, every emitted ``<MISSING:key>``
    marker is also recorded into it. This lets callers distinguish markers that
    THIS mapping actually generated (a genuine missing config/input reference)
    from ``<MISSING:...>`` substrings that merely appear in inlined content
    (e.g. ``context_files`` / ``skill_context`` text) — the latter never
    invoke ``__missing__`` and so are never recorded (#535).
    """

    def __init__(self, *args, missing_sink: Optional[set] = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Stored via object.__setattr__ so __getattr__ below never intercepts it.
        object.__setattr__(self, "_missing_sink", missing_sink)

    def __missing__(self, key: str) -> str:
        logger.warning(f"Template referenced missing key: '{key}' — substituting <MISSING:{key}>")
        sink = object.__getattribute__(self, "_missing_sink")
        if sink is not None:
            sink.add(f"<MISSING:{key}>")
        return f"<MISSING:{key}>"

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError:
            logger.warning(
                f"Template referenced missing attribute: '{key}' — substituting <MISSING:{key}>"
            )
            try:
                sink = object.__getattribute__(self, "_missing_sink")
            except AttributeError:
                sink = None
            if sink is not None:
                sink.add(f"<MISSING:{key}>")
            return f"<MISSING:{key}>"


class _PhaseOutput:
    """Wrapper that allows ``{phase_id.output}`` syntax in prompt templates.

    When a template references ``{requirements.output}``, Python's ``str.format()``
    calls ``getattr(phase_obj, 'output')``.  This class provides that attribute.
    It also has a ``__format__`` method so ``{requirements}`` (without ``.output``)
    returns the output text directly.
    """

    def __init__(self, text: str) -> None:
        self.output = text
        self._text = text

    def __format__(self, format_spec: str) -> str:
        return format(self._text, format_spec)

    def __str__(self) -> str:
        return self._text

    def __repr__(self) -> str:
        return f"_PhaseOutput({self._text[:80]!r}...)"


class _PreviousOutputProxy:
    """Smart proxy for the ``{previous_output}`` template variable (fix/243).

    Behaviour depends on whether *output_dir* is set:

    * **output_dir set** — ``{previous_output}`` expands to a compact,
      file-path-based summary (phase name + word count + ``→ path/phase.md``).
      Full phase content is NOT inlined, saving 30 K+ tokens per run.
      ``{previous_output[phase_id]}`` prepends a ``"Full output at: …"`` note
      before the inline text so models know where to find the full version.

    * **output_dir not set** — ``{previous_output}`` expands to
      ``str(phase_outputs)`` (the old behaviour, full backward compatibility).
      ``{previous_output[phase_id]}`` returns the extracted text with no note.

    Missing phase IDs return ``<MISSING:previous_output[phase_id]>`` so
    template authoring errors produce visible, non-crashing placeholders.
    """

    def __init__(
        self,
        phase_outputs: dict,
        output_dir: Optional[str],
        phase_map: dict,
        missing_sink: Optional[set] = None,
    ) -> None:
        self._phase_outputs = phase_outputs
        self._output_dir = output_dir
        self._phase_map = phase_map
        # Optional set that collects emitted <MISSING:previous_output[...]>
        # markers so the dispatch-level guard can reject genuinely-missing
        # upstream references (#535).
        self._missing_sink = missing_sink

    # ── str.format() calls __format__ for {previous_output} ──────────────────

    def __format__(self, format_spec: str) -> str:
        if self._output_dir:
            return format(self._build_summary(), format_spec)
        return format(str(self._phase_outputs), format_spec)

    def __str__(self) -> str:
        if self._output_dir:
            return self._build_summary()
        return str(self._phase_outputs)

    # ── str.format() calls __getitem__ for {previous_output[phase_id]} ───────

    def __getitem__(self, key: str) -> str:
        if key not in self._phase_outputs:
            if self._missing_sink is not None:
                self._missing_sink.add(f"<MISSING:previous_output[{key}]>")
            return f"<MISSING:previous_output[{key}]>"
        inline = _extract_phase_text(self._phase_outputs[key])
        if self._output_dir:
            safe_pid = key.replace("-", "_")
            return f"Full output at: {self._output_dir}/{safe_pid}.md\n\n{inline}"
        return inline

    # ── internal helpers ──────────────────────────────────────────────────────

    def _build_summary(self) -> str:
        """Return compact summary lines, one per prior phase."""
        if not self._phase_outputs:
            return "No prior phases."
        lines: List[str] = []
        for pid, pout in self._phase_outputs.items():
            text = _extract_phase_text(pout)
            word_count = len(text.split()) if text else 0
            phase_def = self._phase_map.get(pid)
            phase_name = phase_def.name if phase_def else pid
            safe_pid = pid.replace("-", "_")
            lines.append(
                f"- {phase_name} ({pid}): completed, ~{word_count} words"
                f" → {self._output_dir}/{safe_pid}.md"
            )
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"_PreviousOutputProxy(output_dir={self._output_dir!r}, phases={list(self._phase_outputs.keys())})"  # noqa: E501


class _PreviousOutputInlineProxy:
    """Proxy for ``{previous_output_inline}`` — always returns full inline content.

    Preserves the pre-fix/243 ``{previous_output}`` behaviour for templates
    that explicitly require every prior phase output dumped inline.  The full
    raw ``phase_outputs`` dict repr is returned as a string, regardless of
    whether *output_dir* is configured.
    """

    def __init__(self, phase_outputs: dict, missing_sink: Optional[set] = None) -> None:
        self._phase_outputs = phase_outputs
        self._missing_sink = missing_sink

    def __format__(self, format_spec: str) -> str:
        return format(str(self._phase_outputs), format_spec)

    def __str__(self) -> str:
        return str(self._phase_outputs)

    def __getitem__(self, key: str) -> str:
        if key not in self._phase_outputs:
            if self._missing_sink is not None:
                self._missing_sink.add(f"<MISSING:previous_output_inline[{key}]>")
            return f"<MISSING:previous_output_inline[{key}]>"
        return _extract_phase_text(self._phase_outputs[key])

    def __repr__(self) -> str:
        return f"_PreviousOutputInlineProxy(phases={list(self._phase_outputs.keys())})"
