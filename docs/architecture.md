# System Architecture

The Orchestration Engine is a **meta-coordination layer** that sits on top of OpenClaw to provide robust, scalable, and reliable multi-agent workflows.

## Core Philosophy

- **OpenClaw**: Provides the musicians (sub-agents, models, tools)
- **Orchestration Engine**: Provides the conductor's score (queue, retry, quality, templates)
- **Integration**: Seamless coordination via `sessions_spawn()` and structured communication

## High-Level Architecture

```ascii
┌─────────────────────────────────────────────────────────────────┐
│                    ORCHESTRATION ENGINE                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐            │
│  │ CLI Layer   │  │ REST API    │  │ Templates   │            │
│  │ orch submit │  │ /tasks      │  │ Content     │            │
│  │ orch status │  │ /orchestras │  │ Code Sprint │            │
│  │ orch run    │  │ /metrics    │  │ Research    │            │
│  └─────────────┘  └─────────────┘  └─────────────┘            │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐            │
│  │ Task Queue  │  │ Quality     │  │ Progress    │            │
│  │ SQLite      │  │ Gates       │  │ Streaming   │            │
│  │ Persistent  │  │ Validators  │  │ Real-time   │            │
│  │ Concurrent  │  │ Thresholds  │  │ Updates     │            │
│  └─────────────┘  └─────────────┘  └─────────────┘            │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐            │
│  │ Agent       │  │ Error       │  │ Metrics &   │            │
│  │ Memory      │  │ Recovery    │  │ Analytics   │            │
│  │ Episodic    │  │ Retry Logic │  │ Cost Track  │            │
│  │ Semantic    │  │ Escalation  │  │ Performance │            │
│  └─────────────┘  └─────────────┘  └─────────────┘            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                                   │
                   ┌───────────────▼───────────────┐
                   │           OpenClaw            │
                   │  ┌─────────┐  ┌─────────────┐ │
                   │  │Sub-Agent│  │Tool Access  │ │
                   │  │Spawning │  │web_search   │ │
                   │  │sessions_│  │web_fetch    │ │
                   │  │spawn()  │  │exec         │ │
                   │  └─────────┘  │message      │ │
                   │               │browser      │ │
                   │  ┌─────────┐  │...          │ │
                   │  │Model    │  └─────────────┘ │
                   │  │Tiers    │                  │
                   │  │Haiku 4.5│  ┌─────────────┐ │
                   │  │Sonnet 4 │  │Session      │ │
                   │  │Opus 4.6 │  │Management   │ │
                   │  └─────────┘  └─────────────┘ │
                   └───────────────────────────────┘
```

## Component Details

### 1. Task Queue (SQLite-Backed)

**Purpose**: Persistent, concurrent task scheduling with state management

**Database Schema**:
```sql
-- Core tables
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,          -- 'content', 'code', 'research', 'translation'
    priority INTEGER DEFAULT 3,  -- 1=critical, 2=high, 3=normal, 4=low
    status TEXT DEFAULT 'queued',-- 'queued', 'running', 'success', 'failed', 'retry'
    payload JSON NOT NULL,       -- Task-specific data
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    orchestra_id TEXT            -- Parent orchestra workflow
);

CREATE TABLE task_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(id),
    attempt_number INTEGER,
    model TEXT,                  -- 'haiku-4-5', 'sonnet-4', 'opus-4-6'
    session_id TEXT,             -- OpenClaw session ID
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    status TEXT,                 -- 'running', 'success', 'failed'
    result JSON,                 -- Structured output
    error_message TEXT,
    tokens_used INTEGER,
    cost_usd DECIMAL(10,4)
);

CREATE TABLE orchestras (
    id TEXT PRIMARY KEY,
    template TEXT NOT NULL,      -- 'content-pipeline', 'code-sprint', etc.
    status TEXT DEFAULT 'running',
    config JSON NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    total_tasks INTEGER DEFAULT 0,
    completed_tasks INTEGER DEFAULT 0,
    failed_tasks INTEGER DEFAULT 0
);
```

**State Transitions**:
- `queued` → `running` (worker picks up task)
- `running` → `success` (task completes successfully)  
- `running` → `failed` (task fails, no more retries)
- `running` → `retry` (task fails, will retry with backoff)
- `retry` → `queued` (after backoff delay)

**Concurrency Control**: Max 8 concurrent workers (configurable)

### 2. Structured Output Schemas (Pydantic Models)

**Purpose**: Type-safe, validated responses from all agent tasks

**Base Schema**:
```python
class TaskResult(BaseModel):
    task_id: str
    status: Literal["success", "failed", "partial"]
    confidence: float = Field(ge=0.0, le=1.0)  # 0.0-1.0 quality score
    result: Any  # Task-specific payload
    metadata: Dict[str, Any] = {}
    errors: List[str] = []
    created_at: datetime
    model_used: str
    tokens_consumed: int
```

