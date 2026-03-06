"""Composite confidence scoring model for orchestration pipeline runs.

This module provides a 3-tier ConfidenceLevel enum (HIGH/MEDIUM/LOW) and a
ConfidenceCalculator that aggregates multiple signals from a pipeline output
directory into a single composite score.

NOTE: This ConfidenceLevel is distinct from schemas.ConfidenceLevel, which is
a 5-tier (VERY_LOW->VERY_HIGH) enum scoped to individual task results. This
module's ConfidenceLevel is scoped to a full pipeline run and maps to three
coarse bands used for downstream routing and reporting.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Default signal weights (must sum <= 1.0; renormalized at runtime)
# ---------------------------------------------------------------------------
DEFAULT_WEIGHTS: dict[str, float] = {
    "llm_judge": 0.4,
    "test_pass_rate": 0.3,
    "review_quality": 0.2,
    "change_complexity": 0.1,
}


# ---------------------------------------------------------------------------
# Enums & data structures
# ---------------------------------------------------------------------------

class ConfidenceLevel(str, Enum):
    """3-tier confidence level for a full pipeline run.

    Distinct from ``schemas.ConfidenceLevel`` (5-tier, task-scoped).
    Thresholds:
        HIGH   >= 0.90
        MEDIUM >= 0.75
        LOW     < 0.75
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class ConfidenceSignal:
    """A single scored signal contributing to the composite confidence.

    Attributes:
        name: Logical name of the signal (e.g. "llm_judge").
        value: Normalised score in [0, 1] -- clamped automatically.
        weight: Non-negative weight used in the weighted average.
        raw_value: The original, un-normalised value before clamping/mapping.
        source: Human-readable origin description (file + key).
    """

    name: str
    value: float
    weight: float
    raw_value: Any
    source: str

    def __post_init__(self) -> None:
        if self.weight < 0:
            raise ValueError(
                f"Signal '{self.name}' weight must be >= 0, got {self.weight}"
            )
        # Clamp value to [0, 1]
        self.value = max(0.0, min(1.0, float(self.value)))


@dataclass
class ConfidenceResult:
    """Aggregated confidence result for a pipeline run.

    Attributes:
        signals: All signals that were successfully extracted.
        composite_score: Weighted average of signal values in [0, 1].
        confidence_level: Coarse tier mapped from composite_score.
        explanation: Human-readable breakdown of contributing signals.
    """

    signals: list[ConfidenceSignal] = field(default_factory=list)
    composite_score: float = 0.0
    confidence_level: ConfidenceLevel = ConfidenceLevel.LOW
    explanation: str = ""


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------

