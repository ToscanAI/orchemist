# Structured Output Schemas

All agent task results follow **strict, validated schemas** using Pydantic models. This ensures type safety, consistent interfaces, and reliable quality assessment across all task types.

## Design Principles

- **Type Safety**: All fields strictly typed and validated
- **Confidence Scoring**: Every result includes quality/confidence metrics
- **Structured Errors**: Machine-readable error information
- **Extensible Metadata**: Task-specific additional data
- **Backwards Compatible**: Schema versioning for evolution

## Base Schema Hierarchy

```python
from pydantic import BaseModel, Field, validator
from typing import Any, Dict, List, Optional, Union, Literal
from datetime import datetime
from enum import Enum

class TaskStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"      # Some results, but incomplete
    REQUIRES_REVIEW = "requires_review"  # Needs human verification

class ConfidenceLevel(str, Enum):
    VERY_LOW = "very_low"    # 0.0 - 0.2
    LOW = "low"              # 0.2 - 0.4
    MEDIUM = "medium"        # 0.4 - 0.6
    HIGH = "high"            # 0.6 - 0.8
    VERY_HIGH = "very_high"  # 0.8 - 1.0

class TaskError(BaseModel):
    """Structured error information."""
    code: str                # Machine-readable error code
    message: str             # Human-readable description
    severity: Literal["warning", "error", "critical"]
    context: Dict[str, Any] = {}  # Additional error context
    suggestion: Optional[str] = None  # How to fix this error

class TaskResult(BaseModel):
    """Base class for all task results."""
    task_id: str
    task_type: str
    status: TaskStatus
    
    # Quality metrics
    confidence: float = Field(ge=0.0, le=1.0, description="Overall quality score")
    confidence_level: ConfidenceLevel
    
    # Core result data
    result: Any  # Task-specific payload (defined in subclasses)
    
    # Metadata and tracking
    metadata: Dict[str, Any] = {}
    errors: List[TaskError] = []
    warnings: List[str] = []
    
    # Execution details
    created_at: datetime = Field(default_factory=datetime.now)
    model_used: str
    tokens_consumed: int
    execution_time_seconds: float
    cost_usd: Optional[float] = None
    
    # Quality gate results
    quality_checks_passed: Dict[str, bool] = {}
    quality_check_details: Dict[str, Any] = {}
    
    @validator('confidence_level', pre=False, always=True)
    def set_confidence_level(cls, v, values):
        """Auto-set confidence level based on numeric confidence."""
        conf = values.get('confidence', 0.0)
        if conf <= 0.2:
            return ConfidenceLevel.VERY_LOW
        elif conf <= 0.4:
            return ConfidenceLevel.LOW
        elif conf <= 0.6:
            return ConfidenceLevel.MEDIUM
        elif conf <= 0.8:
            return ConfidenceLevel.HIGH
        else:
            return ConfidenceLevel.VERY_HIGH
```

## Task-Specific Result Schemas

### 1. CodeTaskResult

```python
class BuildResult(BaseModel):
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    build_time_seconds: float
    artifacts: List[str] = []  # Generated files

class TestResult(BaseModel):
    success: bool
    tests_run: int
    tests_passed: int
    tests_failed: int
    coverage_percentage: Optional[float] = None
    test_output: str
    failed_tests: List[str] = []

class LintResult(BaseModel):
    clean: bool
    errors: int
    warnings: int
    issues: List[Dict[str, Any]] = []  # Specific lint issues
    
class SecurityScanResult(BaseModel):
    clean: bool
    vulnerabilities: List[Dict[str, Any]] = []
    risk_level: Literal["low", "medium", "high", "critical"]

class CodeTaskResult(TaskResult):
    """Result from code generation, refactoring, or review tasks."""
    task_type: Literal["code"] = "code"
    
    class CodeResult(BaseModel):
        # Generated/modified code
        files: Dict[str, str]  # filename -> content
        
        # Quality metrics
        build_result: Optional[BuildResult] = None
        test_result: Optional[TestResult] = None
        lint_result: Optional[LintResult] = None
        security_scan: Optional[SecurityScanResult] = None
        
        # Code metrics
        lines_of_code: int
        complexity_score: Optional[float] = None
        maintainability_index: Optional[float] = None
        
        # Git integration
        branch_name: Optional[str] = None
        commit_hash: Optional[str] = None
        pr_url: Optional[str] = None
        
    result: CodeResult
    
    @validator('confidence')
    def validate_code_confidence(cls, v, values):
        """Code confidence based on build/test/lint results."""
        result = values.get('result')
        if not result:
            return 0.0
            
        score = 0.0
        checks = 0
        
        # Build success (40% weight)
        if result.build_result:
            score += 0.4 if result.build_result.success else 0.0
            checks += 1
            
        # Test success (30% weight)  
        if result.test_result:
            test_score = result.test_result.tests_passed / max(result.test_result.tests_run, 1)
            score += 0.3 * test_score
            checks += 1
            
        # Lint clean (20% weight)
        if result.lint_result:
            lint_score = 1.0 if result.lint_result.clean else max(0.0, 1.0 - result.lint_result.errors * 0.1)
            score += 0.2 * lint_score
            checks += 1
            
        # Security clean (10% weight)
        if result.security_scan:
            sec_score = 1.0 if result.security_scan.clean else 0.0
            score += 0.1 * sec_score
            checks += 1
            
        return score / max(checks, 1) if checks > 0 else v
```

