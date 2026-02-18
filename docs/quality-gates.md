# Quality Gates

Quality gates are **automated verification checkpoints** that ensure task outputs meet defined standards before proceeding. Each task type has specific quality gates with measurable criteria and thresholds.

## Quality Gate Architecture

```python
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any, Tuple
from enum import Enum

class GateResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"
    SKIP = "skip"

class QualityCheck(BaseModel):
    """Individual quality check within a gate."""
    name: str
    description: str
    weight: float = Field(ge=0.0, le=1.0)  # Contribution to overall gate score
    threshold: float = Field(ge=0.0, le=1.0)  # Pass threshold
    critical: bool = False  # If true, failure blocks progression
    
class QualityGateResult(BaseModel):
    """Result from running a quality gate."""
    gate_id: str
    result: GateResult
    overall_score: float = Field(ge=0.0, le=1.0)
    individual_checks: List[Dict[str, Any]]
    blocking_issues: List[str] = []
    warnings: List[str] = []
    recommendations: List[str] = []
    execution_time_seconds: float
    
class QualityGate(ABC):
    """Base class for all quality gates."""
    
    def __init__(self, gate_id: str, name: str, description: str):
        self.gate_id = gate_id
        self.name = name
        self.description = description
        self.checks: List[QualityCheck] = []
        
    @abstractmethod
    def evaluate(self, task_result: TaskResult) -> QualityGateResult:
        """Evaluate task result against quality criteria."""
        pass
        
    def add_check(self, check: QualityCheck):
        """Add a quality check to this gate."""
        self.checks.append(check)
        
    def calculate_weighted_score(self, check_scores: Dict[str, float]) -> float:
        """Calculate weighted average of check scores."""
        total_score = sum(
            check_scores.get(check.name, 0.0) * check.weight 
            for check in self.checks
        )
        total_weight = sum(check.weight for check in self.checks)
        return total_score / total_weight if total_weight > 0 else 0.0
```

## Code Quality Gates

### 1. Build Success Gate

```python
class BuildSuccessGate(QualityGate):
    """Verifies code builds successfully."""
    
    def __init__(self):
        super().__init__(
            gate_id="build_success",
            name="Build Success Verification",
            description="Ensures code compiles/builds without errors"
        )
        
        self.add_check(QualityCheck(
            name="compilation_success",
            description="Code compiles without errors",
            weight=0.6,
            threshold=1.0,
            critical=True
        ))
        
        self.add_check(QualityCheck(
            name="build_time",
            description="Build completes within reasonable time",
            weight=0.2,
            threshold=0.7
        ))
        
        self.add_check(QualityCheck(
            name="warnings_count",
            description="Minimal build warnings",
            weight=0.2,
            threshold=0.8
        ))
        
    def evaluate(self, task_result: CodeTaskResult) -> QualityGateResult:
        build_result = task_result.result.build_result
        if not build_result:
            return QualityGateResult(
                gate_id=self.gate_id,
                result=GateResult.SKIP,
                overall_score=0.0,
                individual_checks=[],
                execution_time_seconds=0.0
            )
            
        start_time = time.time()
        
        # Check compilation success
        compilation_score = 1.0 if build_result.success else 0.0
        
        # Check build time (assume reasonable build time is < 60 seconds)
        time_score = max(0.0, 1.0 - (build_result.build_time_seconds - 60) / 120) if build_result.build_time_seconds > 60 else 1.0
        
        # Check warnings (penalize excessive warnings)
        warning_count = build_result.stderr.count("warning:")
        warnings_score = max(0.0, 1.0 - warning_count * 0.1)
        
        check_scores = {
            "compilation_success": compilation_score,
            "build_time": time_score,
            "warnings_count": warnings_score
        }
        
        overall_score = self.calculate_weighted_score(check_scores)
        
        # Determine result
        result = GateResult.PASS
        blocking_issues = []
        
        if compilation_score < 1.0:
            result = GateResult.FAIL
            blocking_issues.append("Compilation failed")
            
        execution_time = time.time() - start_time
        
        return QualityGateResult(
            gate_id=self.gate_id,
            result=result,
            overall_score=overall_score,
            individual_checks=[
                {"name": "compilation_success", "score": compilation_score, "passed": compilation_score >= 1.0},
                {"name": "build_time", "score": time_score, "passed": time_score >= 0.7},
                {"name": "warnings_count", "score": warnings_score, "passed": warnings_score >= 0.8}
            ],
            blocking_issues=blocking_issues,
            warnings=["Excessive build warnings"] if warnings_score < 0.8 else [],
            execution_time_seconds=execution_time
        )
```