class ConfidenceCalculator:
    """Computes composite confidence from pipeline output artefacts.

    Args:
        weights: Optional override dict merged with DEFAULT_WEIGHTS.
                 Unknown keys extend the weight table; known keys override.
    """

    def __init__(self, weights: Optional[dict[str, float]] = None) -> None:
        self.weights: dict[str, float] = {**DEFAULT_WEIGHTS}
        if weights:
            self.weights.update(weights)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_confidence(self, output_dir: Path) -> ConfidenceResult:
        """Aggregate signals from *output_dir* into a ConfidenceResult."""
        extractors = [
            self._extract_llm_judge,
            self._extract_test_pass_rate,
            self._extract_review_quality,
            self._extract_change_complexity,
        ]

        signals: list[ConfidenceSignal] = []
        for extractor in extractors:
            try:
                sig = extractor(output_dir)
                if sig is not None:
                    signals.append(sig)
            except Exception:
                pass

        composite = self._weighted_average(signals)
        level = self._map_level(composite)
        explanation = self._build_explanation(signals, composite, level)

        return ConfidenceResult(
            signals=signals,
            composite_score=composite,
            confidence_level=level,
            explanation=explanation,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_json(path: Path) -> Optional[dict]:
        """Return parsed JSON dict or None on any error."""
        try:
            with path.open() as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
            return None
        except Exception:
            return None

    def _weighted_average(self, signals: list[ConfidenceSignal]) -> float:
        """Compute renormalised weighted average over present signals."""
        if not signals:
            return 0.0

        total_weight = sum(
            self.weights.get(s.name, s.weight) for s in signals
        )
        if total_weight == 0.0:
            return sum(s.value for s in signals) / len(signals)

        score = sum(
            s.value * self.weights.get(s.name, s.weight) for s in signals
        )
        return score / total_weight

    @staticmethod
    def _map_level(score: float) -> ConfidenceLevel:
        if score >= 0.90:
            return ConfidenceLevel.HIGH
        if score >= 0.75:
            return ConfidenceLevel.MEDIUM
        return ConfidenceLevel.LOW

    @staticmethod
    def _build_explanation(
        signals: list[ConfidenceSignal],
        composite: float,
        level: ConfidenceLevel,
    ) -> str:
        lines = [
            f"Composite score: {composite:.4f} -> {level.value.upper()}"
        ]
        if signals:
            lines.append("Signals:")
            for s in signals:
                lines.append(
                    f"  [{s.name}] value={s.value:.4f}  "
                    f"weight={s.weight:.2f}  source={s.source}"
                )
        else:
            lines.append("No signals extracted -- defaulting to LOW.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Signal extractors
    # ------------------------------------------------------------------

    def _extract_llm_judge(self, output_dir: Path) -> Optional[ConfidenceSignal]:
        """Extract LLM judge score from _final_output.json or review.json."""
        score: Optional[float] = None
        source = ""

        final_path = output_dir / "_final_output.json"
        data = self._load_json(final_path)
        if data is not None:
            for key in ("score", "weighted_score", "llm_judge_score"):
                if key in data:
                    score = data[key]
                    source = f"_final_output.json[{key}]"
                    break

        if score is None:
            review_path = output_dir / "review.json"
            data = self._load_json(review_path)
            if data is not None and "score" in data:
                score = data["score"]
                source = "review.json[score]"

        if score is None:
            return None

        try:
            raw = score
            clamped = max(0.0, min(1.0, float(score)))
        except (TypeError, ValueError):
            return None

        return ConfidenceSignal(
            name="llm_judge",
            value=clamped,
            weight=self.weights.get("llm_judge", DEFAULT_WEIGHTS["llm_judge"]),
            raw_value=raw,
            source=source,
        )

    def _extract_test_pass_rate(self, output_dir: Path) -> Optional[ConfidenceSignal]:
        """Extract test pass rate from test.json (pytest stdout or exit_code)."""
        test_path = output_dir / "test.json"
        if not test_path.exists():
            return None

        data = self._load_json(test_path)
        if data is None:
            return None

        raw_rate: Any = None
        source = ""
        rate: float

        # Try to parse pytest summary from stdout
        stdout = data.get("stdout", "")
        if stdout:
            passed = 0
            failed = 0
            errors = 0

            m = re.search(r"(\d+)\s+passed", stdout)
            if m:
                passed = int(m.group(1))
            m = re.search(r"(\d+)\s+failed", stdout)
            if m:
                failed = int(m.group(1))
            m = re.search(r"(\d+)\s+error", stdout)
            if m:
                errors = int(m.group(1))

            total = passed + failed + errors
            if total > 0:
                rate = passed / total
                raw_rate = {"passed": passed, "failed": failed, "errors": errors}
                source = "test.json[stdout] pytest summary"
                return ConfidenceSignal(
                    name="test_pass_rate",
                    value=rate,
                    weight=self.weights.get(
                        "test_pass_rate", DEFAULT_WEIGHTS["test_pass_rate"]
                    ),
                    raw_value=raw_rate,
                    source=source,
                )

        # Fallback: exit_code
        exit_code = data.get("exit_code")
        if exit_code is not None:
            rate = 1.0 if exit_code == 0 else 0.0
            source = f"test.json[exit_code={exit_code}]"
            return ConfidenceSignal(
                name="test_pass_rate",
                value=rate,
                weight=self.weights.get(
                    "test_pass_rate", DEFAULT_WEIGHTS["test_pass_rate"]
                ),
                raw_value=exit_code,
                source=source,
            )

        return None

    def _extract_review_quality(self, output_dir: Path) -> Optional[ConfidenceSignal]:
        """Extract review quality from review.json verdict + fix file count."""
        review_path = output_dir / "review.json"
        if not review_path.exists():
            return None

        data = self._load_json(review_path)
        if data is None:
            return None

        # Count fix*.json files in output_dir
        fix_files = list(output_dir.glob("fix*.json"))
        deduction = 0.15 * len(fix_files)
        quality = max(0.0, 1.0 - deduction)

        verdict = data.get("verdict", "")
        source = (
            f"review.json[verdict={verdict!r}] "
            f"+ {len(fix_files)} fix file(s)"
        )

        return ConfidenceSignal(
            name="review_quality",
            value=quality,
            weight=self.weights.get(
                "review_quality", DEFAULT_WEIGHTS["review_quality"]
            ),
            raw_value={"verdict": verdict, "fix_files": len(fix_files)},
            source=source,
        )

    def _extract_change_complexity(self, output_dir: Path) -> Optional[ConfidenceSignal]:
        """Extract change complexity score from implement.json or spec.json."""
        count: Optional[int] = None
        source = ""

        implement_path = output_dir / "implement.json"
        data = self._load_json(implement_path)
        if data is not None:
            if "files_changed" in data:
                try:
                    count = int(data["files_changed"])
                    source = "implement.json[files_changed]"
                except (TypeError, ValueError):
                    pass
            elif "changed_files" in data:
                cf = data["changed_files"]
                if isinstance(cf, list):
                    count = len(cf)
                    source = "implement.json[changed_files] (list)"

        if count is None:
            spec_path = output_dir / "spec.json"
            data = self._load_json(spec_path)
            if data is not None and "files_to_modify" in data:
                ftm = data["files_to_modify"]
                if isinstance(ftm, list):
                    count = len(ftm)
                    source = "spec.json[files_to_modify]"
                else:
                    try:
                        count = int(ftm)
                        source = "spec.json[files_to_modify]"
                    except (TypeError, ValueError):
                        pass

        if count is None:
            return None

        score = 1.0 - min(count / 10.0, 1.0)

        return ConfidenceSignal(
            name="change_complexity",
            value=score,
            weight=self.weights.get(
                "change_complexity", DEFAULT_WEIGHTS["change_complexity"]
            ),
            raw_value=count,
            source=source,
        )