**Task-Specific Schemas**:
- `CodeTaskResult`: Contains code, build status, test results, lint issues
- `ResearchTaskResult`: Contains findings, sources, citations, confidence per claim
- `ContentTaskResult`: Contains text, word count, readability score, fact-check status
- `TranslationTaskResult`: Contains translated text, back-translation, divergence score
- `ReviewTaskResult`: Contains feedback, score, approval status, suggested changes

### 3. Progress Streaming (Real-Time Updates)

**Methods**:
1. **File-based**: Workers update JSON progress files, main process polls
2. **sessions_send**: Direct updates to OpenClaw main session via API

**Progress Schema**:
```python
class ProgressUpdate(BaseModel):
    task_id: str
    orchestra_id: Optional[str]
    stage: str                   # "queued", "running", "validating", "complete"
    progress: float             # 0.0-1.0 completion percentage
    message: str                # Human-readable status
    timestamp: datetime
    metadata: Dict[str, Any] = {}
```

**Checkpoints**: Long-running tasks emit progress at 25%, 50%, 75%, completion

### 4. Error Recovery + Retry Logic

**Retry Strategy**:
- Exponential backoff: 1s, 2s, 4s, 8s, max 60s
- Max retries per task type (configurable)
- Dead letter queue for permanent failures

**Model Tier Escalation**:
1. **First attempt**: Haiku 4.5 (fast, cheap)
2. **First retry**: Sonnet 4 (better reasoning)
3. **Final retry**: Opus 4.6 (highest capability)

**Failure Classification**:
- **Transient**: Network errors, rate limits → retry
- **Permanent**: Invalid input, unsupported operation → dead letter queue
- **Quality**: Low confidence score → escalate model tier

### 5. Confidence Scoring (Per-Output Quality Assessment)

**Scoring Framework**:
```python
class ConfidenceScorer:
    def score_content(self, result: ContentTaskResult) -> float:
        # Fact-check accuracy, citation count, readability
        return weighted_average([fact_score, citation_score, readability_score])
    
    def score_code(self, result: CodeTaskResult) -> float:
        # Build success, test coverage, lint cleanliness
        return weighted_average([build_score, test_score, lint_score])
    
    def score_research(self, result: ResearchTaskResult) -> float:
        # Source diversity, citation validity, claim confidence
        return weighted_average([source_score, citation_score, claim_score])
```

**Thresholds**:
- Content: 0.8+ for publication
- Code: 0.9+ for production
- Research: 0.75+ for citation
- Translation: 0.85+ for back-translation match

### 6. Quality Gates (Task-Type Verification)

**Gate Definitions**:

**Code Quality Gate**:
- Build passes (`exit code 0`)
- Tests pass (coverage ≥ 80%)
- Lint clean (0 errors, <5 warnings)
- Security scan passes

**Content Quality Gate**:
- Fact-check agent verification
- Citation verification (URLs accessible)
- Word count within range
- Readability score acceptable

**Research Quality Gate**:
- Source count ≥ minimum threshold
- Citation URLs valid (HTTP 200)
- Confidence levels ≥ threshold
- No conflicting claims without acknowledgment

**Translation Quality Gate**:
- Back-translation divergence < 0.2
- Reviewer score ≥ threshold
- No untranslated segments
- Cultural context preserved

### 7. Critic/Reviewer Loops (Iterative Refinement)

**Loop Structure**:
1. **Generate**: Initial output from primary agent
2. **Review**: Critic agent evaluates output
3. **Feedback**: Structured critique with specific issues
4. **Refine**: Original agent incorporates feedback
5. **Validate**: Quality gate check
6. **Iterate**: Repeat until passes or max iterations reached

**Max Iterations**: 3 (configurable per task type)

### 8. Orchestra Templates (Reusable Patterns)

**Template Engine**:
```python
class OrchestraTemplate:
    name: str
    description: str
    phases: List[Phase]
    parallel_phases: List[List[str]]  # Phases that can run in parallel
    quality_gates: Dict[str, QualityGate]
    max_duration: timedelta
    cost_budget: Optional[Decimal]
```

**Built-in Templates**:

**Content Pipeline** (5 phases):
1. Research (web_search, fact collection)
2. Write (content generation)
3. Fact-Check (verification agent)
4. Fix (incorporate corrections)
5. Human Review (final approval gate)

**Code Sprint** (parallel workers):
- Multiple developers work on different features
- Git integration (branches, PRs, merges)
- Continuous testing and validation
- Integration testing at completion

**Deep Research** (multi-source synthesis):
- Parallel search across multiple sources
- Source credibility assessment
- Claim extraction and verification
- Synthesis with citation tracking
- Conflict resolution

### 9. Persistent Agent Memory

**Memory Types**:

**Episodic Memory**: Past task executions
```sql
CREATE TABLE memory_episodes (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    orchestra_id TEXT,
    outcome TEXT,              -- 'success', 'failure'
    lessons_learned JSON,      -- What went well/wrong
    context_tags TEXT[],       -- Searchable keywords
    similarity_embedding BLOB, -- Vector embedding for similarity search
    created_at TIMESTAMP
);
```

