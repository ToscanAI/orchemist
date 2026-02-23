# Agent Memory System

> ⚠️ **Status: DEFERRED** — Memory system is planned for post-MVP. No code exists yet. Related issues #19–23 are all deferred. This document describes the intended design only.

The orchestration engine features a **persistent, multi-layered memory system** that enables agents to learn from past executions, accumulate knowledge, and improve performance across sessions.

## Memory Architecture

```ascii
┌─────────────────────────────────────────────────────────────────┐
│                    MEMORY SYSTEM LAYERS                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐            │
│  │ Episodic    │  │ Semantic    │  │ Procedural  │            │
│  │ Memory      │  │ Memory      │  │ Memory      │            │
│  │             │  │             │  │             │            │
│  │• Past Tasks │  │• Facts      │  │• Patterns   │            │
│  │• Outcomes   │  │• Knowledge  │  │• Best       │            │
│  │• Lessons    │  │• Context    │  │  Practices  │            │
│  │• Failures   │  │• Relations  │  │• Heuristics │            │
│  └─────────────┘  └─────────────┘  └─────────────┘            │
│         │                  │                  │                │
│         ▼                  ▼                  ▼                │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              STORAGE BACKEND                            │   │
│  │                                                         │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐    │   │
│  │  │ SQLite DB   │  │ Vector      │  │ Full-Text   │    │   │
│  │  │ Structured  │  │ Embeddings  │  │ Search      │    │   │
│  │  │ Relations   │  │ Similarity  │  │ Content     │    │   │
│  │  │ Indexing    │  │ Search      │  │ Indexing    │    │   │
│  │  └─────────────┘  └─────────────┘  └─────────────┘    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐            │
│  │ Memory      │  │ Learning    │  │ Cross-      │            │
│  │ Consolidation│  │ Engine      │  │ Session     │            │
│  │ • Cleanup   │  │ • Pattern   │  │ Transfer    │            │
│  │ • Archive   │  │   Detection │  │ • Context   │            │
│  │ • Merge     │  │ • Success   │  │   Sharing   │            │
│  │ • Prioritize│  │   Analysis  │  │ • Continuity│            │
│  └─────────────┘  └─────────────┘  └─────────────┘            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Memory Types

### 1. Episodic Memory - Past Task Executions

**Purpose**: Store detailed records of individual task executions with outcomes and lessons learned.

```python
from typing import Optional, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel
import numpy as np

class TaskExecution(BaseModel):
    """Record of a single task execution."""
    execution_id: str
    task_id: str
    task_type: str
    orchestra_id: Optional[str] = None
    
    # Context
    input_payload: Dict[str, Any]
    model_used: str
    thinking_level: str
    
    # Execution details
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    
    # Outcome
    status: str  # 'success', 'failed', 'timeout'
    confidence: Optional[float] = None
    output_result: Optional[Dict[str, Any]] = None
    
    # Resource usage
    tokens_consumed: Optional[int] = None
    cost_usd: Optional[float] = None
    
    # Quality metrics
    quality_gate_results: List[Dict[str, Any]] = []
    retry_count: int = 0
    
    # Learning data
    what_worked: List[str] = []
    what_failed: List[str] = []
    lessons_learned: List[str] = []
    context_tags: List[str] = []
    
    # Embeddings for similarity search
    input_embedding: Optional[List[float]] = None
    output_embedding: Optional[List[float]] = None
    
