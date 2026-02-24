"""Keyword grader — checks for keyword presence in pipeline output.

Supports three matching modes:

* ``"all"``   — every keyword must appear → binary score (1.0 or 0.0)
* ``"any"``   — at least one keyword must appear → binary score (1.0 or 0.0)
* ``"ratio"`` — (matched / total) as a float in [0.0, 1.0]

Matching is case-insensitive in all modes.  The grader recursively extracts
all string content from the output dict (including nested dicts and lists),
so it works equally well with flat ``{"article": "..."}`` outputs and deeply
nested phase-output structures from the pipeline runner.

Example usage::

    grader = KeywordGrader()
    result = grader.grade(
        output={"article": "The quick brown fox"},
        keywords=["fox", "quick"],
        match_mode="all",
    )
    # result.score == 1.0, result.passed == True
"""

from __future__ import annotations

from typing import Any, List, Optional

from ..models import GradeResult


def _extract_text(value: Any) -> str:
    """Recursively extract all textual content from *value*.

    Handles nested dicts, lists/tuples, and scalar values (converted via
    ``str()``).  Enum instances (e.g. ``TaskState.SUCCESS``) are also
    converted via their string representation, so ``"success"`` will be
    searchable.

    Parameters
    ----------
    value:
        Any Python object — dict, list, string, int, Enum, etc.

    Returns
    -------
    str
        A single space-joined string of all leaf values found.
    """
    if isinstance(value, dict):
        parts = [_extract_text(v) for v in value.values()]
        return " ".join(p for p in parts if p)
    if isinstance(value, (list, tuple)):
        parts = [_extract_text(item) for item in value]
        return " ".join(p for p in parts if p)
    # Scalars (str, int, float, bool, Enum, None, …)
    text = str(value) if value is not None else ""
    return text


class KeywordGrader:
    """Grades pipeline output by checking for keyword presence.

    This grader does **not** require an API key and is therefore always safe
    to run in CI / dry-run mode.
    """

    def grade(
        self,
        output: dict,
        keywords: List[str],
        match_mode: str = "all",
        output_field: Optional[str] = None,
    ) -> GradeResult:
        """Grade *output* against *keywords* using *match_mode*.

        Parameters
        ----------
        output:
            The pipeline output dict (may be deeply nested).
        keywords:
            List of keyword strings to search for (case-insensitive).
        match_mode:
            One of ``"all"``, ``"any"``, or ``"ratio"``.
        output_field:
            Optional top-level key to restrict the search to (e.g.
            ``"article"``).  When ``None`` (the default), all content in
            *output* is searched recursively.

        Returns
        -------
        GradeResult
            * ``score = 1.0 / 0.0`` for ``"all"`` and ``"any"`` modes.
            * ``score`` in ``[0.0, 1.0]`` for ``"ratio"`` mode.
            * ``grader_type = "keyword"``

        Raises
        ------
        ValueError
            If *match_mode* is not one of the supported values.
        """
        valid_modes = {"all", "any", "ratio"}
        if match_mode not in valid_modes:
            return GradeResult(
                passed=False,
                score=0.0,
                details=f"Unknown match_mode '{match_mode}'. "
                        f"Valid values: {sorted(valid_modes)}",
                grader_type="keyword",
            )

        # --- Extract searchable text ---
        if output_field is not None:
            raw = output.get(output_field, "")
            search_text = _extract_text(raw)
        else:
            search_text = _extract_text(output)

        search_lower = search_text.lower()

        # --- Match keywords ---
        if not keywords:
            # Vacuous truth: no keywords to check → always passes
            return GradeResult(
                passed=True,
                score=1.0,
                details="No keywords specified — vacuous pass.",
                grader_type="keyword",
            )

        matched = [kw for kw in keywords if kw.lower() in search_lower]
        total = len(keywords)
        matched_count = len(matched)
        not_matched = [kw for kw in keywords if kw.lower() not in search_lower]

        # --- Compute score based on mode ---
        if match_mode == "all":
            passed = matched_count == total
            score = 1.0 if passed else 0.0
            detail_msg = (
                f"All {total} keyword(s) found: {matched}"
                if passed
                else f"Missing {len(not_matched)} keyword(s): {not_matched}; "
                     f"found: {matched}"
            )

        elif match_mode == "any":
            passed = matched_count > 0
            score = 1.0 if passed else 0.0
            detail_msg = (
                f"At least one keyword found: {matched}"
                if passed
                else f"None of the keywords found: {keywords}"
            )

        else:  # ratio
            score = matched_count / total
            passed = score > 0.0  # ratio mode: any match is a pass
            detail_msg = (
                f"{matched_count}/{total} keyword(s) matched "
                f"(score={score:.3f}): found={matched}, missing={not_matched}"
            )

        return GradeResult(
            passed=passed,
            score=score,
            details=detail_msg,
            grader_type="keyword",
        )