### 2. Unit Test Gate

```python
class UnitTestGate(QualityGate):
    """Verifies unit tests pass with adequate coverage."""
    
    def __init__(self, min_coverage: float = 0.8):
        super().__init__(
            gate_id="unit_tests",
            name="Unit Test Verification",
            description="Ensures unit tests pass with adequate coverage"
        )
        
        self.min_coverage = min_coverage
        
        self.add_check(QualityCheck(
            name="test_pass_rate",
            description="Percentage of tests that pass",
            weight=0.5,
            threshold=1.0,
            critical=True
        ))
        
        self.add_check(QualityCheck(
            name="test_coverage",
            description="Code coverage percentage",
            weight=0.3,
            threshold=min_coverage
        ))
        
        self.add_check(QualityCheck(
            name="test_completeness",
            description="Adequate number of tests",
            weight=0.2,
            threshold=0.7
        ))
        
    def evaluate(self, task_result: CodeTaskResult) -> QualityGateResult:
        test_result = task_result.result.test_result
        if not test_result:
            return QualityGateResult(
                gate_id=self.gate_id,
                result=GateResult.SKIP,
                overall_score=0.0,
                individual_checks=[],
                execution_time_seconds=0.0
            )
            
        start_time = time.time()
        
        # Test pass rate
        pass_rate = test_result.tests_passed / max(test_result.tests_run, 1)
        
        # Coverage score
        coverage_score = (test_result.coverage_percentage or 0.0) / 100.0
        
        # Test completeness (heuristic: at least 1 test per 50 lines of code)
        expected_tests = max(1, task_result.result.lines_of_code / 50)
        completeness_score = min(1.0, test_result.tests_run / expected_tests)
        
        check_scores = {
            "test_pass_rate": pass_rate,
            "test_coverage": coverage_score,
            "test_completeness": completeness_score
        }
        
        overall_score = self.calculate_weighted_score(check_scores)
        
        # Determine result
        result = GateResult.PASS
        blocking_issues = []
        warnings = []
        
        if pass_rate < 1.0:
            result = GateResult.FAIL
            blocking_issues.append(f"{test_result.tests_failed} tests failed")
            
        if coverage_score < self.min_coverage:
            warnings.append(f"Test coverage {coverage_score*100:.1f}% below threshold {self.min_coverage*100:.1f}%")
            if result == GateResult.PASS:
                result = GateResult.WARNING
                
        execution_time = time.time() - start_time
        
        return QualityGateResult(
            gate_id=self.gate_id,
            result=result,
            overall_score=overall_score,
            individual_checks=[
                {"name": "test_pass_rate", "score": pass_rate, "passed": pass_rate >= 1.0},
                {"name": "test_coverage", "score": coverage_score, "passed": coverage_score >= self.min_coverage},
                {"name": "test_completeness", "score": completeness_score, "passed": completeness_score >= 0.7}
            ],
            blocking_issues=blocking_issues,
            warnings=warnings,
            execution_time_seconds=execution_time
        )
```

### 3. Code Quality Gate