### 2. ResearchTaskResult

```python
class Source(BaseModel):
    url: str
    title: str
    domain: str
    credibility_score: float = Field(ge=0.0, le=1.0)
    publish_date: Optional[datetime] = None
    author: Optional[str] = None
    bias_score: Optional[float] = Field(None, ge=-1.0, le=1.0)  # -1=left, +1=right
    
class Citation(BaseModel):
    source: Source
    quote: str
    page_number: Optional[int] = None
    relevance_score: float = Field(ge=0.0, le=1.0)
    fact_checked: bool = False
    
class Finding(BaseModel):
    claim: str
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_citations: List[Citation]
    conflicting_evidence: List[Citation] = []
    category: str  # e.g., "statistic", "opinion", "fact"

class ResearchTaskResult(TaskResult):
    """Result from research, fact-checking, or analysis tasks."""
    task_type: Literal["research"] = "research"
    
    class ResearchResult(BaseModel):
        # Core research findings
        findings: List[Finding]
        sources: List[Source]
        
        # Research quality metrics
        source_count: int
        source_diversity: float = Field(ge=0.0, le=1.0)  # Domain diversity
        avg_source_credibility: float = Field(ge=0.0, le=1.0)
        citation_count: int
        fact_check_coverage: float = Field(ge=0.0, le=1.0)  # % claims fact-checked
        
        # Search metadata
        search_queries: List[str]
        search_results_total: int
        search_time_seconds: float
        
        # Summary and synthesis
        summary: str
        key_insights: List[str]
        conflicting_information: List[str] = []
        research_gaps: List[str] = []
        
    result: ResearchResult
    
    @validator('confidence')
    def validate_research_confidence(cls, v, values):
        """Research confidence based on source quality and coverage."""
        result = values.get('result')
        if not result:
            return 0.0
            
        # Source quality (40% weight)
        source_quality = result.avg_source_credibility * 0.4
        
        # Source count (30% weight)
        source_count_score = min(1.0, result.source_count / 10.0) * 0.3
        
        # Fact-check coverage (20% weight)  
        fact_check_score = result.fact_check_coverage * 0.2
        
        # Source diversity (10% weight)
        diversity_score = result.source_diversity * 0.1
        
        return source_quality + source_count_score + fact_check_score + diversity_score
```

### 3. ContentTaskResult

```python
class ReadabilityMetrics(BaseModel):
    flesch_reading_ease: float
    flesch_kincaid_grade: float
    avg_sentence_length: float
    avg_syllables_per_word: float
    readability_level: Literal["elementary", "middle", "high", "college", "graduate"]

class FactCheckResult(BaseModel):
    claims_checked: int
    claims_verified: int
    claims_disputed: int
    claims_uncertain: int
    overall_accuracy: float = Field(ge=0.0, le=1.0)
    disputed_claims: List[str] = []

class ContentTaskResult(TaskResult):
    """Result from content creation, editing, or review tasks."""
    task_type: Literal["content"] = "content"
    
    class ContentResult(BaseModel):
        # Generated content
        content: str
        title: Optional[str] = None
        subtitle: Optional[str] = None
        excerpt: Optional[str] = None
        
        # Content metrics
        word_count: int
        character_count: int
        paragraph_count: int
        sentence_count: int
        
        # Quality metrics
        readability: Optional[ReadabilityMetrics] = None
        fact_check: Optional[FactCheckResult] = None
        
        # SEO and engagement
        keywords: List[str] = []
        keyword_density: Dict[str, float] = {}
        sentiment_score: Optional[float] = Field(None, ge=-1.0, le=1.0)
        
        # Structure analysis
        headers: List[str] = []
        outline: List[str] = []
        
        # Citations and sources
        citations: List[Citation] = []
        external_links: List[str] = []
        
    result: ContentResult
    
    @validator('confidence')
    def validate_content_confidence(cls, v, values):
        """Content confidence based on readability, fact-checking, and structure."""
        result = values.get('result')
        if not result:
            return 0.0
            
        score = 0.0
        
        # Fact-check accuracy (50% weight)
        if result.fact_check:
            score += result.fact_check.overall_accuracy * 0.5
        else:
            score += 0.3  # Assume moderate confidence without fact-check
            
        # Readability (25% weight)
        if result.readability:
            # Score based on appropriate reading level
            reading_levels = {"elementary": 0.6, "middle": 0.8, "high": 1.0, "college": 0.9, "graduate": 0.7}
            score += reading_levels.get(result.readability.readability_level, 0.5) * 0.25
            
        # Structure quality (25% weight)
        structure_score = 0.0
        if result.headers:
            structure_score += 0.3
        if result.citations:
            structure_score += 0.4
        if 500 <= result.word_count <= 3000:  # Appropriate length
            structure_score += 0.3
        score += structure_score * 0.25
        
        return min(1.0, score)
```

