"""Pure module-level helper functions for the phase sequencer.

EPIC #942, sub-issue 953a — these functions were extracted VERBATIM from the
former single-file ``sequencer.py``. They have zero class behaviour (they do
not reference :class:`PhaseSequencer` / :class:`StateMachineSequencer`), so
they live here and are imported back into the package facade and into the
inline class bodies by bare name.
"""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from ..schemas import TaskType

logger = logging.getLogger(__name__)


def _is_within_dir(path: Path, directory: Path) -> bool:
    """Return True if *path* is the same as, or a descendant of, *directory*.

    Both arguments should already be resolved (absolute, symlink-free) paths.
    """
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _extract_phase_text(phase_output: Any) -> str:
    """Extract clean text from a phase output dict.

    Phase outputs are stored as ``TaskResult.model_dump()`` dicts.  The actual
    text lives at ``result.text`` (or ``result["text"]``).  If the output is
    already a string, return it as-is.
    """
    if isinstance(phase_output, str):
        return phase_output
    if isinstance(phase_output, dict):
        # Primary path: result dict from TaskResult.model_dump()
        result = phase_output.get("result", {})
        if isinstance(result, dict):
            text = result.get("text", "")
            if text:
                return str(text)
        # Fallback: maybe it's a flat dict with 'text' at top level
        text = phase_output.get("text", "")
        if text:
            return str(text)
        # Last resort: stringify but warn
        logger.warning(
            f"Phase output dict has no 'result.text' key; falling back to str(). "
            f"Keys: {list(phase_output.keys())}"
        )
        return str(phase_output)
    return str(phase_output)


# ---------------------------------------------------------------------------
# Issue #651 — Finding analysis helpers for MAX_ITERATIONS_EXCEEDED
# ---------------------------------------------------------------------------

#: Regex matching a tagged finding line: [SEVERITY][category] description
_FINDING_TAG_RE = re.compile(r"^\s*\[([A-Za-z]+)\]\[([^\]]+)\]\s+(.+)$")


def _extract_findings_from_text(text: str) -> list:
    """Extract findings from a round file's text content.

    Returns a list of finding strings.  Tagged lines of the form
    ``[SEVERITY][category] description`` are returned as individual findings.
    If no tagged lines exist, the entire file content (up to 2 000 chars) is
    returned as a single finding — unless the file contains only markdown
    headers or empty lines, in which case an empty list is returned.
    """
    findings = []
    for line in text.splitlines():
        if _FINDING_TAG_RE.match(line):
            findings.append(line.strip())  # noqa: PERF401
    if not findings:
        # No tagged lines — check for substantive untagged content
        content_lines = [
            line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")
        ]
        if content_lines:
            findings = [text.strip()[:2000]]
    return findings


def _are_findings_similar(a: str, b: str) -> bool:
    """Return *True* if two finding strings are substantially similar.

    Uses word-set Jaccard similarity (intersection / union ≥ 0.50) after
    lowercasing and stripping ``[TAG][TAG]`` prefixes.  Empty word-sets
    are never considered similar.
    """

    def _words(s: str) -> set:
        # Strip [SEVERITY][category] tag prefix before comparison
        s = re.sub(r"^\s*\[[^\]]+\]\[[^\]]+\]\s*", "", s.lower())
        return set(re.findall(r"\w+", s))

    words_a = _words(a)
    words_b = _words(b)
    if not words_a or not words_b:
        return False
    intersection = len(words_a & words_b)
    union = len(words_a | words_b)
    return (intersection / union) >= 0.50