```python
class CodeQualityGate(QualityGate):
    """Verifies code quality through static analysis."""
    
    def __init__(self):
        super().__init__(
            gate_id="code_quality",
            name="Code Quality Analysis",
            description="Static analysis for code quality, complexity, and maintainability"
        )
        
        self.add_check(QualityCheck(
            name="lint_cleanliness",
            description="Code passes linting rules",
            weight=0.3,
            threshold=0.9
        ))
        
        self.add_check(QualityCheck(
            name="complexity_score",
            description="Code complexity within acceptable limits",
            weight=0.25,
            threshold=0.7
        ))
        
        self.add_check(QualityCheck(
            name="maintainability",
            description="Code maintainability index",
            weight=0.25,
            threshold=0.7
        ))
        
        self.add_check(QualityCheck(
            name="security_clean",
            description="No security vulnerabilities detected",
            weight=0.2,
            threshold=0.95,
            critical=True
        ))
        
    def evaluate(self, task_result: CodeTaskResult) -> QualityGateResult:
        start_time = time.time()
        
        # Lint cleanliness
        lint_score = 1.0
        if task_result.result.lint_result:
            lint = task_result.result.lint_result
            if not lint.clean:
                # Penalize errors more than warnings
                penalty = lint.errors * 0.2 + lint.warnings * 0.05
                lint_score = max(0.0, 1.0 - penalty)
                
        # Complexity score (inverse of complexity - lower complexity is better)
        complexity_score = 0.8  # Default if not available
        if task_result.result.complexity_score:
            # Assume complexity_score is 0-100, with 100 being very complex
            complexity_score = max(0.0, 1.0 - task_result.result.complexity_score / 100.0)
            
        # Maintainability 
        maintainability_score = 0.8  # Default
        if task_result.result.maintainability_index:
            maintainability_score = task_result.result.maintainability_index / 100.0
            
        # Security
        security_score = 1.0
        if task_result.result.security_scan:
            security_score = 1.0 if task_result.result.security_scan.clean else 0.0
            
        check_scores = {
            "lint_cleanliness": lint_score,
            "complexity_score": complexity_score,
            "maintainability": maintainability_score,
            "security_clean": security_score
        }
        
        overall_score = self.calculate_weighted_score(check_scores)
        
        # Determine result
        result = GateResult.PASS
        blocking_issues = []
        warnings = []
        
        if security_score < 0.95:
            result = GateResult.FAIL
            blocking_issues.append("Security vulnerabilities detected")
            
        if lint_score < 0.9:
            warnings.append("Code has linting issues")
            if result == GateResult.PASS:
                result = GateResult.WARNING
                
        execution_time = time.time() - start_time
        
        return QualityGateResult(
            gate_id=self.gate_id,
            result=result,
            overall_score=overall_score,
            individual_checks=[
                {"name": "lint_cleanliness", "score": lint_score, "passed": lint_score >= 0.9},
                {"name": "complexity_score", "score": complexity_score, "passed": complexity_score >= 0.7},
                {"name": "maintainability", "score": maintainability_score, "passed": maintainability_score >= 0.7},
                {"name": "security_clean", "score": security_score, "passed": security_score >= 0.95}
            ],
            blocking_issues=blocking_issues,
            warnings=warnings,
            execution_time_seconds=execution_time
        )
```

## Content Quality Gates

### 4. Fact-Check Gate