### 4. TranslationTaskResult

```python
class BackTranslationResult(BaseModel):
    back_translated_text: str
    divergence_score: float = Field(ge=0.0, le=1.0)  # 0=identical, 1=completely different
    meaning_preserved: bool
    
class CulturalAdaptation(BaseModel):
    adaptations_made: List[str]
    cultural_notes: List[str]
    region_specific_terms: Dict[str, str] = {}  # original -> localized

class TranslationTaskResult(TaskResult):
    """Result from translation, localization, or language tasks."""
    task_type: Literal["translation"] = "translation"
    
    class TranslationResult(BaseModel):
        # Translation output
        translated_text: str
        source_language: str
        target_language: str
        
        # Quality verification
        back_translation: Optional[BackTranslationResult] = None
        cultural_adaptation: Optional[CulturalAdaptation] = None
        
        # Translation metrics
        translation_confidence: float = Field(ge=0.0, le=1.0)
        fluency_score: float = Field(ge=0.0, le=1.0)
        adequacy_score: float = Field(ge=0.0, le=1.0)  # Meaning preservation
        
        # Technical details
        word_count_source: int
        word_count_target: int
        length_ratio: float  # target/source
        
        # Glossary and terminology
        terminology_used: Dict[str, str] = {}
        glossary_coverage: float = Field(ge=0.0, le=1.0)
        
    result: TranslationResult
    
    @validator('confidence')
    def validate_translation_confidence(cls, v, values):
        """Translation confidence based on back-translation and fluency."""
        result = values.get('result')
        if not result:
            return 0.0
            
        score = 0.0
        
        # Back-translation quality (60% weight)
        if result.back_translation:
            back_trans_score = 1.0 - result.back_translation.divergence_score
            score += back_trans_score * 0.6
            
        # Fluency and adequacy (40% weight)
        fluency_adequacy = (result.fluency_score + result.adequacy_score) / 2.0
        score += fluency_adequacy * 0.4
        
        return score
```

### 5. ReviewTaskResult

```python
class ReviewCriterion(BaseModel):
    name: str
    score: float = Field(ge=0.0, le=1.0)
    feedback: str
    weight: float = Field(ge=0.0, le=1.0)

class ReviewDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    REVISE = "revise"
    ESCALATE = "escalate"

class ReviewTaskResult(TaskResult):
    """Result from review, critique, or quality assessment tasks."""
    task_type: Literal["review"] = "review"
    
    class ReviewResult(BaseModel):
        # Review decision
        decision: ReviewDecision
        overall_score: float = Field(ge=0.0, le=1.0)
        
        # Detailed feedback
        criteria_scores: List[ReviewCriterion]
        summary_feedback: str
        specific_issues: List[str] = []
        suggestions: List[str] = []
        
        # Approval workflow
        requires_changes: bool
        blocking_issues: List[str] = []
        nice_to_have: List[str] = []
        
        # Review metadata
        review_type: str  # "code_review", "content_review", "design_review"
        reviewer_expertise: Literal["junior", "senior", "expert"]
        time_spent_minutes: float
        
    result: ReviewResult
    
    @validator('confidence')
    def validate_review_confidence(cls, v, values):
        """Review confidence based on consistency of criteria scores."""
        result = values.get('result')
        if not result:
            return 0.0
            
        if not result.criteria_scores:
            return 0.5  # Default moderate confidence
            
        # Calculate weighted average of criteria scores
        total_score = sum(c.score * c.weight for c in result.criteria_scores)
        total_weight = sum(c.weight for c in result.criteria_scores)
        
        if total_weight == 0:
            return 0.5
            
        weighted_avg = total_score / total_weight
        
        # Calculate consistency (lower variance = higher confidence)
        scores = [c.score for c in result.criteria_scores]
        variance = sum((s - weighted_avg) ** 2 for s in scores) / len(scores)
        consistency = max(0.0, 1.0 - variance)  # Lower variance = higher consistency
        
        # Combine score and consistency
        return (weighted_avg + consistency) / 2.0
```

