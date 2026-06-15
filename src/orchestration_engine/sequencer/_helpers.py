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
from typing import Any

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