def _analyze_round_findings(output_dir, phase_id: str, max_iterations: int) -> str:  # noqa: C901
    """Analyse iteration-indexed round files for repeated vs new findings.

    Returns an analysis string to append to the error message, or ``""`` when
    analysis is not applicable (non-iterative phase, fewer than 2 files,
    fewer than 2 readable files).

    Side-effects when analysis is produced:
    * Logs the result at ``ERROR`` level.
    * Writes ``finding_analysis.md`` to *output_dir*.
    """
    if max_iterations <= 1:
        return ""  # Non-iterative phase — skip

    if output_dir is None:
        return ""

    output_path = Path(output_dir)
    safe_pid = re.sub(r"[^\w\-]", "_", phase_id)

    # Discover all round files: {safe_pid}_round{N}.md
    round_files = []
    for n in range(1, max_iterations + 2):  # scan up to max+1 to catch edge cases
        rp = output_path / f"{safe_pid}_round{n}.md"
        if rp.exists():
            round_files.append((n, rp))

    if len(round_files) < 2:
        return ""  # Nothing to compare

    # Read each file; skip unreadable files with a warning
    per_round_findings: list = []
    for n, rp in round_files:
        try:
            text = rp.read_text(encoding="utf-8", errors="replace")
            findings = _extract_findings_from_text(text)
            per_round_findings.append(findings)
        except Exception as exc:  # noqa: BLE001, PERF203
            logger.warning(
                "Finding analysis: could not read round file '%s': %s — skipping.", rp, exc
            )

    if len(per_round_findings) < 2:
        # After skipping unreadable files, fewer than 2 readable files remain
        return ""

    n_rounds = len(per_round_findings)

    # Check how many rounds contributed at least one finding
    rounds_with_findings = [f for f in per_round_findings if f]
    if len(rounds_with_findings) < 2:
        analysis = (
            f"{n_rounds} round files found but no structured findings could be "
            f"extracted for comparison."
        )
        logger.error("Finding analysis for phase '%s': %s", phase_id, analysis)
        if output_dir:
            try:
                analysis_path = output_path / "finding_analysis.md"
                analysis_path.write_text(
                    f"# Finding Analysis\n\nPhase: `{phase_id}`\n\n{analysis}\n",
                    encoding="utf-8",
                )
            except Exception as _exc:  # noqa: BLE001
                logger.warning("Failed to write finding_analysis.md: %s", _exc)
        return analysis

    # Compare findings across all rounds: find any pair from different rounds
    # that is similar.  Track the finding that matches the most rounds.
    all_findings_flat = [
        (r_idx, finding)
        for r_idx, findings in enumerate(per_round_findings)
        for finding in findings
    ]

    repeated_finding = None
    best_match_count = 0

    for i, (round_i, finding_i) in enumerate(all_findings_flat):
        match_rounds = {round_i}
        for j, (round_j, finding_j) in enumerate(all_findings_flat):
            if i == j or round_j == round_i:
                continue
            if _are_findings_similar(finding_i, finding_j):
                match_rounds.add(round_j)
        if len(match_rounds) >= 2 and len(match_rounds) > best_match_count:
            best_match_count = len(match_rounds)
            repeated_finding = finding_i

    if repeated_finding is not None:
        summary = repeated_finding[:200]
        analysis = (
            f"Repeated finding detected across {best_match_count} rounds: {summary}. "
            f"The loop may be stuck on a hallucinated or unfixable issue."
        )
    else:
        analysis = (
            f"All {n_rounds} rounds raised different issues. The code may need "
            f"manual intervention or the issue should be split."
        )

    logger.error("Finding analysis for phase '%s': %s", phase_id, analysis)
    if output_dir:
        try:
            analysis_path = output_path / "finding_analysis.md"
            analysis_path.write_text(
                f"# Finding Analysis\n\nPhase: `{phase_id}`\n\n{analysis}\n",
                encoding="utf-8",
            )
        except Exception as _exc:  # noqa: BLE001
            logger.warning("Failed to write finding_analysis.md: %s", _exc)

    return analysis