```python
class FactCheckGate(QualityGate):
    """Verifies content accuracy through fact-checking."""
    
    def __init__(self, min_accuracy: float = 0.9):
        super().__init__(
            gate_id="fact_accuracy",
            name="Fact Verification",
            description="Verifies factual claims in content"
        )
        
        self.min_accuracy = min_accuracy
        
        self.add_check(QualityCheck(
            name="claim_accuracy",
            description="Percentage of claims that are accurate",
            weight=0.5,
            threshold=min_accuracy,
            critical=True
        ))
        
        self.add_check(QualityCheck(
            name="citation_validity",
            description="Citations are accessible and accurate",
            weight=0.3,
            threshold=0.95
        ))
        
        self.add_check(QualityCheck(
            name="source_credibility",
            description="Sources are credible and authoritative",
            weight=0.2,
            threshold=0.8
        ))
        
    def evaluate(self, task_result: ContentTaskResult) -> QualityGateResult:
        fact_check = task_result.result.fact_check
        if not fact_check:
            # If no fact-check data, run basic fact verification
            return self._run_basic_fact_check(task_result)
            
        start_time = time.time()
        
        # Claim accuracy
        accuracy_score = fact_check.overall_accuracy
        
        # Citation validity (check if URLs are accessible)
        citation_validity = self._verify_citations(task_result.result.citations)
        
        # Source credibility (analyze source domains and reputation)
        source_credibility = self._assess_source_credibility(task_result.result.citations)
        
        check_scores = {
            "claim_accuracy": accuracy_score,
            "citation_validity": citation_validity,
            "source_credibility": source_credibility
        }
        
        overall_score = self.calculate_weighted_score(check_scores)
        
        # Determine result
        result = GateResult.PASS
        blocking_issues = []
        warnings = []
        
        if accuracy_score < self.min_accuracy:
            result = GateResult.FAIL
            blocking_issues.extend(fact_check.disputed_claims)
            
        if citation_validity < 0.95:
            warnings.append("Some citations may not be accessible")
            if result == GateResult.PASS:
                result = GateResult.WARNING
                
        execution_time = time.time() - start_time
        
        return QualityGateResult(
            gate_id=self.gate_id,
            result=result,
            overall_score=overall_score,
            individual_checks=[
                {"name": "claim_accuracy", "score": accuracy_score, "passed": accuracy_score >= self.min_accuracy},
                {"name": "citation_validity", "score": citation_validity, "passed": citation_validity >= 0.95},
                {"name": "source_credibility", "score": source_credibility, "passed": source_credibility >= 0.8}
            ],
            blocking_issues=blocking_issues,
            warnings=warnings,
            execution_time_seconds=execution_time
        )
        
    def _verify_citations(self, citations: List[Citation]) -> float:
        """Verify that cited URLs are accessible."""
        if not citations:
            return 1.0
            
        accessible_count = 0
        for citation in citations:
            try:
                response = requests.head(citation.source.url, timeout=5)
                if response.status_code == 200:
                    accessible_count += 1
            except:
                pass  # URL not accessible
                
        return accessible_count / len(citations)
        
    def _assess_source_credibility(self, citations: List[Citation]) -> float:
        """Assess credibility of source domains."""
        if not citations:
            return 1.0
            
        # This would use a credibility database or heuristics
        total_credibility = sum(citation.source.credibility_score for citation in citations)
        return total_credibility / len(citations)
```

### 5. Readability Gate

