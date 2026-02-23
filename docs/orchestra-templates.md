# Orchestra Templates

> ✅ **Template Engine: IMPLEMENTED** (Week 3, PR #64). YAML templates with topological phase sorting, dependency resolution, and output forwarding are working. The `PhaseSequencer` executes phases in order, passing outputs between them. 442 tests passing.
>
> The Content Pipeline v2.3 (8 phases) is the reference template. Code Sprint and Deep Research templates are drafts.

Orchestra templates are **reusable multi-agent coordination patterns** that encode proven workflows for complex tasks. Each template defines phases, dependencies, quality gates, and coordination logic.

## Template Architecture

```python
from typing import List, Dict, Optional, Any, Set
from enum import Enum

class PhaseType(str, Enum):
    SEQUENTIAL = "sequential"    # Phases run one after another
    PARALLEL = "parallel"       # Phases run simultaneously  
    CONDITIONAL = "conditional" # Phase runs based on conditions
    HUMAN_GATE = "human_gate"   # Requires human approval

class Phase(BaseModel):
    id: str
    name: str
    description: str
    type: PhaseType
    
    # Task configuration
    task_type: str              # 'code', 'content', 'research', 'translation', 'review'
    model_tier: str             # 'haiku', 'sonnet', 'opus'
    thinking_level: str         # 'off', 'low', 'medium', 'high'
    
    # Dependencies and flow control
    depends_on: List[str] = []  # Phase IDs that must complete first
    timeout_minutes: int = 60
    max_retries: int = 3
    
    # Quality requirements
    min_confidence: float = 0.7
    quality_gates: List[str] = []  # Quality gate IDs to run
    
    # Conditional logic
    run_condition: Optional[str] = None  # Python expression
    success_condition: Optional[str] = None
    
    # Human interaction
    human_approval_required: bool = False
    human_timeout_minutes: int = 1440  # 24 hours

class OrchestraTemplate(BaseModel):
    id: str
    name: str
    description: str
    version: str
    
    # Template structure
    phases: List[Phase]
    parallel_groups: List[List[str]] = []  # Phase IDs that can run together
    
    # Resource limits
    max_duration_hours: int = 24
    cost_budget_usd: Optional[float] = None
    
    # Quality requirements
    overall_confidence_threshold: float = 0.8
    
    # Configuration schema
    config_schema: Dict[str, Any]  # JSON schema for template parameters
    
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate configuration against schema."""
        pass
        
    def get_execution_plan(self, config: Dict[str, Any]) -> List[List[str]]:
        """Generate phase execution plan based on dependencies."""
        pass
```

## Built-In Templates

### 1. Content Pipeline Template

**Use Case**: High-quality content creation with fact-checking and human review

```python
CONTENT_PIPELINE_TEMPLATE = OrchestraTemplate(
    id="content-pipeline",
    name="Content Creation Pipeline v2.3",
    description="8-phase content creation: Research → Write → Fact-Check → Logical Flow → Red Team → Consistency → Apply Fixes → Human Review",
    version="2.3.0",
    phases=[
        Phase(
            id="research",
            name="Phase 1: Research & Source Gathering",
            description="Deep research with 30+ sources, verified citations, structured brief",
            type=PhaseType.SEQUENTIAL,
            task_type="research",
            model_tier="sonnet",
            thinking_level="low",
            timeout_minutes=30,
            min_confidence=0.7,
            quality_gates=["research_quality", "source_count"]
        ),
        Phase(
            id="write",
            name="Phase 2: Content Creation",
            description="Generate article + companion post from research brief",
            type=PhaseType.SEQUENTIAL,
            task_type="content",
            model_tier="sonnet",
            thinking_level="low",
            depends_on=["research"],
            timeout_minutes=45,
            min_confidence=0.6,
            quality_gates=["content_structure", "readability"]
        ),
        Phase(
            id="fact_check",
            name="Phase 3: Fact Verification",
            description="Independent agent verifies claims, citations, and source accuracy. MUST be a different agent than writer.",
            type=PhaseType.PARALLEL,
            task_type="review",
            model_tier="sonnet",
            thinking_level="low",
            depends_on=["write"],
            timeout_minutes=30,
            min_confidence=0.8,
            quality_gates=["fact_accuracy", "citation_verification", "source_accuracy"]
        ),
        Phase(
            id="logical_flow",
            name="Phase 4: Logical Flow Review",
            description="Independent agent reviews argument structure, thesis clarity, transitions, and narrative arc",
            type=PhaseType.PARALLEL,
            task_type="review",
            model_tier="sonnet",
            thinking_level="low",
            depends_on=["write"],
            timeout_minutes=30,
            min_confidence=0.7,
            quality_gates=["thesis_present", "transitions_smooth", "sections_flow"]
        ),
        Phase(
            id="red_team",
            name="Phase 5: Red Team / Backlash Review",
            description="Independent agent adversarially reviews for backlash risk, tone issues, outsider-writing problems. Cross-domain articles REQUIRE practitioner persona.",
            type=PhaseType.PARALLEL,
            task_type="review",
            model_tier="sonnet",
            thinking_level="low",
            depends_on=["write"],
            timeout_minutes=30,
            min_confidence=0.7,
            quality_gates=["backlash_score", "tone_check", "outsider_writing"]
        ),
        Phase(
            id="consistency",
            name="Phase 6: Cross-Phase Consistency Check",
            description="Verify all review findings are consistent, no contradictions between phases 3-5",
            type=PhaseType.SEQUENTIAL,
            task_type="review",
            model_tier="sonnet",
            thinking_level="low",
            depends_on=["fact_check", "logical_flow", "red_team"],
            timeout_minutes=15,
            min_confidence=0.7
        ),
        Phase(
            id="apply_fixes",
            name="Phase 7: Apply Fixes (7a mechanical + 7b tone + 7c companion red team)",
            description="Apply all corrections from phases 3-6. 7a: mechanical fixes. 7b: tone/framing rewrites (requires editorial judgment — use Sonnet+). 7c: companion post red team against corrected article.",
            type=PhaseType.SEQUENTIAL,
            task_type="content",
            model_tier="sonnet",
            thinking_level="low",
            depends_on=["consistency"],
            timeout_minutes=45,
            min_confidence=0.8,
            quality_gates=["all_fixes_applied", "companion_backlash_check"]
        ),
        Phase(
            id="human_review",
            name="Phase 8: Human Review & 24h Cool-Down",
            description="Final human review before publication. MANDATORY 24-hour cool-down period.",
            type=PhaseType.HUMAN_GATE,
            task_type="review",
            depends_on=["apply_fixes"],
            human_approval_required=True,
            human_timeout_minutes=2880  # 48 hours (includes 24h cool-down)
        )
    ],
    parallel_groups=[["fact_check", "logical_flow", "red_team"]],
    max_duration_hours=8,
    overall_confidence_threshold=0.8,
    config_schema={
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Content topic or brief"},
            "word_count": {"type": "integer", "minimum": 500, "maximum": 5000},
            "target_audience": {"type": "string", "description": "Who is this for"},
            "tone": {"type": "string", "description": "e.g. practitioner sharing experience"},
            "author": {"type": "string", "description": "Author name for byline"},
            "include_companion": {"type": "boolean", "default": True, "description": "Generate companion social post"},
            "cross_domain": {"type": "boolean", "default": False, "description": "If true, Phase 5 uses practitioner persona"},
            "publication_deadline": {"type": "string", "format": "date-time"}
        },
        "required": ["topic", "target_audience"]
    }
)
```

### 2. Code Sprint Template

**Use Case**: Parallel development with testing and integration

```python
CODE_SPRINT_TEMPLATE = OrchestraTemplate(
    id="code-sprint",
    name="Parallel Code Sprint",
    description="Multiple agents develop features in parallel with git integration and testing",
    version="1.0.0",
    phases=[
        Phase(
            id="planning",
            name="Sprint Planning",
            description="Break down requirements into parallel work items",
            type=PhaseType.SEQUENTIAL,
            task_type="code",
            model_tier="sonnet",
            thinking_level="high",
            timeout_minutes=20,
            quality_gates=["task_decomposition"]
        ),
        Phase(
            id="feature_a",
            name="Feature A Development",
            description="Develop feature A in parallel",
            type=PhaseType.PARALLEL,
            task_type="code",
            model_tier="sonnet",
            thinking_level="medium",
            depends_on=["planning"],
            timeout_minutes=90,
            min_confidence=0.8,
            quality_gates=["build_success", "unit_tests", "code_quality"]
        ),
        Phase(
            id="feature_b",
            name="Feature B Development", 
            description="Develop feature B in parallel",
            type=PhaseType.PARALLEL,
            task_type="code",
            model_tier="sonnet",
            thinking_level="medium",
            depends_on=["planning"],
            timeout_minutes=90,
            min_confidence=0.8,
            quality_gates=["build_success", "unit_tests", "code_quality"]
        ),
        Phase(
            id="feature_c",
            name="Feature C Development",
            description="Develop feature C in parallel",
            type=PhaseType.PARALLEL,
            task_type="code",
            model_tier="sonnet",
            thinking_level="medium",
            depends_on=["planning"],
            timeout_minutes=90,
            min_confidence=0.8,
            quality_gates=["build_success", "unit_tests", "code_quality"]
        ),
        Phase(
            id="integration",
            name="Feature Integration",
            description="Merge all features and resolve conflicts",
            type=PhaseType.SEQUENTIAL,
            task_type="code",
            model_tier="opus",
            thinking_level="high",
            depends_on=["feature_a", "feature_b", "feature_c"],
            timeout_minutes=60,
            min_confidence=0.9,
            quality_gates=["integration_tests", "conflict_resolution"]
        ),
        Phase(
            id="system_test",
            name="System Testing",
            description="End-to-end testing of integrated system",
            type=PhaseType.SEQUENTIAL,
            task_type="review",
            model_tier="sonnet",
            thinking_level="high",
            depends_on=["integration"],
            timeout_minutes=45,
            min_confidence=0.9,
            quality_gates=["e2e_tests", "performance_tests"]
        )
    ],
    parallel_groups=[["feature_a", "feature_b", "feature_c"]],
    max_duration_hours=6,
    overall_confidence_threshold=0.85,
    config_schema={
        "type": "object",
        "properties": {
            "repository_url": {"type": "string", "format": "uri"},
            "base_branch": {"type": "string", "default": "main"},
            "features": {
                "type": "array",
                "items": {
                    "type": "object", 
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "files": {"type": "array", "items": {"type": "string"}},
                        "tests": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["name", "description"]
                },
                "minItems": 2,
                "maxItems": 5
            },
            "test_framework": {"type": "string", "enum": ["pytest", "jest", "junit"]},
            "ci_config": {"type": "object"}
        },
        "required": ["repository_url", "features"]
    }
)
```

### 3. Deep Research Template

**Use Case**: Multi-source research with synthesis and citation verification

```python
DEEP_RESEARCH_TEMPLATE = OrchestraTemplate(
    id="deep-research",
    name="Deep Research Pipeline",
    description="Multi-source search → synthesis → citation verification",
    version="1.0.0", 
    phases=[
        Phase(
            id="search_academic",
            name="Academic Source Search",
            description="Search scholarly databases and papers",
            type=PhaseType.PARALLEL,
            task_type="research",
            model_tier="haiku",
            thinking_level="low",
            timeout_minutes=20,
            min_confidence=0.6,
            quality_gates=["source_credibility"]
        ),
        Phase(
            id="search_news",
            name="News & Media Search", 
            description="Search current news and media sources",
            type=PhaseType.PARALLEL,
            task_type="research",
            model_tier="haiku",
            thinking_level="low",
            timeout_minutes=15,
            min_confidence=0.6,
            quality_gates=["source_recency", "bias_detection"]
        ),
        Phase(
            id="search_expert",
            name="Expert Opinion Search",
            description="Search for expert opinions and industry analysis",
            type=PhaseType.PARALLEL,
            task_type="research",
            model_tier="haiku", 
            thinking_level="low",
            timeout_minutes=20,
            min_confidence=0.6,
            quality_gates=["expert_credibility"]
        ),
        Phase(
            id="synthesis",
            name="Research Synthesis",
            description="Combine findings from all sources into coherent analysis",
            type=PhaseType.SEQUENTIAL,
            task_type="research",
            model_tier="opus",
            thinking_level="high",
            depends_on=["search_academic", "search_news", "search_expert"],
            timeout_minutes=45,
            min_confidence=0.8,
            quality_gates=["synthesis_quality", "claim_support"]
        ),
        Phase(
            id="citation_verification",
            name="Citation Verification",
            description="Verify all citations are accessible and accurately quoted",
            type=PhaseType.SEQUENTIAL,
            task_type="review",
            model_tier="sonnet",
            thinking_level="medium",
            depends_on=["synthesis"],
            timeout_minutes=30,
            min_confidence=0.9,
            quality_gates=["citation_accessibility", "quote_accuracy"]
        ),
        Phase(
            id="conflict_resolution",
            name="Conflict Resolution",
            description="Address conflicting information found across sources",
            type=PhaseType.CONDITIONAL,
            task_type="research",
            model_tier="opus",
            thinking_level="high",
            depends_on=["citation_verification"],
            run_condition="len(synthesis.result.conflicting_information) > 0",
            timeout_minutes=30,
            min_confidence=0.8
        )
    ],
    parallel_groups=[["search_academic", "search_news", "search_expert"]],
    max_duration_hours=3,
    overall_confidence_threshold=0.85,
    config_schema={
        "type": "object",
        "properties": {
            "research_question": {"type": "string", "description": "Primary research question"},
            "scope": {"type": "string", "enum": ["narrow", "broad", "comprehensive"]},
            "time_range": {"type": "string", "description": "Time range for sources (e.g., 'last 5 years')"},
            "source_types": {
                "type": "array",
                "items": {"type": "string", "enum": ["academic", "news", "expert", "government", "industry"]},
                "default": ["academic", "news", "expert"]
            },
            "min_sources": {"type": "integer", "minimum": 5, "maximum": 50, "default": 15},
            "required_domains": {"type": "array", "items": {"type": "string"}},
            "exclude_domains": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["research_question"]
    }
)
```

### 4. Translation Pipeline Template

**Use Case**: High-quality translation with cultural adaptation

```python
TRANSLATION_PIPELINE_TEMPLATE = OrchestraTemplate(
    id="translation-pipeline",
    name="Translation Pipeline V3",
    description="Voice calibration → translate → back-translate → review → harmonize",
    version="3.0.0",
    phases=[
        Phase(
            id="voice_calibration",
            name="Voice & Style Calibration",
            description="Analyze source text style and voice for preservation",
            type=PhaseType.SEQUENTIAL,
            task_type="content",
            model_tier="sonnet",
            thinking_level="medium",
            timeout_minutes=15,
            min_confidence=0.7,
            quality_gates=["style_analysis"]
        ),
        Phase(
            id="initial_translation",
            name="Initial Translation",
            description="Translate content preserving style and meaning",
            type=PhaseType.SEQUENTIAL,
            task_type="translation",
            model_tier="opus",
            thinking_level="high",
            depends_on=["voice_calibration"],
            timeout_minutes=60,
            min_confidence=0.8,
            quality_gates=["translation_fluency", "meaning_preservation"]
        ),
        Phase(
            id="back_translation",
            name="Back Translation Verification",
            description="Translate back to source language to check meaning preservation",
            type=PhaseType.SEQUENTIAL,
            task_type="translation",
            model_tier="sonnet",
            thinking_level="medium",
            depends_on=["initial_translation"],
            timeout_minutes=30,
            min_confidence=0.8,
            quality_gates=["back_translation_divergence"]
        ),
        Phase(
            id="cultural_review",
            name="Cultural Adaptation Review",
            description="Review for cultural appropriateness and local context",
            type=PhaseType.SEQUENTIAL,
            task_type="review",
            model_tier="opus",
            thinking_level="high",
            depends_on=["initial_translation"],
            timeout_minutes=45,
            min_confidence=0.8,
            quality_gates=["cultural_appropriateness", "local_context"]
        ),
        Phase(
            id="harmonization",
            name="Final Harmonization",
            description="Incorporate feedback from back-translation and cultural review",
            type=PhaseType.CONDITIONAL,
            task_type="translation",
            model_tier="opus",
            thinking_level="high",
            depends_on=["back_translation", "cultural_review"],
            run_condition="back_translation.result.divergence_score > 0.2 or cultural_review.result.decision == 'revise'",
            timeout_minutes=30,
            min_confidence=0.9
        ),
        Phase(
            id="final_review",
            name="Final Quality Review",
            description="Final review of harmonized translation",
            type=PhaseType.SEQUENTIAL,
            task_type="review",
            model_tier="sonnet",
            thinking_level="high",
            depends_on=["harmonization", "cultural_review"],
            success_condition="any([harmonization.status == 'success', cultural_review.result.decision == 'approve'])",
            timeout_minutes=20,
            min_confidence=0.9,
            quality_gates=["final_quality_check"]
        )
    ],
    max_duration_hours=4,
    overall_confidence_threshold=0.85,
    config_schema={
        "type": "object",
        "properties": {
            "source_text": {"type": "string"},
            "source_language": {"type": "string", "pattern": "^[a-z]{2}(-[A-Z]{2})?$"},
            "target_language": {"type": "string", "pattern": "^[a-z]{2}(-[A-Z]{2})?$"},
            "target_region": {"type": "string", "description": "Target cultural region"},
            "style": {"type": "string", "enum": ["formal", "casual", "technical", "marketing", "literary"]},
            "domain": {"type": "string", "enum": ["general", "legal", "medical", "technical", "marketing"]},
            "glossary": {
                "type": "object",
                "description": "Term translations to enforce",
                "patternProperties": {
                    ".*": {"type": "string"}
                }
            },
            "cultural_sensitivity": {"type": "boolean", "default": true},
            "preserve_formatting": {"type": "boolean", "default": true}
        },
        "required": ["source_text", "source_language", "target_language"]
    }
)
```

### 5. Security Audit Template

**Use Case**: Comprehensive security analysis and remediation

```python
SECURITY_AUDIT_TEMPLATE = OrchestraTemplate(
    id="security-audit", 
    name="Security Audit Pipeline",
    description="Scan → analyze → report → remediate security vulnerabilities",
    version="1.0.0",
    phases=[
        Phase(
            id="static_analysis",
            name="Static Code Analysis",
            description="Scan code for security vulnerabilities",
            type=PhaseType.PARALLEL,
            task_type="code",
            model_tier="sonnet",
            thinking_level="medium",
            timeout_minutes=30,
            min_confidence=0.8,
            quality_gates=["sast_scan"]
        ),
        Phase(
            id="dependency_scan",
            name="Dependency Vulnerability Scan",
            description="Check dependencies for known vulnerabilities",
            type=PhaseType.PARALLEL,
            task_type="code",
            model_tier="haiku",
            thinking_level="low",
            timeout_minutes=15,
            min_confidence=0.9,
            quality_gates=["dependency_vulnerabilities"]
        ),
        Phase(
            id="configuration_review",
            name="Configuration Security Review",
            description="Review configuration files for security issues",
            type=PhaseType.PARALLEL,
            task_type="review",
            model_tier="sonnet",
            thinking_level="medium",
            timeout_minutes=20,
            min_confidence=0.8,
            quality_gates=["config_security"]
        ),
        Phase(
            id="threat_modeling",
            name="Threat Model Analysis",
            description="Analyze potential attack vectors and threats",
            type=PhaseType.SEQUENTIAL,
            task_type="review",
            model_tier="opus",
            thinking_level="high",
            depends_on=["static_analysis", "dependency_scan", "configuration_review"],
            timeout_minutes=45,
            min_confidence=0.8,
            quality_gates=["threat_analysis"]
        ),
        Phase(
            id="security_report",
            name="Security Assessment Report",
            description="Generate comprehensive security report with prioritized findings",
            type=PhaseType.SEQUENTIAL,
            task_type="content",
            model_tier="sonnet",
            thinking_level="medium",
            depends_on=["threat_modeling"],
            timeout_minutes=30,
            min_confidence=0.8,
            quality_gates=["report_completeness"]
        ),
        Phase(
            id="remediation_plan",
            name="Remediation Planning",
            description="Create actionable remediation plan for identified issues",
            type=PhaseType.SEQUENTIAL,
            task_type="code",
            model_tier="opus",
            thinking_level="high", 
            depends_on=["security_report"],
            timeout_minutes=60,
            min_confidence=0.9,
            quality_gates=["remediation_feasibility"]
        ),
        Phase(
            id="critical_fixes",
            name="Critical Issue Remediation",
            description="Automatically fix critical security issues where possible",
            type=PhaseType.CONDITIONAL,
            task_type="code",
            model_tier="opus",
            thinking_level="high",
            depends_on=["remediation_plan"],
            run_condition="security_report.result.critical_issues > 0",
            timeout_minutes=90,
            min_confidence=0.95,
            quality_gates=["fix_verification", "regression_testing"]
        )
    ],
    parallel_groups=[["static_analysis", "dependency_scan", "configuration_review"]],
    max_duration_hours=8,
    overall_confidence_threshold=0.85,
    config_schema={
        "type": "object",
        "properties": {
            "target_path": {"type": "string", "description": "Path to code/config to audit"},
            "scan_types": {
                "type": "array",
                "items": {"type": "string", "enum": ["sast", "dependencies", "secrets", "config", "iac"]},
                "default": ["sast", "dependencies", "config"]
            },
            "severity_threshold": {"type": "string", "enum": ["low", "medium", "high", "critical"], "default": "medium"},
            "auto_fix": {"type": "boolean", "default": false, "description": "Attempt to auto-fix critical issues"},
            "compliance_frameworks": {"type": "array", "items": {"type": "string", "enum": ["owasp", "nist", "cis"]}},
            "exclude_paths": {"type": "array", "items": {"type": "string"}},
            "include_archived": {"type": "boolean", "default": false}
        },
        "required": ["target_path"]
    }
)
```

## Template Engine

```python
class TemplateEngine:
    """Orchestration template execution engine."""
    
    def __init__(self, task_queue: 'TaskQueue', quality_gates: 'QualityGateManager'):
        self.task_queue = task_queue
        self.quality_gates = quality_gates
        self.templates = {}
        
        # Register built-in templates
        self._register_builtin_templates()
    
    def register_template(self, template: OrchestraTemplate):
        """Register a new orchestra template."""
        self.templates[template.id] = template
        
    def execute_template(
        self, 
        template_id: str, 
        config: Dict[str, Any],
        priority: int = 3
    ) -> str:
        """Execute an orchestra template with given configuration."""
        template = self.templates.get(template_id)
        if not template:
            raise ValueError(f"Template {template_id} not found")
            
        # Validate configuration
        if not template.validate_config(config):
            raise ValueError("Invalid template configuration")
            
        # Create orchestra record
        orchestra_id = self._create_orchestra(template, config, priority)
        
        # Generate execution plan
        execution_plan = template.get_execution_plan(config)
        
        # Submit initial phase tasks
        self._submit_initial_phases(orchestra_id, template, config, execution_plan)
        
        return orchestra_id
        
    def _create_orchestra(
        self, 
        template: OrchestraTemplate, 
        config: Dict[str, Any], 
        priority: int
    ) -> str:
        """Create new orchestra execution record."""
        orchestra_id = f"orch_{uuid4().hex[:12]}"
        
        self.task_queue.create_orchestra(
            orchestra_id=orchestra_id,
            template=template.id,
            config=config,
            priority=priority,
            cost_budget=template.cost_budget_usd,
            time_budget=template.max_duration_hours
        )
        
        return orchestra_id
        
    def _submit_initial_phases(
        self,
        orchestra_id: str,
        template: OrchestraTemplate,
        config: Dict[str, Any],
        execution_plan: List[List[str]]
    ):
        """Submit initial phases that have no dependencies."""
        initial_phase_batch = execution_plan[0] if execution_plan else []
        
        for phase_id in initial_phase_batch:
            phase = next(p for p in template.phases if p.id == phase_id)
            
            # Create task for this phase
            task_config = self._build_phase_task_config(phase, config, orchestra_id)
            
            task_id = self.task_queue.submit_task(
                task_type=phase.task_type,
                payload=task_config,
                orchestra_id=orchestra_id,
                orchestra_phase=phase_id,
                priority=config.get('priority', 3),
                model_tier=phase.model_tier,
                min_confidence=phase.min_confidence,
                timeout_seconds=phase.timeout_minutes * 60
            )
            
            # Track phase->task mapping
            self._track_phase_task(orchestra_id, phase_id, task_id)
    
    def on_phase_complete(self, orchestra_id: str, phase_id: str, result: TaskResult):
        """Handle completion of a phase - trigger dependent phases."""
        template = self._get_orchestra_template(orchestra_id)
        config = self._get_orchestra_config(orchestra_id)
        
        # Check if this phase success/failure affects the workflow
        phase = next(p for p in template.phases if p.id == phase_id)
        
        # Evaluate success condition if present
        if phase.success_condition:
            success = self._evaluate_condition(phase.success_condition, orchestra_id)
            if not success:
                self._fail_orchestra(orchestra_id, f"Phase {phase_id} success condition failed")
                return
                
        # Find phases that depend on this one
        dependent_phases = [p for p in template.phases if phase_id in p.depends_on]
        
        for dep_phase in dependent_phases:
            # Check if all dependencies are satisfied
            if self._dependencies_satisfied(dep_phase, orchestra_id):
                # Evaluate run condition
                should_run = True
                if dep_phase.run_condition:
                    should_run = self._evaluate_condition(dep_phase.run_condition, orchestra_id)
                    
                if should_run:
                    self._submit_phase(orchestra_id, dep_phase, config)
                else:
                    # Mark as skipped
                    self._mark_phase_skipped(orchestra_id, dep_phase.id)
        
        # Check if orchestra is complete
        if self._orchestra_complete(orchestra_id):
            self._complete_orchestra(orchestra_id)
    
    def _evaluate_condition(self, condition: str, orchestra_id: str) -> bool:
        """Evaluate a condition expression in the context of orchestra results."""
        # This would implement a safe expression evaluator
        # that can reference phase results and status
        pass
        
    def _dependencies_satisfied(self, phase: Phase, orchestra_id: str) -> bool:
        """Check if all dependencies for a phase are satisfied."""
        for dep_phase_id in phase.depends_on:
            dep_status = self._get_phase_status(orchestra_id, dep_phase_id)
            if dep_status not in ['success', 'skipped']:
                return False
        return True
```

## Custom Template Creation

```python
# Example: Custom template for blog post creation
BLOG_POST_TEMPLATE = OrchestraTemplate(
    id="blog-post-seo",
    name="SEO-Optimized Blog Post",
    description="Research → Write → Optimize → Review → Publish pipeline",
    version="1.0.0",
    phases=[
        Phase(
            id="keyword_research",
            name="SEO Keyword Research",
            type=PhaseType.SEQUENTIAL,
            task_type="research",
            model_tier="haiku",
            timeout_minutes=20,
            quality_gates=["keyword_relevance"]
        ),
        Phase(
            id="content_outline",
            name="Content Outline Creation",
            type=PhaseType.SEQUENTIAL,
            task_type="content",
            model_tier="sonnet",
            depends_on=["keyword_research"],
            timeout_minutes=15,
            quality_gates=["outline_structure"]
        ),
        Phase(
            id="draft_writing",
            name="Draft Writing",
            type=PhaseType.SEQUENTIAL,
            task_type="content",
            model_tier="opus",
            depends_on=["content_outline"],
            timeout_minutes=60,
            min_confidence=0.7,
            quality_gates=["content_quality", "seo_optimization"]
        ),
        Phase(
            id="seo_optimization",
            name="SEO Enhancement",
            type=PhaseType.SEQUENTIAL,
            task_type="content",
            model_tier="sonnet",
            depends_on=["draft_writing"],
            timeout_minutes=20,
            quality_gates=["seo_score", "readability"]
        ),
        Phase(
            id="final_review",
            name="Editorial Review",
            type=PhaseType.HUMAN_GATE,
            task_type="review",
            depends_on=["seo_optimization"],
            human_approval_required=True,
            human_timeout_minutes=720  # 12 hours
        )
    ],
    config_schema={
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "target_keywords": {"type": "array", "items": {"type": "string"}},
            "word_count": {"type": "integer", "minimum": 800, "maximum": 3000},
            "target_audience": {"type": "string"},
            "publish_date": {"type": "string", "format": "date"}
        },
        "required": ["topic", "target_keywords", "word_count"]
    }
)

# Register custom template
template_engine.register_template(BLOG_POST_TEMPLATE)

# Execute template
orchestra_id = template_engine.execute_template(
    template_id="blog-post-seo",
    config={
        "topic": "AI Agent Orchestration Best Practices",
        "target_keywords": ["AI agents", "orchestration", "automation", "workflow"],
        "word_count": 2000,
        "target_audience": "technical professionals",
        "publish_date": "2024-12-01"
    },
    priority=2
)
```

These orchestra templates provide **proven, reusable patterns** for complex multi-agent workflows with built-in quality assurance, dependency management, and error recovery.