def _wrap_callable_runner(fn):  # noqa: C901
    """Wrap a plain callable as a minimal runner object for testing.

    When *fn* is a callable (not already a runner with `.queue`/`.executors`),
    this creates a lightweight shim so that ``StateMachineSequencer`` can
    accept a bare function as its ``runner`` argument in unit tests.

    The callable signature expected by the shim::

        fn(phase_def, context, **kwargs) -> dict

    The returned dict is used as the phase result directly.
    """

    class _FakeQueue:
        def __init__(self):
            self._store = {}

        def submit_task(self, spec):
            self._store[spec.id] = spec
            return spec.id

        def get_task(self, task_id):
            return self._store.get(task_id)

        def complete_task(self, task_id, result):
            pass

        def fail_task(self, task_id, error):
            pass

    class _FakeExecutor:
        def __init__(self, callable_fn):
            self._fn = callable_fn

        def can_handle(self, task_type):  # noqa: ARG002
            return True

        def execute(self, task_spec, **kwargs):
            # Call the wrapped function and convert the result
            result_dict = self._fn(
                None,  # phase_def (not available here)
                task_spec.payload.get("prompt", ""),
                **kwargs,
            )
            # Wrap the dict in a minimal object that satisfies _execute_and_wait
            return _FakeTaskResult(result_dict)

    class _FakeTaskResult:
        """Minimal TaskResult shim for use in tests."""

        def __init__(self, data: dict):
            self._data = data
            # Map common dict keys to TaskResult attributes
            self.state = type("S", (), {"value": "success"})()
            self.state = _FakeState(data)
            self.confidence = data.get("confidence", 0.8)
            self.metadata = data.get("metadata", {})
            self.errors = data.get("errors", [])
            self.model_used = data.get("model_used")

        def model_dump(self):
            content = self._data.get("content", self._data.get("result", ""))
            verdict = self._data.get("verdict", "")
            # Build text for content-based routing (extract_verdict).
            # If a verdict key is present, prepend it so extract_verdict finds it.
            text_parts = []
            if verdict:
                text_parts.append(verdict)
            if content:
                text_parts.append(content)
            text = "\n".join(text_parts) if text_parts else ""
            return {
                "content": content,
                "verdict": verdict,
                "status": self._data.get("status", "completed"),
                "text": text,
                "result": {"text": text},
                "state": "success",
                **self._data,
            }

        # Pydantic v1 compat
        def dict(self):
            return self.model_dump()

    class _FakeState:
        def __init__(self, data: dict):
            status = data.get("status", "completed")
            # Map status strings to TaskState
            if status in ("completed", "success"):
                self.value = "success"
            else:
                self.value = "failed"

        def __eq__(self, other):
            # Handle TaskState (str enum): its value attr holds the string
            if hasattr(other, "value"):
                return self.value == other.value
            # Handle plain strings
            return self.value == other

        def __hash__(self):
            return hash(self.value)

        def __str__(self):
            return self.value

        def __repr__(self):
            return f"_FakeState({self.value!r})"

    class _FakeRunner:
        def __init__(self, callable_fn):
            self.queue = _FakeQueue()
            self.executors = [_FakeExecutor(callable_fn)]

    return _FakeRunner(fn)


# ---------------------------------------------------------------------------
# EPIC #942 953b — stateless @staticmethods relocated off PhaseSequencer /
# StateMachineSequencer. These were PURE staticmethods (no self/cls use), moved
# here VERBATIM as module-level free functions. The classes keep them resolving
# at every historical call site via a class-level ``staticmethod(...)`` alias
# (e.g. ``_parse_supervisor_response = staticmethod(_parse_supervisor_response)``),
# so ``self._x(...)`` / ``ClassName._x(...)`` and the test imports of the free
# names stay byte-identical.
# ---------------------------------------------------------------------------