class EpisodicMemory:
    """Manages episodic memory for task executions."""
    
    def __init__(self, db_connection):
        self.db = db_connection
        self.embedding_model = self._init_embedding_model()
        
    def store_execution(self, execution: TaskExecution):
        """Store a task execution record."""
        # Generate embeddings
        if execution.input_payload:
            execution.input_embedding = self._generate_embedding(
                json.dumps(execution.input_payload)
            )
            
        if execution.output_result:
            execution.output_embedding = self._generate_embedding(
                json.dumps(execution.output_result)
            )
            
        # Store in database
        self.db.execute("""
            INSERT INTO episodic_memory (
                execution_id, task_id, task_type, orchestra_id,
                input_payload, model_used, thinking_level,
                started_at, completed_at, duration_seconds,
                status, confidence, output_result,
                tokens_consumed, cost_usd, quality_gate_results,
                retry_count, what_worked, what_failed, lessons_learned,
                context_tags, input_embedding, output_embedding
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            execution.execution_id, execution.task_id, execution.task_type,
            execution.orchestra_id, json.dumps(execution.input_payload),
            execution.model_used, execution.thinking_level,
            execution.started_at, execution.completed_at, execution.duration_seconds,
            execution.status, execution.confidence, json.dumps(execution.output_result),
            execution.tokens_consumed, execution.cost_usd,
            json.dumps(execution.quality_gate_results), execution.retry_count,
            json.dumps(execution.what_worked), json.dumps(execution.what_failed),
            json.dumps(execution.lessons_learned), json.dumps(execution.context_tags),
            execution.input_embedding, execution.output_embedding
        ))
        
    def find_similar_executions(
        self, 
        task_type: str, 
        input_payload: Dict[str, Any], 
        limit: int = 5
    ) -> List[TaskExecution]:
        """Find similar past executions using embedding similarity."""
        query_embedding = self._generate_embedding(json.dumps(input_payload))
        
        # This would use vector similarity search
        # For SQLite, we can use a simple approach or integrate with vector extensions
        similar_executions = self.db.execute("""
            SELECT * FROM episodic_memory 
            WHERE task_type = ? 
            AND status = 'success'
            ORDER BY created_at DESC 
            LIMIT ?
        """, (task_type, limit)).fetchall()
        
        return [self._row_to_execution(row) for row in similar_executions]
        
    def get_success_patterns(self, task_type: str) -> Dict[str, Any]:
        """Analyze success patterns for a task type."""
        executions = self.db.execute("""
            SELECT model_used, thinking_level, confidence, duration_seconds,
                   what_worked, quality_gate_results
            FROM episodic_memory 
            WHERE task_type = ? AND status = 'success'
        """, (task_type,)).fetchall()
        
        if not executions:
            return {}
            
        # Analyze patterns
        model_success_rates = {}
        thinking_level_performance = {}
        
        for row in executions:
            model = row['model_used']
            thinking = row['thinking_level']
            confidence = row['confidence'] or 0.0
            
            if model not in model_success_rates:
                model_success_rates[model] = []
            model_success_rates[model].append(confidence)
            
            if thinking not in thinking_level_performance:
                thinking_level_performance[thinking] = []
            thinking_level_performance[thinking].append(confidence)
            
        return {
            'best_model': max(model_success_rates, key=lambda m: np.mean(model_success_rates[m])),
            'best_thinking_level': max(thinking_level_performance, key=lambda t: np.mean(thinking_level_performance[t])),
            'avg_confidence_by_model': {m: np.mean(scores) for m, scores in model_success_rates.items()},
            'sample_count': len(executions)
        }
```

### 2. Semantic Memory - Facts and Knowledge

**Purpose**: Store factual knowledge, relationships, and domain-specific information accumulated across tasks.

```python
class Fact(BaseModel):
    """A single piece of factual knowledge."""
    fact_id: str
    statement: str
    domain: str  # 'code', 'content', 'research', 'general'
    confidence: float = Field(ge=0.0, le=1.0)
    source: Optional[str] = None
    
    # Verification
    verified_at: Optional[datetime] = None
    verification_source: Optional[str] = None
    
    # Relations
    related_facts: List[str] = []  # Other fact IDs
    contradicts: List[str] = []    # Contradictory fact IDs
    
    # Context
    context_tags: List[str] = []
    learned_from_task: Optional[str] = None
    
    # Embedding for semantic search
    embedding: Optional[List[float]] = None
    
class Relationship(BaseModel):
    """Relationship between two entities or facts."""
    relation_id: str
    subject: str
    predicate: str  # 'is_a', 'part_of', 'causes', 'enables', etc.
    object: str
    confidence: float = Field(ge=0.0, le=1.0)
    context: Optional[str] = None
    
class SemanticMemory:
    """Manages semantic knowledge and relationships."""
    
    def __init__(self, db_connection):
        self.db = db_connection
        self.embedding_model = self._init_embedding_model()
        
    def store_fact(self, fact: Fact):
        """Store a new fact in semantic memory."""
        # Generate embedding
        fact.embedding = self._generate_embedding(fact.statement)
        
        # Check for existing similar facts to avoid duplicates
        similar_facts = self.find_similar_facts(fact.statement, threshold=0.95)
        if similar_facts:
            # Update existing fact instead of creating duplicate
            existing_fact = similar_facts[0]
            self.update_fact_confidence(existing_fact.fact_id, 
                                      max(existing_fact.confidence, fact.confidence))
            return existing_fact.fact_id
            
        # Store new fact
        self.db.execute("""
            INSERT INTO semantic_facts (
                fact_id, statement, domain, confidence, source,
                verified_at, verification_source, related_facts,
                contradicts, context_tags, learned_from_task, embedding
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            fact.fact_id, fact.statement, fact.domain, fact.confidence,
            fact.source, fact.verified_at, fact.verification_source,
            json.dumps(fact.related_facts), json.dumps(fact.contradicts),
            json.dumps(fact.context_tags), fact.learned_from_task,
            fact.embedding
        ))
        
        return fact.fact_id
        
    def find_similar_facts(
        self, 
        query: str, 
        domain: Optional[str] = None,
        threshold: float = 0.8,
        limit: int = 10
    ) -> List[Fact]:
        """Find facts similar to the query."""
        query_embedding = self._generate_embedding(query)
        
        # Vector similarity search would go here
        # For now, use simple text search
        where_clause = "WHERE 1=1"
        params = []
        
        if domain:
            where_clause += " AND domain = ?"
            params.append(domain)
            
        facts = self.db.execute(f"""
            SELECT * FROM semantic_facts {where_clause}
            ORDER BY confidence DESC
            LIMIT ?
        """, params + [limit]).fetchall()
        
        return [self._row_to_fact(row) for row in facts]
        
    def store_relationship(self, relationship: Relationship):
        """Store a relationship between entities."""
        self.db.execute("""
            INSERT OR REPLACE INTO semantic_relationships (
                relation_id, subject, predicate, object,
                confidence, context
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            relationship.relation_id, relationship.subject,
            relationship.predicate, relationship.object,
            relationship.confidence, relationship.context
        ))
        
    def query_knowledge(self, query: str, domain: Optional[str] = None) -> Dict[str, Any]:
        """Query semantic knowledge with natural language."""
        # Find relevant facts
        facts = self.find_similar_facts(query, domain=domain)
        
        # Find relevant relationships
        relationships = self._find_related_relationships([f.fact_id for f in facts])
        
        return {
            'facts': facts,
            'relationships': relationships,
            'confidence': np.mean([f.confidence for f in facts]) if facts else 0.0
        }
        
    def consolidate_knowledge(self):
        """Consolidate and clean up semantic knowledge."""
        # Merge similar facts
        self._merge_similar_facts()
        
        # Remove low-confidence facts that haven't been verified
        self.db.execute("""
            DELETE FROM semantic_facts 
            WHERE confidence < 0.3 AND verified_at IS NULL
            AND learned_from_task NOT IN (
                SELECT task_id FROM episodic_memory 
                WHERE created_at > date('now', '-7 days')
            )
        """)
        
        # Update fact confidence based on usage
        self._update_fact_usage_scores()
```

### 3. Procedural Memory - Learned Patterns

**Purpose**: Store learned patterns, best practices, and procedural knowledge for task execution.

```python
class TaskPattern(BaseModel):
    """Learned pattern for task execution."""
    pattern_id: str
    task_type: str
    pattern_name: str
    description: str
    
    # Pattern definition
    input_conditions: Dict[str, Any]  # When to apply this pattern
    execution_steps: List[Dict[str, Any]]  # How to execute
    success_criteria: Dict[str, Any]  # How to measure success
    
    # Performance metrics
    usage_count: int = 0
    success_rate: float = 0.0
    avg_confidence: float = 0.0
    avg_tokens: int = 0
    avg_duration_seconds: float = 0.0
    
    # Model/strategy preferences
    preferred_model: Optional[str] = None
    preferred_thinking_level: Optional[str] = None
    quality_gates: List[str] = []
    
    # Learning metadata
    created_at: datetime = Field(default_factory=datetime.now)
    last_used: Optional[datetime] = None
    learned_from_executions: List[str] = []
    
class ProceduralMemory:
    """Manages procedural knowledge and patterns."""
    
    def __init__(self, db_connection):
        self.db = db_connection
        self.pattern_detector = PatternDetector()
        
    def learn_patterns(self, task_type: str, min_executions: int = 5):
        """Analyze recent executions to learn new patterns."""
        # Get recent successful executions
        executions = self.db.execute("""
            SELECT * FROM episodic_memory 
            WHERE task_type = ? AND status = 'success'
            AND created_at > date('now', '-30 days')
            ORDER BY confidence DESC, created_at DESC
        """, (task_type,)).fetchall()
        
        if len(executions) < min_executions:
            return
            
        # Group executions by similarity
        execution_groups = self.pattern_detector.group_similar_executions(executions)
        
        for group in execution_groups:
            if len(group) >= min_executions:
                pattern = self._extract_pattern_from_group(group, task_type)
                if pattern:
                    self.store_pattern(pattern)
                    
    def store_pattern(self, pattern: TaskPattern):
        """Store a learned task pattern."""
        self.db.execute("""
            INSERT OR REPLACE INTO procedural_patterns (
                pattern_id, task_type, pattern_name, description,
                input_conditions, execution_steps, success_criteria,
                usage_count, success_rate, avg_confidence,
                avg_tokens, avg_duration_seconds,
                preferred_model, preferred_thinking_level,
                quality_gates, created_at, last_used,
                learned_from_executions
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pattern.pattern_id, pattern.task_type, pattern.pattern_name,
            pattern.description, json.dumps(pattern.input_conditions),
            json.dumps(pattern.execution_steps), json.dumps(pattern.success_criteria),
            pattern.usage_count, pattern.success_rate, pattern.avg_confidence,
            pattern.avg_tokens, pattern.avg_duration_seconds,
            pattern.preferred_model, pattern.preferred_thinking_level,
            json.dumps(pattern.quality_gates), pattern.created_at,
            pattern.last_used, json.dumps(pattern.learned_from_executions)
        ))
        
    def find_applicable_patterns(
        self, 
        task_type: str, 
        input_payload: Dict[str, Any]
    ) -> List[TaskPattern]:
        """Find patterns applicable to current task."""
        patterns = self.db.execute("""
            SELECT * FROM procedural_patterns 
            WHERE task_type = ? AND success_rate > 0.7
            ORDER BY success_rate DESC, usage_count DESC
        """, (task_type,)).fetchall()
        
        applicable_patterns = []
        for row in patterns:
            pattern = self._row_to_pattern(row)
            if self._pattern_matches_input(pattern, input_payload):
                applicable_patterns.append(pattern)
                
        return applicable_patterns
        
    def get_best_strategy(self, task_type: str, input_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Get the best execution strategy based on learned patterns."""
        patterns = self.find_applicable_patterns(task_type, input_payload)
        
        if not patterns:
            # Fall back to general statistics
            return self._get_default_strategy(task_type)
            
        # Use the best pattern
        best_pattern = patterns[0]
        
        return {
            'model': best_pattern.preferred_model,
            'thinking_level': best_pattern.preferred_thinking_level,
            'quality_gates': best_pattern.quality_gates,
            'expected_tokens': best_pattern.avg_tokens,
            'expected_duration': best_pattern.avg_duration_seconds,
            'confidence_prediction': best_pattern.avg_confidence,
            'pattern_used': best_pattern.pattern_id
        }
        
    def update_pattern_performance(self, pattern_id: str, execution: TaskExecution):
        """Update pattern performance based on new execution."""
        pattern = self.get_pattern(pattern_id)
        if not pattern:
            return
            
        # Update statistics
        old_count = pattern.usage_count
        new_count = old_count + 1
        
        # Update running averages
        if execution.status == 'success':
            pattern.success_rate = ((pattern.success_rate * old_count) + 1.0) / new_count
        else:
            pattern.success_rate = (pattern.success_rate * old_count) / new_count
            
        if execution.confidence:
            pattern.avg_confidence = ((pattern.avg_confidence * old_count) + execution.confidence) / new_count
            
        if execution.tokens_consumed:
            pattern.avg_tokens = int(((pattern.avg_tokens * old_count) + execution.tokens_consumed) / new_count)
            
        if execution.duration_seconds:
            pattern.avg_duration_seconds = ((pattern.avg_duration_seconds * old_count) + execution.duration_seconds) / new_count
            
        pattern.usage_count = new_count
        pattern.last_used = datetime.now()
        
        # Store updated pattern
        self.store_pattern(pattern)
```

## Storage Backend

### SQLite Schema

```sql
-- Episodic Memory: Past task executions
CREATE TABLE episodic_memory (
    execution_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    orchestra_id TEXT,
    
    -- Input context
    input_payload TEXT NOT NULL,  -- JSON
    model_used TEXT NOT NULL,
    thinking_level TEXT,
    
    -- Timing
    started_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,
    duration_seconds REAL,
    
    -- Outcome
    status TEXT NOT NULL,  -- 'success', 'failed', 'timeout'
    confidence REAL,
    output_result TEXT,    -- JSON
    
    -- Resources
    tokens_consumed INTEGER,
    cost_usd DECIMAL(10,4),
    
    -- Quality and learning
    quality_gate_results TEXT,  -- JSON array
    retry_count INTEGER DEFAULT 0,
    what_worked TEXT,           -- JSON array
    what_failed TEXT,           -- JSON array
    lessons_learned TEXT,       -- JSON array
    context_tags TEXT,          -- JSON array
    
    -- Embeddings (could be BLOB or TEXT depending on implementation)
    input_embedding BLOB,
    output_embedding BLOB,
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Semantic Memory: Facts and knowledge
CREATE TABLE semantic_facts (
    fact_id TEXT PRIMARY KEY,
    statement TEXT NOT NULL,
    domain TEXT NOT NULL,
    confidence REAL NOT NULL,
    source TEXT,
    
    verified_at TIMESTAMP,
    verification_source TEXT,
    
    related_facts TEXT,      -- JSON array of fact IDs
    contradicts TEXT,        -- JSON array of fact IDs
    context_tags TEXT,       -- JSON array
    learned_from_task TEXT,  -- Task ID where this was learned
    
    embedding BLOB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Semantic relationships
CREATE TABLE semantic_relationships (
    relation_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    confidence REAL NOT NULL,
    context TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Procedural Memory: Learned patterns
CREATE TABLE procedural_patterns (
    pattern_id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    pattern_name TEXT NOT NULL,
    description TEXT NOT NULL,
    
    -- Pattern definition
    input_conditions TEXT NOT NULL,  -- JSON
    execution_steps TEXT NOT NULL,   -- JSON array
    success_criteria TEXT NOT NULL,  -- JSON
    
    -- Performance metrics
    usage_count INTEGER DEFAULT 0,
    success_rate REAL DEFAULT 0.0,
    avg_confidence REAL DEFAULT 0.0,
    avg_tokens INTEGER DEFAULT 0,
    avg_duration_seconds REAL DEFAULT 0.0,
    
    -- Preferences
    preferred_model TEXT,
    preferred_thinking_level TEXT,
    quality_gates TEXT,  -- JSON array
    
    -- Metadata
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used TIMESTAMP,
    learned_from_executions TEXT  -- JSON array of execution IDs
);

-- Indexes for performance
CREATE INDEX idx_episodic_task_type ON episodic_memory(task_type, status);
CREATE INDEX idx_episodic_orchestra ON episodic_memory(orchestra_id);
CREATE INDEX idx_episodic_created ON episodic_memory(created_at DESC);

CREATE INDEX idx_semantic_domain ON semantic_facts(domain, confidence DESC);
CREATE INDEX idx_semantic_context ON semantic_facts(context_tags);

CREATE INDEX idx_procedural_task ON procedural_patterns(task_type, success_rate DESC);
CREATE INDEX idx_procedural_usage ON procedural_patterns(usage_count DESC, last_used DESC);
```

## Cross-Session Learning

```python
class CrossSessionLearning:
    """Manages learning and knowledge transfer across sessions."""
    
    def __init__(self, memory_system):
        self.memory = memory_system
        self.learning_engine = LearningEngine(memory_system)
        
    def on_task_complete(self, execution: TaskExecution):
        """Process completed task for learning."""
        # Store in episodic memory
        self.memory.episodic.store_execution(execution)
        
        # Extract semantic knowledge
        if execution.status == 'success' and execution.output_result:
            self._extract_semantic_knowledge(execution)
            
        # Update procedural patterns
        self._update_procedural_knowledge(execution)
        
    def _extract_semantic_knowledge(self, execution: TaskExecution):
        """Extract facts and relationships from successful executions."""
        # This would implement knowledge extraction logic
        # For research tasks: extract facts and citations
        # For code tasks: extract patterns and best practices
        # For content tasks: extract domain knowledge
        
        if execution.task_type == 'research':
            self._extract_research_knowledge(execution)
        elif execution.task_type == 'code':
            self._extract_code_knowledge(execution)
        elif execution.task_type == 'content':
            self._extract_content_knowledge(execution)
            
    def _extract_research_knowledge(self, execution: TaskExecution):
        """Extract facts from research task results."""
        result = execution.output_result
        if not result or 'result' not in result:
            return
            
        findings = result['result'].get('findings', [])
        for finding in findings:
            fact = Fact(
                fact_id=f"fact_{uuid4().hex[:12]}",
                statement=finding.get('claim', ''),
                domain='research',
                confidence=finding.get('confidence', 0.5),
                context_tags=[execution.task_type],
                learned_from_task=execution.task_id
            )
            self.memory.semantic.store_fact(fact)
            
    def consolidate_memory(self):
        """Periodic memory consolidation and cleanup."""
        # Clean up low-value episodic memories
        cutoff_date = datetime.now() - timedelta(days=90)
        self.memory.db.execute("""
            DELETE FROM episodic_memory 
            WHERE created_at < ? 
            AND status = 'failed' 
            AND confidence IS NULL
        """, (cutoff_date,))
        
        # Consolidate semantic knowledge
        self.memory.semantic.consolidate_knowledge()
        
        # Update procedural patterns
        for task_type in ['code', 'content', 'research', 'translation']:
            self.memory.procedural.learn_patterns(task_type)
            
    def get_context_for_task(self, task_type: str, input_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Get relevant context from memory for a new task."""
        # Find similar past executions
        similar_executions = self.memory.episodic.find_similar_executions(
            task_type, input_payload, limit=3
        )
        
        # Get relevant semantic knowledge
        query_text = json.dumps(input_payload)
        semantic_context = self.memory.semantic.query_knowledge(query_text, domain=task_type)
        
        # Get best execution strategy
        strategy = self.memory.procedural.get_best_strategy(task_type, input_payload)
        
        return {
            'similar_executions': similar_executions,
            'relevant_knowledge': semantic_context,
            'recommended_strategy': strategy,
            'lessons_learned': self._extract_relevant_lessons(similar_executions)
        }
        
    def _extract_relevant_lessons(self, executions: List[TaskExecution]) -> List[str]:
        """Extract relevant lessons from similar executions."""
        lessons = []
        for execution in executions:
            lessons.extend(execution.lessons_learned)
            if execution.status == 'failed':
                lessons.extend([f"Avoid: {failure}" for failure in execution.what_failed])
            else:
                lessons.extend([f"Success factor: {success}" for success in execution.what_worked])
                
        # Deduplicate and rank by frequency
        lesson_counts = {}
        for lesson in lessons:
            lesson_counts[lesson] = lesson_counts.get(lesson, 0) + 1
            
        return sorted(lesson_counts.keys(), key=lambda l: lesson_counts[l], reverse=True)[:10]
```

## Memory Management and CLI

```bash
# Memory management commands
orch memory status                    # Show memory statistics
orch memory consolidate              # Run memory consolidation
orch memory query "AI agents"       # Query semantic knowledge
orch memory patterns --task-type code  # Show learned patterns
orch memory lessons --task-type research  # Show lessons learned
orch memory cleanup --older-than 90d    # Clean up old memories
orch memory export --format json        # Export memory data
orch memory import --file memory.json   # Import memory data
```

The memory system enables **continuous learning and improvement** across all orchestration tasks, making the system smarter and more efficient over time through accumulated experience and knowledge.