## Generic Task Result Envelope

```python
class GenericTaskResult(TaskResult):
    """Envelope for custom or undefined task types."""
    task_type: str  # Any string allowed
    
    class GenericResult(BaseModel):
        output: Any  # Flexible output structure
        custom_metrics: Dict[str, float] = {}
        custom_metadata: Dict[str, Any] = {}
        
    result: GenericResult
```

## Schema Registry

```python
from typing import Type, Dict

class SchemaRegistry:
    """Central registry for task result schemas."""
    
    _schemas: Dict[str, Type[TaskResult]] = {
        "code": CodeTaskResult,
        "research": ResearchTaskResult,
        "content": ContentTaskResult,
        "translation": TranslationTaskResult,
        "review": ReviewTaskResult,
    }
    
    @classmethod
    def register_schema(cls, task_type: str, schema_class: Type[TaskResult]):
        """Register a new task result schema."""
        cls._schemas[task_type] = schema_class
        
    @classmethod
    def get_schema(cls, task_type: str) -> Type[TaskResult]:
        """Get schema class for task type."""
        return cls._schemas.get(task_type, GenericTaskResult)
        
    @classmethod
    def validate_result(cls, task_type: str, result_data: Dict) -> TaskResult:
        """Validate and parse result data into appropriate schema."""
        schema_class = cls.get_schema(task_type)
        return schema_class(**result_data)
```

## Confidence Calculation Framework

```python
class ConfidenceCalculator:
    """Standardized confidence calculation across task types."""
    
    @staticmethod
    def calculate_weighted_confidence(
        metrics: Dict[str, float], 
        weights: Dict[str, float]
    ) -> float:
        """Calculate weighted average confidence score."""
        total_score = sum(metrics.get(key, 0.0) * weight for key, weight in weights.items())
        total_weight = sum(weights.values())
        return total_score / total_weight if total_weight > 0 else 0.0
        
    @staticmethod
    def penalize_for_errors(base_confidence: float, error_count: int) -> float:
        """Apply confidence penalty for errors."""
        penalty = min(0.5, error_count * 0.1)  # Max 50% penalty
        return max(0.0, base_confidence - penalty)
        
    @staticmethod
    def boost_for_validation(base_confidence: float, validations_passed: int, total_validations: int) -> float:
        """Boost confidence for passed validations."""
        if total_validations == 0:
            return base_confidence
        validation_rate = validations_passed / total_validations
        boost = min(0.2, validation_rate * 0.2)  # Max 20% boost
        return min(1.0, base_confidence + boost)
```

## Schema Evolution and Versioning

```python
class SchemaVersion(BaseModel):
    """Schema version metadata."""
    schema_name: str
    version: str  # Semantic versioning: "1.2.3"
    created_at: datetime
    breaking_changes: List[str] = []
    migration_notes: str = ""

class VersionedTaskResult(TaskResult):
    """Base class with versioning support."""
    schema_version: str = "1.0.0"
    
    def migrate_from_version(self, old_version: str, old_data: Dict) -> 'VersionedTaskResult':
        """Override in subclasses to handle schema migrations."""
        return self
```

## Usage Examples

```python
# Creating a code task result
code_result = CodeTaskResult(
    task_id="task_123",
    status=TaskStatus.SUCCESS,
    confidence=0.85,
    result=CodeTaskResult.CodeResult(
        files={"main.py": "def hello(): return 'world'"},
        build_result=BuildResult(success=True, exit_code=0, stdout="Build successful", stderr="", build_time_seconds=2.5),
        test_result=TestResult(success=True, tests_run=5, tests_passed=5, tests_failed=0, coverage_percentage=95.0, test_output="All tests passed"),
        lines_of_code=50
    ),
    model_used="sonnet-4",
    tokens_consumed=1500,
    execution_time_seconds=45.2
)

# Validating arbitrary result data
result_data = {...}  # From agent execution
validated_result = SchemaRegistry.validate_result("code", result_data)

# Schema-based confidence calculation  
confidence = ConfidenceCalculator.calculate_weighted_confidence(
    metrics={"build_success": 1.0, "test_coverage": 0.95, "lint_clean": 0.8},
    weights={"build_success": 0.5, "test_coverage": 0.3, "lint_clean": 0.2}
)
```

These structured schemas ensure **consistent, validated, and comparable results** across all agent tasks, enabling reliable quality assessment, automated decision-making, and comprehensive analytics.