def _parse_supervisor_response(text: str):
    """Parse supervisor text response for APPROVE / REVISE / ABORT.

    Scans each line and checks its first word (case-insensitive).
    Returns ``("APPROVE" | "REVISE" | "ABORT" | "UNKNOWN", reason_str)``.
    On no match defaults to APPROVE with a warning.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        for verdict in ("APPROVE", "REVISE", "ABORT"):
            if upper.startswith(verdict):
                # Extract reason after the keyword (and optional colon/space)
                remainder = stripped[len(verdict) :].lstrip(":").strip()
                return verdict, remainder
    logger.warning(
        f"Supervisor response had no APPROVE/REVISE/ABORT verdict; "
        f"defaulting to APPROVE. Response preview: {text[:200]!r}"
    )
    return "APPROVE", "no verdict found — defaulting to APPROVE"


def _load_skill(  # noqa: C901
    skill_ref: str, template_dir: Optional[Path] = None
) -> Tuple[str, str]:
    """Load a skill file, stripping YAML frontmatter.

    Resolves ``skill_ref`` in this order:
    1. Absolute path (if given) — must be under ``~/.orch/skills/``
    2. Relative to ``template_dir`` (if provided)
    3. ``~/.orch/skills/``

    Path traversal protection: the resolved path must lie within one of the
    permitted directories.  Absolute paths are restricted to ``~/.orch/skills/``
    only; relative paths may also resolve within ``template_dir``.

    Args:
        skill_ref:    Path string from the ``skill_refs`` list.
        template_dir: Directory of the template file (for relative resolution).

    Returns:
        ``(skill_name, skill_content)`` where ``skill_name`` comes from the
        frontmatter ``name:`` field or the filename stem, and
        ``skill_content`` is the body text with frontmatter stripped.

    Raises:
        FileNotFoundError: If the skill file cannot be located.
        ValueError: If the resolved path escapes the allowed directories
                    (path traversal protection).
    """
    skill_path = Path(skill_ref)
    global_skills_dir = (Path.home() / ".orch" / "skills").resolve()

    # Build the set of allowed root directories.
    # Absolute skill_refs are only permitted under the global skills dir.
    # Relative skill_refs may also resolve within template_dir.
    if skill_path.is_absolute():
        allowed_dirs = [global_skills_dir]
    else:
        allowed_dirs = [global_skills_dir]
        if template_dir is not None:
            allowed_dirs.append(template_dir.resolve())

    # Resolve to an existing file
    resolved: Optional[Path] = None
    if skill_path.is_absolute():
        if skill_path.exists():
            resolved = skill_path.resolve()
    else:
        if template_dir is not None:
            candidate = template_dir / skill_path
            if candidate.exists():
                resolved = candidate.resolve()
        if resolved is None:
            candidate_global = global_skills_dir / skill_path
            if candidate_global.exists():
                resolved = candidate_global

    if resolved is None:
        raise FileNotFoundError(
            f"Skill file '{skill_ref}' not found " f"(template_dir={template_dir}, ~/.orch/skills/)"
        )

    # --- Path traversal protection -----------------------------------
    resolved_real = resolved.resolve()
    if not any(_is_within_dir(resolved_real, d) for d in allowed_dirs):
        raise ValueError(
            f"Skill path '{skill_ref}' resolves to '{resolved_real}', which is "
            f"outside the allowed directories: "
            f"{[str(d) for d in allowed_dirs]}. "
            f"Relative skill_refs must stay within the template directory or "
            f"~/.orch/skills/; absolute paths must be under ~/.orch/skills/."
        )

    raw = resolved_real.read_text(encoding="utf-8")

    # Strip YAML frontmatter: text between --- delimiters at start of file
    frontmatter_data: Dict[str, Any] = {}
    body = raw
    if raw.startswith("---"):
        # Find closing ---
        end_match = re.search(r"\n---[ \t]*(?:\n|$)", raw[3:])
        if end_match:
            fm_text = raw[3 : 3 + end_match.start()]
            body = raw[3 + end_match.end() :]
            try:
                frontmatter_data = yaml.safe_load(fm_text) or {}
            except Exception:  # noqa: BLE001
                frontmatter_data = {}

    # Skill name: prefer frontmatter 'name:', else filename stem
    skill_name: str = str(frontmatter_data.get("name", "")).strip() or resolved_real.stem

    return skill_name, body.strip()


def _sanitize_error_for_prompt(error: str) -> str:
    """Strip Python tracebacks and ANSI codes from an error string, then truncate.

    Intended to produce a clean, concise error message suitable for
    inclusion in an LLM retry prompt.

    Processing steps:
    1. Strip ANSI escape codes (colour codes, cursor movement, etc.)
    2. Remove Python traceback blocks — keeps only the final exception line
       (e.g. ``ValueError: something``).
    3. Truncate the result to 500 characters, appending ``"..."`` when
       truncation occurs.

    Args:
        error: Raw error string (may contain tracebacks and/or ANSI codes).

    Returns:
        Sanitized string of at most 500 characters.
    """
    # 1. Remove ANSI escape codes
    ansi_escape = re.compile(r"\x1b\[[0-9;]*[mGKHFJsr]")
    error = ansi_escape.sub("", error)

    # 2. Strip Python traceback blocks.
    #    A traceback starts with "Traceback (most recent call last):" and
    #    the body consists of indented lines ("  File ...", "    code...").
    #    The block ends at the first non-indented line which is the actual
    #    exception type/message (e.g. "ValueError: foo").  We skip the
    #    traceback body and keep only that final exception line.
    lines = error.splitlines()
    in_traceback = False
    filtered: List[str] = []
    for line in lines:
        if line.strip().startswith("Traceback (most recent call last):"):
            in_traceback = True
            continue
        if in_traceback:
            # Traceback body: lines indented with spaces or tabs
            if line.startswith("  ") or line.startswith("\t"):
                continue
            else:
                # Non-indented line → the exception class/message line
                in_traceback = False
                filtered.append(line)
        else:
            filtered.append(line)

    error = "\n".join(filtered).strip()

    # 3. Truncate to 500 chars
    if len(error) > 500:
        error = error[:497] + "..."

    return error


def _format_failure_context(attempt: int, error: str, partial_output: str) -> str:
    """Return a markdown-formatted failure context block for LLM retry prompts.

    The returned string is injected into the phase prompt on the *next*
    attempt (via the ``{failure_context}`` placeholder) so that the LLM
    can see what went wrong and try a different approach.

    Partial output is truncated to 1 000 characters inside this method;
    pass the full partial output here and store it separately in
    ``retry_history`` if you need the untruncated version.

    Args:
        attempt:        The 1-based attempt number that failed.
        error:          Sanitized error message (output of
                        :meth:`_sanitize_error_for_prompt`).
        partial_output: Any text produced by the failed attempt before it
                        errored.  May be an empty string.

    Returns:
        Markdown string ready for injection into a prompt template.
    """
    display_partial = partial_output[:1000] if partial_output else "(none)"

    return (
        f"## Previous Attempt Failed\n\n"
        f"**Attempt:** {attempt}\n"
        f"**Error:** {error}\n\n"
        f"**Partial Output:**\n"
        f"{display_partial}\n\n"
        f"Please review the above failure and try a different approach."
    )


def _resolve_task_type(task_type_str: str) -> TaskType:
    """Map a string task type to a TaskType enum, defaulting to CONTENT."""
    try:
        return TaskType(task_type_str.lower())
    except ValueError:
        logger.warning(f"Unknown task_type '{task_type_str}'; defaulting to 'content'")
        return TaskType.CONTENT


def _resolve_model_tier(
    model_tier_str: str,
    min_tier: Optional[str] = None,
    max_tier: Optional[str] = None,
):
    """Map a friendly model tier name to a ModelTier enum value.

    The PhaseDefinition uses short names (haiku, sonnet, opus) while
    the schema uses versioned names (haiku-4-5, sonnet-4, opus-4-6).
    Delegates to the canonical model_registry (#916) — the single
    short↔versioned bridge. Returns None if the tier is not recognised
    (runner will use its default).

    #987: when *min_tier*/*max_tier* are given, the resolved tier is clamped
    into the inclusive band [min_tier, max_tier] (haiku<sonnet<opus). A
    ``None`` bound is unbounded on that side; with both ``None`` this is a
    no-op (byte-identical default). Clamping is applied ONLY when the phase's
    own tier resolved to a concrete ModelTier — an unresolved tier (None,
    "use runner default") is returned unchanged so the floor never
    manufactures a model where the author asked for the runner default.
    """
    from ..model_registry import clamp_tier, resolve_tier  # noqa: PLC0415

    resolved = resolve_tier(model_tier_str)
    if resolved is None:
        if model_tier_str:
            logger.debug(f"Unrecognised model_tier '{model_tier_str}'; using runner default")
        return None
    lo = resolve_tier(min_tier) if min_tier else None
    hi = resolve_tier(max_tier) if max_tier else None
    return clamp_tier(resolved, lo, hi)


def _safe_call_hook(hook, *args, pipeline_id: str = "") -> None:
    """Call *hook* with *args*, logging but swallowing all exceptions.

    Relocated from :class:`StateMachineSequencer` (EPIC #942 953b) — a pure
    helper with no instance/class state. The class keeps a
    ``staticmethod(...)`` alias so ``self._safe_call_hook(...)`` resolves
    byte-identically. Ensures a misbehaving ``on_pipeline_complete`` callback
    never crashes the pipeline.
    """
    if hook is None:
        return
    try:
        hook(*args)
    except Exception as hook_exc:  # noqa: BLE001
        logger.warning(f"Pipeline {pipeline_id}: on_pipeline_complete hook failed: {hook_exc}")