**Semantic Memory**: Facts and knowledge
```sql
CREATE TABLE memory_facts (
    id TEXT PRIMARY KEY,
    fact_text TEXT,
    confidence FLOAT,
    source TEXT,
    domain TEXT,              -- 'code', 'content', 'research'
    last_verified TIMESTAMP,
    embedding BLOB
);
```

**Procedural Memory**: Learned patterns
```sql
CREATE TABLE memory_procedures (
    id TEXT PRIMARY KEY,
    task_type TEXT,
    pattern_name TEXT,
    success_rate FLOAT,
    avg_tokens INTEGER,
    avg_duration INTERVAL,
    best_model TEXT,
    created_at TIMESTAMP
);
```

### 10. MCP Integration (Model Context Protocol)

**Purpose**: Structured tool sharing and context between sub-agents

**MCP Server Setup**:
- Orchestration engine runs MCP server
- Sub-agents connect as MCP clients
- Shared context, tools, and state

**Capabilities**:
- Tool discovery across agents
- Shared memory access
- Cross-agent communication
- Capability advertisement

**Authentication**: Session-based tokens for sub-agent access

### 11. Metrics Dashboard

**Tracked Metrics**:

**Per-Task Metrics**:
- Tokens consumed
- Runtime duration
- Model used
- Success/failure rate
- Cost in USD
- Retry count

**Per-Orchestra Metrics**:
- Total cost
- Total duration
- Success rate
- Task distribution
- Bottleneck identification

**Per-Model Metrics**:
- Average tokens per task type
- Average runtime
- Failure rate
- Cost efficiency

**Storage Schema**:
```sql
CREATE TABLE metrics (
    id TEXT PRIMARY KEY,
    metric_type TEXT,          -- 'task', 'orchestra', 'model'
    entity_id TEXT,            -- Task ID, orchestra ID, or model name
    metric_name TEXT,
    metric_value DECIMAL,
    timestamp TIMESTAMP,
    metadata JSON
);
```

### 12. Cost Tracking

**Granular Cost Tracking**:
- Per-task cost calculation
- Per-orchestra cost aggregation
- Per-model cost analysis
- Monthly/daily cost summaries

**Cost Alerts**:
- Budget thresholds per orchestra
- Daily/monthly spend limits
- Cost spike detection

## Integration with OpenClaw

### Session Management
```python
# Spawn sub-agent for task execution
session_id = sessions_spawn(
    model=selected_model_tier,
    thinking=thinking_level,
    label=f"orch-task-{task.id}"
)

# Send task to sub-agent
sessions_send(session_id, task_prompt)

# Monitor progress
while not complete:
    status = sessions_status(session_id)
    update_progress(task.id, status)
```

### Tool Access
Sub-agents inherit full OpenClaw tool access:
- `web_search`, `web_fetch` for research tasks
- `exec`, `process` for code tasks
- `write`, `edit`, `read` for content tasks
- `message` for communication tasks

### Model Selection
Orchestration engine selects appropriate model tier:
- **Haiku 4.5**: Simple, mechanical tasks
- **Sonnet 4**: Complex reasoning, synthesis
- **Opus 4.6**: Creative, high-stakes tasks

## Deployment Architecture

```ascii
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   CLI Client    │    │  Web Dashboard  │    │   REST API      │
│   orch submit   │    │   Status View   │    │   /api/v1/      │
│   orch status   │    │   Metrics       │    │   tasks         │
│   orch run      │    │   Logs          │    │   orchestras    │
└─────────────────┘    └─────────────────┘    └─────────────────┘
        │                       │                       │
        └───────────────────────┼───────────────────────┘
                                │
                ┌───────────────▼────────────────┐
                │      Orchestration Engine      │
                │                                │
                │  ┌──────────┐  ┌──────────┐   │
                │  │Task Queue│  │Quality   │   │
                │  │Worker    │  │Gates     │   │
                │  │Pool      │  │Validator │   │
                │  └──────────┘  └──────────┘   │
                │                                │
                │  ┌──────────┐  ┌──────────┐   │
                │  │Progress  │  │Memory    │   │
                │  │Monitor   │  │Manager   │   │
                │  └──────────┘  └──────────┘   │
                └───────────────┬────────────────┘
                                │
                    ┌───────────▼────────────────┐
                    │        OpenClaw             │
                    │                             │
                    │  Sub-Agent Orchestration    │
                    │  Model Tier Management      │
                    │  Tool Access Layer          │
                    │  Session Management         │
                    └─────────────────────────────┘
```

## Data Flow

1. **Task Submission**: CLI/API → Task Queue (SQLite)
2. **Task Pickup**: Worker → OpenClaw session spawn
3. **Execution**: Sub-agent → Tools → Results
4. **Quality Check**: Results → Quality Gates → Pass/Fail
5. **Retry/Success**: Fail → Retry Logic | Pass → Complete
6. **Metrics**: All stages → Metrics collection
7. **Memory**: Outcomes → Memory storage

This architecture ensures **reliable, scalable, and observable multi-agent coordination** with comprehensive error handling, quality assurance, and learning capabilities.