```python
class ReadabilityGate(QualityGate):
    """Verifies content readability and structure."""
    
    def __init__(self, target_audience: str = "general"):
        super().__init__(
            gate_id="readability",
            name="Content Readability",
            description="Ensures content is readable for target audience"
        )
        
        self.target_audience = target_audience
        
        # Readability thresholds by audience
        self.reading_level_targets = {
            "elementary": (90, 100),  # Flesch Reading Ease
            "middle": (70, 90),
            "high": (50, 70),
            "general": (60, 80),
            "college": (30, 60),
            "graduate": (0, 30)
        }
        
        self.add_check(QualityCheck(
            name="reading_ease",
            description="Flesch Reading Ease appropriate for audience",
            weight=0.3,
            threshold=0.8
        ))
        
        self.add_check(QualityCheck(
            name="sentence_structure",
            description="Appropriate sentence length and structure",
            weight=0.25,
            threshold=0.7
        ))
        
        self.add_check(QualityCheck(
            name="content_structure",
            description="Well-structured with headers and paragraphs",
            weight=0.25,
            threshold=0.8
        ))
        
        self.add_check(QualityCheck(
            name="word_choice",
            description="Appropriate vocabulary complexity",
            weight=0.2,
            threshold=0.7
        ))
        
    def evaluate(self, task_result: ContentTaskResult) -> QualityGateResult:
        readability = task_result.result.readability
        if not readability:
            return self._calculate_basic_readability(task_result)
            
        start_time = time.time()
        
        # Reading ease score
        target_min, target_max = self.reading_level_targets.get(self.target_audience, (60, 80))
        reading_ease = readability.flesch_reading_ease
        
        if target_min <= reading_ease <= target_max:
            ease_score = 1.0
        else:
            # Penalize based on distance from target range
            distance = min(abs(reading_ease - target_min), abs(reading_ease - target_max))
            ease_score = max(0.0, 1.0 - distance / 50.0)
            
        # Sentence structure (ideal average: 15-20 words per sentence)
        avg_sentence_length = readability.avg_sentence_length
        if 15 <= avg_sentence_length <= 20:
            sentence_score = 1.0
        elif 10 <= avg_sentence_length <= 25:
            sentence_score = 0.8
        else:
            sentence_score = 0.6
            
        # Content structure
        result = task_result.result
        structure_score = 0.0
        if result.headers:
            structure_score += 0.4
        if result.paragraph_count > 0:
            avg_para_length = result.word_count / result.paragraph_count
            if 50 <= avg_para_length <= 150:  # Good paragraph length
                structure_score += 0.4
        if 500 <= result.word_count <= 3000:  # Reasonable length
            structure_score += 0.2
        structure_score = min(1.0, structure_score)
        
        # Word choice (based on syllables per word)
        syllable_score = 1.0
        if readability.avg_syllables_per_word > 2.0:
            syllable_score = max(0.0, 2.0 - (readability.avg_syllables_per_word - 2.0) * 0.5)
            
        check_scores = {
            "reading_ease": ease_score,
            "sentence_structure": sentence_score,
            "content_structure": structure_score,
            "word_choice": syllable_score
        }
        
        overall_score = self.calculate_weighted_score(check_scores)
        
        result = GateResult.PASS
        warnings = []
        
        if ease_score < 0.8:
            warnings.append(f"Reading level may not match target audience ({self.target_audience})")
            result = GateResult.WARNING
            
        if structure_score < 0.8:
            warnings.append("Content structure could be improved with better organization")
            
        execution_time = time.time() - start_time
        
        return QualityGateResult(
            gate_id=self.gate_id,
            result=result,
            overall_score=overall_score,
            individual_checks=[
                {"name": "reading_ease", "score": ease_score, "passed": ease_score >= 0.8},
                {"name": "sentence_structure", "score": sentence_score, "passed": sentence_score >= 0.7},
                {"name": "content_structure", "score": structure_score, "passed": structure_score >= 0.8},
                {"name": "word_choice", "score": syllable_score, "passed": syllable_score >= 0.7}
            ],
            blocking_issues=[],
            warnings=warnings,
            execution_time_seconds=execution_time
        )
```

## Research Quality Gates

### 6. Research Quality Gate

```python
class ResearchQualityGate(QualityGate):
    """Verifies research thoroughness and quality."""
    
    def __init__(self, min_sources: int = 5):
        super().__init__(
            gate_id="research_quality",
            name="Research Quality Verification",
            description="Ensures research is thorough, credible, and well-sourced"
        )
        
        self.min_sources = min_sources
        
        self.add_check(QualityCheck(
            name="source_quantity",
            description="Adequate number of sources",
            weight=0.2,
            threshold=0.8
        ))
        
        self.add_check(QualityCheck(
            name="source_diversity",
            description="Diverse range of source types",
            weight=0.25,
            threshold=0.7
        ))
        
        self.add_check(QualityCheck(
            name="source_credibility",
            description="High-credibility sources",
            weight=0.3,
            threshold=0.8
        ))
        
        self.add_check(QualityCheck(
            name="citation_coverage",
            description="Claims properly supported by citations",
            weight=0.25,
            threshold=0.9
        ))
        
    def evaluate(self, task_result: ResearchTaskResult) -> QualityGateResult:
        research = task_result.result
        start_time = time.time()
        
        # Source quantity
        source_count_score = min(1.0, research.source_count / self.min_sources)
        
        # Source diversity (already calculated in research result)
        diversity_score = research.source_diversity
        
        # Source credibility (average credibility of sources)
        credibility_score = research.avg_source_credibility
        
        # Citation coverage (percentage of findings with supporting citations)
        citation_coverage = 0.0
        if research.findings:
            findings_with_citations = sum(
                1 for finding in research.findings 
                if finding.supporting_citations
            )
            citation_coverage = findings_with_citations / len(research.findings)
            
        check_scores = {
            "source_quantity": source_count_score,
            "source_diversity": diversity_score,
            "source_credibility": credibility_score,
            "citation_coverage": citation_coverage
        }
        
        overall_score = self.calculate_weighted_score(check_scores)
        
        result = GateResult.PASS
        warnings = []
        blocking_issues = []
        
        if source_count_score < 0.8:
            warnings.append(f"Only {research.source_count} sources found, recommended minimum is {self.min_sources}")
            result = GateResult.WARNING
            
        if credibility_score < 0.6:
            blocking_issues.append("Source credibility too low for reliable research")
            result = GateResult.FAIL
            
        if citation_coverage < 0.7:
            warnings.append("Some claims lack proper citation support")
            if result == GateResult.PASS:
                result = GateResult.WARNING
                
        execution_time = time.time() - start_time
        
        return QualityGateResult(
            gate_id=self.gate_id,
            result=result,
            overall_score=overall_score,
            individual_checks=[
                {"name": "source_quantity", "score": source_count_score, "passed": source_count_score >= 0.8},
                {"name": "source_diversity", "score": diversity_score, "passed": diversity_score >= 0.7},
                {"name": "source_credibility", "score": credibility_score, "passed": credibility_score >= 0.8},
                {"name": "citation_coverage", "score": citation_coverage, "passed": citation_coverage >= 0.9}
            ],
            blocking_issues=blocking_issues,
            warnings=warnings,
            execution_time_seconds=execution_time
        )
```

## Translation Quality Gates

### 7. Back-Translation Gate

```python
class BackTranslationGate(QualityGate):
    """Verifies translation quality through back-translation."""
    
    def __init__(self, max_divergence: float = 0.2):
        super().__init__(
            gate_id="back_translation_divergence",
            name="Back-Translation Verification",
            description="Verifies translation accuracy through back-translation"
        )
        
        self.max_divergence = max_divergence
        
        self.add_check(QualityCheck(
            name="meaning_preservation",
            description="Original meaning preserved in translation",
            weight=0.6,
            threshold=0.8,
            critical=True
        ))
        
        self.add_check(QualityCheck(
            name="fluency",
            description="Translation reads naturally",
            weight=0.25,
            threshold=0.8
        ))
        
        self.add_check(QualityCheck(
            name="completeness",
            description="No untranslated segments",
            weight=0.15,
            threshold=0.95
        ))
        
    def evaluate(self, task_result: TranslationTaskResult) -> QualityGateResult:
        translation = task_result.result
        back_trans = translation.back_translation
        
        if not back_trans:
            return self._run_basic_translation_check(task_result)
            
        start_time = time.time()
        
        # Meaning preservation (inverse of divergence)
        meaning_score = 1.0 - back_trans.divergence_score
        
        # Fluency score
        fluency_score = translation.fluency_score
        
        # Completeness check (look for untranslated text)
        completeness_score = self._check_translation_completeness(translation)
        
        check_scores = {
            "meaning_preservation": meaning_score,
            "fluency": fluency_score,
            "completeness": completeness_score
        }
        
        overall_score = self.calculate_weighted_score(check_scores)
        
        result = GateResult.PASS
        blocking_issues = []
        warnings = []
        
        if back_trans.divergence_score > self.max_divergence:
            result = GateResult.FAIL
            blocking_issues.append(f"Back-translation divergence {back_trans.divergence_score:.2f} exceeds threshold {self.max_divergence}")
            
        if not back_trans.meaning_preserved:
            result = GateResult.FAIL
            blocking_issues.append("Meaning not preserved in translation")
            
        if fluency_score < 0.8:
            warnings.append("Translation may not read naturally")
            if result == GateResult.PASS:
                result = GateResult.WARNING
                
        execution_time = time.time() - start_time
        
        return QualityGateResult(
            gate_id=self.gate_id,
            result=result,
            overall_score=overall_score,
            individual_checks=[
                {"name": "meaning_preservation", "score": meaning_score, "passed": meaning_score >= 0.8},
                {"name": "fluency", "score": fluency_score, "passed": fluency_score >= 0.8},
                {"name": "completeness", "score": completeness_score, "passed": completeness_score >= 0.95}
            ],
            blocking_issues=blocking_issues,
            warnings=warnings,
            execution_time_seconds=execution_time
        )
        
    def _check_translation_completeness(self, translation: 'TranslationResult') -> float:
        """Check for untranslated segments."""
        # This would implement logic to detect untranslated text
        # For now, return 1.0 if lengths are reasonable
        length_ratio = translation.length_ratio
        if 0.5 <= length_ratio <= 2.0:  # Reasonable length variation
            return 1.0
        else:
            return 0.7  # Suspicious length ratio
```

## Quality Gate Manager

```python
class QualityGateManager:
    """Central manager for all quality gates."""
    
    def __init__(self):
        self.gates: Dict[str, QualityGate] = {}
        self._register_default_gates()
        
    def register_gate(self, gate: QualityGate):
        """Register a quality gate."""
        self.gates[gate.gate_id] = gate
        
    def run_gate(self, gate_id: str, task_result: TaskResult) -> QualityGateResult:
        """Run a specific quality gate."""
        gate = self.gates.get(gate_id)
        if not gate:
            raise ValueError(f"Quality gate {gate_id} not found")
            
        return gate.evaluate(task_result)
        
    def run_gates(self, gate_ids: List[str], task_result: TaskResult) -> List[QualityGateResult]:
        """Run multiple quality gates."""
        results = []
        for gate_id in gate_ids:
            try:
                result = self.run_gate(gate_id, task_result)
                results.append(result)
            except Exception as e:
                # Log error and create failed result
                results.append(QualityGateResult(
                    gate_id=gate_id,
                    result=GateResult.FAIL,
                    overall_score=0.0,
                    individual_checks=[],
                    blocking_issues=[f"Gate execution failed: {str(e)}"],
                    execution_time_seconds=0.0
                ))
                
        return results
        
    def _register_default_gates(self):
        """Register all default quality gates."""
        # Code gates
        self.register_gate(BuildSuccessGate())
        self.register_gate(UnitTestGate())
        self.register_gate(CodeQualityGate())
        
        # Content gates
        self.register_gate(FactCheckGate())
        self.register_gate(ReadabilityGate())
        
        # Research gates
        self.register_gate(ResearchQualityGate())
        
        # Translation gates
        self.register_gate(BackTranslationGate())
```

## Usage Examples

```python
# Initialize quality gate manager
gate_manager = QualityGateManager()

# Run quality gates for a code task
code_result = CodeTaskResult(...)  # Task result from code generation
gate_results = gate_manager.run_gates(
    gate_ids=["build_success", "unit_tests", "code_quality"],
    task_result=code_result
)

# Check if all gates passed
all_passed = all(result.result == GateResult.PASS for result in gate_results)
has_warnings = any(result.result == GateResult.WARNING for result in gate_results)
blocking_issues = [issue for result in gate_results for issue in result.blocking_issues]

# Custom gate configuration
custom_test_gate = UnitTestGate(min_coverage=0.9)  # Higher coverage requirement
gate_manager.register_gate(custom_test_gate)

# Content quality gates
content_result = ContentTaskResult(...)
content_gates = gate_manager.run_gates(
    gate_ids=["fact_accuracy", "readability"],
    task_result=content_result
)
```

Quality gates provide **automated, consistent, and measurable quality assurance** that ensures all task outputs meet defined standards before proceeding in the orchestration workflow.