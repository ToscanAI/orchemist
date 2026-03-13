# Metrics and Analytics

> ⚠️ **Status: PARTIALLY IMPLEMENTED** — The advanced "metrics dashboard" described below remains deferred. However, several foundational metric capabilities are now live:
>
> - **Cost tracking**: `cost_tracker.py` — per-phase, per-run, per-day cost aggregation with budget caps and alerts. REST API endpoints at `/api/v1/costs/daily` and `/api/v1/costs/run/{run_id}`.
> - **Confidence scoring**: `confidence.py` — `ConfidenceCalculator` with weighted composite scoring (`acceptance_pass_rate`, `test_pass_rate`, `review_quality`).
> - **Trust profiles**: `trust.py` — per-(repo, template, task_type) trust calibration with decay and history. REST API endpoints at `/api/v1/trust/`.
> - **Run analytics**: `db.py` stores run duration, phase timing, token counts, and scoring results across 22+ tables.
>
> What remains deferred: the unified analytics UI, trend visualizations, failure clustering, and the advanced export/alerting described below.

The orchestration engine provides **comprehensive metrics collection and analysis** for tasks, orchestras, models, and system performance to enable data-driven optimization and monitoring.

## Metrics Architecture

```ascii
┌─────────────────────────────────────────────────────────────────┐
│                    METRICS COLLECTION                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐            │
│  │ Task        │  │ Orchestra   │  │ Model       │            │
│  │ Metrics     │  │ Metrics     │  │ Performance │            │
│  │             │  │             │  │             │            │
│  │• Tokens     │  │• Total Cost │  │• Avg Tokens │            │
│  │• Runtime    │  │• Duration   │  │• Success    │            │
│  │• Success    │  │• Task Count │  │  Rate       │            │
│  │• Quality    │  │• Success %  │  │• Avg Cost   │            │
│  │• Cost       │  │• Bottleneck │  │• Speed      │            │
│  └─────────────┘  └─────────────┘  └─────────────┘            │
│         │                  │                  │                │
│         ▼                  ▼                  ▼                │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              METRICS STORAGE                            │   │
│  │                                                         │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐    │   │
│  │  │ Time-Series │  │ Aggregated  │  │ Event       │    │   │
│  │  │ Data        │  │ Summaries   │  │ Logs        │    │   │
│  │  │ SQLite      │  │ Daily/      │  │ Failures    │    │   │
│  │  │ Tables      │  │ Monthly     │  │ Anomalies   │    │   │
│  │  └─────────────┘  └─────────────┘  └─────────────┘    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐            │
│  │ Analytics   │  │ Dashboards  │  │ Alerts      │            │
│  │ Engine      │  │ & Reports   │  │ & Anomaly   │            │
│  │             │  │             │  │ Detection   │            │
│  │• Trends     │  │• CLI        │  │• Cost       │            │
│  │• Patterns   │  │• Markdown   │  │  Spikes     │            │
│  │• Forecasts  │  │• JSON       │  │• Failure    │            │
│  │• Insights   │  │• Charts     │  │  Patterns   │            │
│  └─────────────┘  └─────────────┘  └─────────────┘            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Metrics Data Models

### Core Metrics Schema

```python
from typing import Optional, Dict, List, Any
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pydantic import BaseModel, Field

class MetricType(str, Enum):
    COUNTER = "counter"      # Monotonically increasing (task count, tokens)
    GAUGE = "gauge"          # Point-in-time value (queue depth, active workers)
    HISTOGRAM = "histogram"  # Distribution (latency, cost distribution)
    DURATION = "duration"    # Time-based measurements
    RATE = "rate"           # Events per time period

class MetricScope(str, Enum):
    TASK = "task"
    ORCHESTRA = "orchestra"
    MODEL = "model"
    SYSTEM = "system"
    QUALITY_GATE = "quality_gate"

class Metric(BaseModel):
    """Individual metric data point."""
    metric_id: str
    name: str
    scope: MetricScope
    metric_type: MetricType
    value: float
    unit: str
    
    # Context
    entity_id: str  # Task ID, orchestra ID, model name, etc.
    entity_type: str
    tags: Dict[str, str] = {}
    
    # Timing
    timestamp: datetime = Field(default_factory=datetime.now)
    
    # Optional metadata
    metadata: Dict[str, Any] = {}

class TaskMetrics(BaseModel):
    """Comprehensive metrics for a single task execution."""
    task_id: str
    task_type: str
    orchestra_id: Optional[str] = None
    
    # Performance metrics
    tokens_consumed: int
    execution_time_seconds: float
    queue_wait_time_seconds: float
    model_used: str
    thinking_level: str
    
    # Cost metrics
    cost_usd: Decimal
    cost_per_token: Decimal
    
    # Quality metrics
    confidence: float
    quality_gate_scores: Dict[str, float]
    retry_count: int
    
    # Success metrics
    status: str  # 'success', 'failed', 'timeout'
    error_type: Optional[str] = None
    
    # Resource metrics
    memory_peak_mb: Optional[int] = None
    cpu_usage_percent: Optional[float] = None
    
    # Timestamps
    created_at: datetime
    started_at: datetime
    completed_at: datetime
    
    @property
    def tokens_per_second(self) -> float:
        """Calculate tokens processed per second."""
        if self.execution_time_seconds > 0:
            return self.tokens_consumed / self.execution_time_seconds
        return 0.0
        
    @property
    def cost_per_minute(self) -> float:
        """Calculate cost per minute of execution."""
        if self.execution_time_seconds > 0:
            return float(self.cost_usd) / (self.execution_time_seconds / 60)
        return 0.0

class OrchestraMetrics(BaseModel):
    """Metrics for an entire orchestra execution."""
    orchestra_id: str
    template: str
    
    # Timing metrics
    total_duration_seconds: float
    avg_task_duration_seconds: float
    critical_path_duration_seconds: float
    
    # Task metrics
    total_tasks: int
    completed_tasks: int
    failed_tasks: int
    skipped_tasks: int
    success_rate: float
    
    # Cost metrics
    total_cost_usd: Decimal
    avg_cost_per_task: Decimal
    cost_efficiency_score: float  # Output value / cost
    
    # Quality metrics
    avg_confidence: float
    avg_quality_score: float
    quality_gates_passed: int
    quality_gates_failed: int
    
    # Resource metrics
    total_tokens_consumed: int
    peak_concurrent_tasks: int
    
    # Bottleneck analysis
    slowest_phase: str
    bottleneck_duration_seconds: float
    parallelization_efficiency: float
    
    # Timestamps
    started_at: datetime
    completed_at: datetime
    
class ModelMetrics(BaseModel):
    """Performance metrics for a specific model."""
    model_name: str
    
    # Usage statistics (time period: last 24h, 7d, 30d)
    total_tasks: int
    successful_tasks: int
    failed_tasks: int
    success_rate: float
    
    # Performance averages
    avg_tokens_per_task: float
    avg_duration_seconds: float
    avg_confidence: float
    avg_cost_per_task: Decimal
    
    # Distribution percentiles
    p50_duration_seconds: float
    p95_duration_seconds: float
    p99_duration_seconds: float
    
    # Task type breakdown
    task_type_distribution: Dict[str, int]
    task_type_success_rates: Dict[str, float]
    
    # Quality performance
    avg_quality_gate_score: float
    quality_gate_pass_rate: float
    
    # Cost efficiency
    tokens_per_dollar: float
    cost_efficiency_rank: int  # Among all models
```

## Metrics Collection System

### Metrics Collector

```python
class MetricsCollector:
    """Central metrics collection system."""
    
    def __init__(self, db_connection):
        self.db = db_connection
        self.buffer = []  # For batch processing
        self.buffer_size = 100
        
    def record_task_start(self, task_id: str, task_type: str, model: str):
        """Record task execution start."""
        self._record_metric(Metric(
            metric_id=f"task_{task_id}_start",
            name="task_started",
            scope=MetricScope.TASK,
            metric_type=MetricType.COUNTER,
            value=1.0,
            unit="count",
            entity_id=task_id,
            entity_type="task",
            tags={"task_type": task_type, "model": model}
        ))
        
    def record_task_completion(self, task_metrics: TaskMetrics):
        """Record comprehensive task completion metrics."""
        base_tags = {
            "task_type": task_metrics.task_type,
            "model": task_metrics.model_used,
            "status": task_metrics.status,
            "thinking_level": task_metrics.thinking_level
        }
        
        if task_metrics.orchestra_id:
            base_tags["orchestra_id"] = task_metrics.orchestra_id
            
        # Record multiple metrics for this task
        metrics = [
            Metric(
                metric_id=f"task_{task_metrics.task_id}_tokens",
                name="task_tokens_consumed",
                scope=MetricScope.TASK,
                metric_type=MetricType.COUNTER,
                value=float(task_metrics.tokens_consumed),
                unit="tokens",
                entity_id=task_metrics.task_id,
                entity_type="task",
                tags=base_tags,
                timestamp=task_metrics.completed_at
            ),
            Metric(
                metric_id=f"task_{task_metrics.task_id}_duration",
                name="task_execution_duration",
                scope=MetricScope.TASK,
                metric_type=MetricType.DURATION,
                value=task_metrics.execution_time_seconds,
                unit="seconds",
                entity_id=task_metrics.task_id,
                entity_type="task",
                tags=base_tags,
                timestamp=task_metrics.completed_at
            ),
            Metric(
                metric_id=f"task_{task_metrics.task_id}_cost",
                name="task_cost",
                scope=MetricScope.TASK,
                metric_type=MetricType.COUNTER,
                value=float(task_metrics.cost_usd),
                unit="usd",
                entity_id=task_metrics.task_id,
                entity_type="task",
                tags=base_tags,
                timestamp=task_metrics.completed_at
            ),
            Metric(
                metric_id=f"task_{task_metrics.task_id}_confidence",
                name="task_confidence",
                scope=MetricScope.TASK,
                metric_type=MetricType.GAUGE,
                value=task_metrics.confidence,
                unit="score",
                entity_id=task_metrics.task_id,
                entity_type="task",
                tags=base_tags,
                timestamp=task_metrics.completed_at
            )
        ]
        
        for metric in metrics:
            self._record_metric(metric)
            
    def record_orchestra_completion(self, orchestra_metrics: OrchestraMetrics):
        """Record orchestra-level metrics."""
        tags = {"template": orchestra_metrics.template}
        
        orchestra_level_metrics = [
            Metric(
                metric_id=f"orchestra_{orchestra_metrics.orchestra_id}_duration",
                name="orchestra_total_duration",
                scope=MetricScope.ORCHESTRA,
                metric_type=MetricType.DURATION,
                value=orchestra_metrics.total_duration_seconds,
                unit="seconds",
                entity_id=orchestra_metrics.orchestra_id,
                entity_type="orchestra",
                tags=tags,
                timestamp=orchestra_metrics.completed_at
            ),
            Metric(
                metric_id=f"orchestra_{orchestra_metrics.orchestra_id}_cost",
                name="orchestra_total_cost",
                scope=MetricScope.ORCHESTRA,
                metric_type=MetricType.COUNTER,
                value=float(orchestra_metrics.total_cost_usd),
                unit="usd",
                entity_id=orchestra_metrics.orchestra_id,
                entity_type="orchestra",
                tags=tags,
                timestamp=orchestra_metrics.completed_at
            ),
            Metric(
                metric_id=f"orchestra_{orchestra_metrics.orchestra_id}_success_rate",
                name="orchestra_success_rate",
                scope=MetricScope.ORCHESTRA,
                metric_type=MetricType.GAUGE,
                value=orchestra_metrics.success_rate,
                unit="percent",
                entity_id=orchestra_metrics.orchestra_id,
                entity_type="orchestra",
                tags=tags,
                timestamp=orchestra_metrics.completed_at
            )
        ]
        
        for metric in orchestra_level_metrics:
            self._record_metric(metric)
            
    def record_quality_gate_result(
        self, 
        task_id: str, 
        gate_id: str, 
        result: 'QualityGateResult'
    ):
        """Record quality gate execution metrics."""
        self._record_metric(Metric(
            metric_id=f"quality_gate_{gate_id}_{task_id}",
            name="quality_gate_score",
            scope=MetricScope.QUALITY_GATE,
            metric_type=MetricType.GAUGE,
            value=result.overall_score,
            unit="score",
            entity_id=gate_id,
            entity_type="quality_gate",
            tags={
                "task_id": task_id,
                "gate_result": result.result.value,
                "gate_name": gate_id
            }
        ))
        
    def record_system_metric(self, name: str, value: float, unit: str, tags: Dict[str, str] = None):
        """Record system-level metric."""
        self._record_metric(Metric(
            metric_id=f"system_{name}_{int(datetime.now().timestamp())}",
            name=name,
            scope=MetricScope.SYSTEM,
            metric_type=MetricType.GAUGE,
            value=value,
            unit=unit,
            entity_id="system",
            entity_type="system",
            tags=tags or {}
        ))
        
    def _record_metric(self, metric: Metric):
        """Record a single metric (with buffering)."""
        self.buffer.append(metric)
        
        if len(self.buffer) >= self.buffer_size:
            self._flush_buffer()
            
    def _flush_buffer(self):
        """Flush buffered metrics to database."""
        if not self.buffer:
            return
            
        # Batch insert metrics
        metrics_data = [
            (
                metric.metric_id, metric.name, metric.scope.value,
                metric.metric_type.value, metric.value, metric.unit,
                metric.entity_id, metric.entity_type,
                json.dumps(metric.tags), json.dumps(metric.metadata),
                metric.timestamp
            )
            for metric in self.buffer
        ]
        
        self.db.executemany("""
            INSERT OR REPLACE INTO metrics (
                metric_id, name, scope, metric_type, value, unit,
                entity_id, entity_type, tags, metadata, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, metrics_data)
        
        self.buffer.clear()
        
    def flush(self):
        """Force flush of all buffered metrics."""
        self._flush_buffer()
```

### Analytics Engine

```python
class MetricsAnalyzer:
    """Analytics engine for metrics data."""
    
    def __init__(self, db_connection, metrics_collector: MetricsCollector):
        self.db = db_connection
        self.collector = metrics_collector
        
    def get_task_performance_summary(
        self, 
        task_type: Optional[str] = None,
        time_range_hours: int = 24
    ) -> Dict[str, Any]:
        """Get task performance summary."""
        where_clause = "WHERE timestamp > datetime('now', '-{} hours')".format(time_range_hours)
        params = []
        
        if task_type:
            where_clause += " AND JSON_EXTRACT(tags, '$.task_type') = ?"
            params.append(task_type)
            
        # Get task metrics
        task_stats = self.db.execute(f"""
            SELECT 
                COUNT(DISTINCT entity_id) as total_tasks,
                COUNT(DISTINCT CASE WHEN JSON_EXTRACT(tags, '$.status') = 'success' 
                                   THEN entity_id END) as successful_tasks,
                AVG(CASE WHEN name = 'task_execution_duration' THEN value END) as avg_duration,
                AVG(CASE WHEN name = 'task_tokens_consumed' THEN value END) as avg_tokens,
                AVG(CASE WHEN name = 'task_cost' THEN value END) as avg_cost,
                AVG(CASE WHEN name = 'task_confidence' THEN value END) as avg_confidence,
                SUM(CASE WHEN name = 'task_cost' THEN value ELSE 0 END) as total_cost
            FROM metrics 
            {where_clause}
            AND scope = 'task'
        """, params).fetchone()
        
        if not task_stats or task_stats['total_tasks'] == 0:
            return {"error": "No tasks found in specified time range"}
            
        success_rate = (task_stats['successful_tasks'] or 0) / task_stats['total_tasks']
        
        return {
            "time_range_hours": time_range_hours,
            "task_type": task_type,
            "total_tasks": task_stats['total_tasks'],
            "successful_tasks": task_stats['successful_tasks'],
            "success_rate": success_rate,
            "avg_duration_seconds": task_stats['avg_duration'],
            "avg_tokens": int(task_stats['avg_tokens'] or 0),
            "avg_cost_usd": round(float(task_stats['avg_cost'] or 0), 4),
            "avg_confidence": round(task_stats['avg_confidence'] or 0, 3),
            "total_cost_usd": round(float(task_stats['total_cost'] or 0), 2),
            "tokens_per_second": self._calculate_tokens_per_second(task_stats),
            "cost_per_token": self._calculate_cost_per_token(task_stats)
        }
        
    def get_model_comparison(self, time_range_hours: int = 24) -> List[Dict[str, Any]]:
        """Compare performance across different models."""
        model_stats = self.db.execute("""
            SELECT 
                JSON_EXTRACT(tags, '$.model') as model,
                COUNT(DISTINCT entity_id) as total_tasks,
                COUNT(DISTINCT CASE WHEN JSON_EXTRACT(tags, '$.status') = 'success' 
                                   THEN entity_id END) as successful_tasks,
                AVG(CASE WHEN name = 'task_execution_duration' THEN value END) as avg_duration,
                AVG(CASE WHEN name = 'task_tokens_consumed' THEN value END) as avg_tokens,
                AVG(CASE WHEN name = 'task_cost' THEN value END) as avg_cost,
                AVG(CASE WHEN name = 'task_confidence' THEN value END) as avg_confidence,
                SUM(CASE WHEN name = 'task_cost' THEN value ELSE 0 END) as total_cost
            FROM metrics 
            WHERE timestamp > datetime('now', '-{} hours')
            AND scope = 'task'
            AND JSON_EXTRACT(tags, '$.model') IS NOT NULL
            GROUP BY JSON_EXTRACT(tags, '$.model')
            ORDER BY total_tasks DESC
        """.format(time_range_hours)).fetchall()
        
        model_comparison = []
        for row in model_stats:
            if row['total_tasks'] == 0:
                continue
                
            success_rate = (row['successful_tasks'] or 0) / row['total_tasks']
            
            model_comparison.append({
                "model": row['model'],
                "total_tasks": row['total_tasks'],
                "success_rate": success_rate,
                "avg_duration_seconds": round(row['avg_duration'] or 0, 2),
                "avg_tokens": int(row['avg_tokens'] or 0),
                "avg_cost_usd": round(float(row['avg_cost'] or 0), 4),
                "avg_confidence": round(row['avg_confidence'] or 0, 3),
                "total_cost_usd": round(float(row['total_cost'] or 0), 2),
                "cost_efficiency": self._calculate_cost_efficiency(row),
                "speed_rank": 0  # Will be calculated after sorting
            })
            
        # Add speed rankings
        model_comparison.sort(key=lambda x: x['avg_duration_seconds'])
        for i, model in enumerate(model_comparison):
            model['speed_rank'] = i + 1
            
        return model_comparison
        
    def get_orchestra_analytics(self, template: Optional[str] = None) -> Dict[str, Any]:
        """Get orchestra-level analytics."""
        where_clause = "WHERE scope = 'orchestra'"
        params = []
        
        if template:
            where_clause += " AND JSON_EXTRACT(tags, '$.template') = ?"
            params.append(template)
            
        orchestra_stats = self.db.execute(f"""
            SELECT 
                COUNT(DISTINCT entity_id) as total_orchestras,
                AVG(CASE WHEN name = 'orchestra_total_duration' THEN value END) as avg_duration,
                AVG(CASE WHEN name = 'orchestra_total_cost' THEN value END) as avg_cost,
                AVG(CASE WHEN name = 'orchestra_success_rate' THEN value END) as avg_success_rate,
                SUM(CASE WHEN name = 'orchestra_total_cost' THEN value ELSE 0 END) as total_cost
            FROM metrics 
            {where_clause}
        """, params).fetchone()
        
        return {
            "template": template,
            "total_orchestras": orchestra_stats['total_orchestras'] or 0,
            "avg_duration_seconds": round(orchestra_stats['avg_duration'] or 0, 2),
            "avg_cost_usd": round(float(orchestra_stats['avg_cost'] or 0), 2),
            "avg_success_rate": round(orchestra_stats['avg_success_rate'] or 0, 3),
            "total_cost_usd": round(float(orchestra_stats['total_cost'] or 0), 2)
        }
        
    def detect_anomalies(self, metric_name: str, lookback_hours: int = 24) -> List[Dict[str, Any]]:
        """Detect anomalies in metric values."""
        # Get recent data
        recent_data = self.db.execute("""
            SELECT value, timestamp, entity_id, tags
            FROM metrics 
            WHERE name = ? 
            AND timestamp > datetime('now', '-{} hours')
            ORDER BY timestamp DESC
        """.format(lookback_hours), (metric_name,)).fetchall()
        
        if len(recent_data) < 10:
            return []  # Need more data for anomaly detection
            
        values = [row['value'] for row in recent_data]
        
        # Simple statistical anomaly detection
        mean_value = np.mean(values)
        std_value = np.std(values)
        threshold = 2.5 * std_value  # 2.5 sigma threshold
        
        anomalies = []
        for row in recent_data:
            if abs(row['value'] - mean_value) > threshold:
                anomalies.append({
                    'metric_name': metric_name,
                    'value': row['value'],
                    'expected_range': [mean_value - threshold, mean_value + threshold],
                    'deviation_sigma': abs(row['value'] - mean_value) / std_value,
                    'timestamp': row['timestamp'],
                    'entity_id': row['entity_id'],
                    'tags': json.loads(row['tags'] or '{}')
                })
                
        return anomalies
        
    def generate_cost_forecast(self, days_ahead: int = 7) -> Dict[str, Any]:
        """Generate cost forecast based on recent trends."""
        # Get cost data for the last 7 days
        daily_costs = self.db.execute("""
            SELECT 
                DATE(timestamp) as date,
                SUM(CASE WHEN name = 'task_cost' THEN value ELSE 0 END) as daily_cost
            FROM metrics 
            WHERE timestamp > datetime('now', '-7 days')
            AND scope = 'task'
            GROUP BY DATE(timestamp)
            ORDER BY date
        """).fetchall()
        
        if len(daily_costs) < 3:
            return {"error": "Insufficient cost data for forecasting"}
            
        costs = [row['daily_cost'] for row in daily_costs]
        
        # Simple linear trend forecast
        x = np.arange(len(costs))
        coefficients = np.polyfit(x, costs, 1)
        trend_slope = coefficients[0]
        
        # Forecast next days
        last_cost = costs[-1]
        forecasted_costs = []
        
        for i in range(1, days_ahead + 1):
            forecasted_cost = last_cost + (trend_slope * i)
            forecasted_costs.append(max(0, forecasted_cost))  # Costs can't be negative
            
        return {
            "days_ahead": days_ahead,
            "historical_daily_costs": costs,
            "trend_slope_per_day": trend_slope,
            "forecasted_daily_costs": forecasted_costs,
            "forecasted_total": sum(forecasted_costs),
            "current_daily_average": np.mean(costs),
            "trend": "increasing" if trend_slope > 0 else "decreasing" if trend_slope < 0 else "stable"
        }
```

## Metrics Storage Schema

```sql
-- Core metrics table
CREATE TABLE metrics (
    metric_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    scope TEXT NOT NULL,           -- 'task', 'orchestra', 'model', 'system', 'quality_gate'
    metric_type TEXT NOT NULL,     -- 'counter', 'gauge', 'histogram', 'duration', 'rate'
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    
    entity_id TEXT NOT NULL,       -- Task ID, orchestra ID, model name, etc.
    entity_type TEXT NOT NULL,
    
    tags TEXT DEFAULT '{}',        -- JSON object with key-value tags
    metadata TEXT DEFAULT '{}',    -- JSON object with additional data
    
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    INDEX idx_metrics_name_time ON (name, timestamp DESC),
    INDEX idx_metrics_scope_entity ON (scope, entity_id),
    INDEX idx_metrics_timestamp ON (timestamp DESC)
);

-- Pre-aggregated daily summaries for performance
CREATE TABLE daily_metric_summaries (
    date DATE NOT NULL,
    metric_name TEXT NOT NULL,
    scope TEXT NOT NULL,
    
    count INTEGER DEFAULT 0,
    sum_value REAL DEFAULT 0.0,
    avg_value REAL DEFAULT 0.0,
    min_value REAL DEFAULT 0.0,
    max_value REAL DEFAULT 0.0,
    std_dev REAL DEFAULT 0.0,
    
    PRIMARY KEY (date, metric_name, scope)
);

-- Model performance summaries
CREATE TABLE model_performance_summaries (
    model_name TEXT NOT NULL,
    date DATE NOT NULL,
    
    total_tasks INTEGER DEFAULT 0,
    successful_tasks INTEGER DEFAULT 0,
    failed_tasks INTEGER DEFAULT 0,
    
    avg_duration_seconds REAL DEFAULT 0.0,
    avg_tokens REAL DEFAULT 0.0,
    avg_cost_usd DECIMAL(10,4) DEFAULT 0.0,
    avg_confidence REAL DEFAULT 0.0,
    
    total_cost_usd DECIMAL(10,2) DEFAULT 0.0,
    
    PRIMARY KEY (model_name, date)
);

-- Cost tracking with detailed breakdown
CREATE TABLE cost_tracking (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATE NOT NULL,
    entity_type TEXT NOT NULL,     -- 'task', 'orchestra', 'model'
    entity_id TEXT NOT NULL,
    
    cost_usd DECIMAL(10,4) NOT NULL,
    tokens_consumed INTEGER DEFAULT 0,
    
    cost_category TEXT,            -- 'input_tokens', 'output_tokens', 'processing'
    model_used TEXT,
    task_type TEXT,
    
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## Reporting and Dashboards

### CLI Metrics Commands

```bash
# Basic metrics commands
orch metrics show                           # Overall system metrics
orch metrics tasks --last 24h              # Task metrics for last 24 hours
orch metrics models --compare              # Model performance comparison
orch metrics orchestras --template content # Orchestra metrics by template
orch metrics costs --forecast 7d           # Cost forecast for next 7 days

# Detailed analysis
orch metrics analyze --metric task_duration --anomalies  # Anomaly detection
orch metrics trends --metric success_rate --period 30d   # Trend analysis
orch metrics efficiency --top-models 5                   # Top performing models

# Export and reporting
orch metrics export --format json --output metrics.json
orch metrics report --template monthly --output report.md
```

### Markdown Report Generator

```python
class MetricsReporter:
    """Generate formatted reports from metrics data."""
    
    def __init__(self, analyzer: MetricsAnalyzer):
        self.analyzer = analyzer
        
    def generate_daily_report(self, date: Optional[str] = None) -> str:
        """Generate daily metrics report in Markdown."""
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')
            
        # Get metrics for the day
        task_summary = self.analyzer.get_task_performance_summary(time_range_hours=24)
        model_comparison = self.analyzer.get_model_comparison(time_range_hours=24)
        orchestra_analytics = self.analyzer.get_orchestra_analytics()
        
        report = f"""# Orchestration Engine Metrics Report
## Date: {date}

### Task Performance Summary
- **Total Tasks**: {task_summary.get('total_tasks', 0):,}
- **Success Rate**: {task_summary.get('success_rate', 0):.2%}
- **Average Duration**: {task_summary.get('avg_duration_seconds', 0):.1f}s
- **Average Confidence**: {task_summary.get('avg_confidence', 0):.3f}
- **Total Cost**: ${task_summary.get('total_cost_usd', 0):.2f}
- **Average Cost per Task**: ${task_summary.get('avg_cost_usd', 0):.4f}

### Model Performance Comparison

| Model | Tasks | Success Rate | Avg Duration | Avg Tokens | Avg Cost | Confidence |
|-------|-------|--------------|--------------|------------|----------|------------|
"""

        for model in model_comparison:
            report += f"| {model['model']} | {model['total_tasks']} | {model['success_rate']:.2%} | {model['avg_duration_seconds']:.1f}s | {model['avg_tokens']} | ${model['avg_cost_usd']:.4f} | {model['avg_confidence']:.3f} |\n"
            
        report += f"""
### Orchestra Analytics
- **Total Orchestras**: {orchestra_analytics.get('total_orchestras', 0)}
- **Average Duration**: {orchestra_analytics.get('avg_duration_seconds', 0):.1f}s
- **Average Success Rate**: {orchestra_analytics.get('avg_success_rate', 0):.2%}
- **Total Cost**: ${orchestra_analytics.get('total_cost_usd', 0):.2f}

### Recommendations
"""

        # Add recommendations based on metrics
        recommendations = self._generate_recommendations(task_summary, model_comparison)
        for rec in recommendations:
            report += f"- {rec}\n"
            
        return report
        
    def _generate_recommendations(self, task_summary: Dict, model_comparison: List[Dict]) -> List[str]:
        """Generate actionable recommendations based on metrics."""
        recommendations = []
        
        # Success rate recommendations
        if task_summary.get('success_rate', 0) < 0.8:
            recommendations.append("⚠️ Success rate below 80% - review failed tasks and improve error handling")
            
        # Cost efficiency recommendations
        if model_comparison:
            cheapest_model = min(model_comparison, key=lambda x: x['avg_cost_usd'])
            most_expensive = max(model_comparison, key=lambda x: x['avg_cost_usd'])
            
            if most_expensive['avg_cost_usd'] > cheapest_model['avg_cost_usd'] * 3:
                recommendations.append(f"💰 Consider using {cheapest_model['model']} instead of {most_expensive['model']} for cost savings")
                
        # Performance recommendations
        avg_duration = task_summary.get('avg_duration_seconds', 0)
        if avg_duration > 120:  # 2 minutes
            recommendations.append("⚡ Average task duration is high - consider optimizing prompts or using faster models")
            
        return recommendations
```

## Alerting and Monitoring

### Alert Manager

```python
class AlertManager:
    """Manages alerts based on metric thresholds."""
    
    def __init__(self, analyzer: MetricsAnalyzer, notification_client):
        self.analyzer = analyzer
        self.notification_client = notification_client
        self.alert_rules = []
        
    def add_alert_rule(self, rule: 'AlertRule'):
        """Add an alert rule."""
        self.alert_rules.append(rule)
        
    def check_all_alerts(self):
        """Check all alert rules and trigger notifications."""
        for rule in self.alert_rules:
            try:
                if rule.should_alert(self.analyzer):
                    self._trigger_alert(rule)
            except Exception as e:
                print(f"Error checking alert rule {rule.name}: {e}")
                
    def _trigger_alert(self, rule: 'AlertRule'):
        """Trigger an alert notification."""
        message = rule.generate_alert_message()
        self.notification_client.send_alert(
            title=f"🚨 Orchestration Alert: {rule.name}",
            message=message,
            severity=rule.severity
        )

class AlertRule:
    """Individual alert rule definition."""
    
    def __init__(
        self, 
        name: str, 
        description: str,
        check_function: Callable,
        threshold: float,
        severity: str = "warning"
    ):
        self.name = name
        self.description = description
        self.check_function = check_function
        self.threshold = threshold
        self.severity = severity
        
    def should_alert(self, analyzer: MetricsAnalyzer) -> bool:
        """Check if alert conditions are met."""
        current_value = self.check_function(analyzer)
        return current_value >= self.threshold
        
    def generate_alert_message(self) -> str:
        """Generate alert message."""
        return f"{self.description}\nThreshold: {self.threshold}\nSeverity: {self.severity}"

# Example alert rules
def setup_default_alerts(alert_manager: AlertManager):
    """Set up default alert rules."""
    
    # Cost spike alert
    def check_hourly_cost(analyzer):
        summary = analyzer.get_task_performance_summary(time_range_hours=1)
        return summary.get('total_cost_usd', 0)
        
    alert_manager.add_alert_rule(AlertRule(
        name="High Hourly Cost",
        description="Hourly cost exceeds budget threshold",
        check_function=check_hourly_cost,
        threshold=10.0,  # $10/hour
        severity="critical"
    ))
    
    # Failure rate alert
    def check_failure_rate(analyzer):
        summary = analyzer.get_task_performance_summary(time_range_hours=1)
        return 1.0 - summary.get('success_rate', 1.0)  # Return failure rate
        
    alert_manager.add_alert_rule(AlertRule(
        name="High Failure Rate",
        description="Task failure rate is too high",
        check_function=check_failure_rate,
        threshold=0.2,  # 20% failure rate
        severity="warning"
    ))
    
    # Queue depth alert  
    def check_queue_depth(analyzer):
        # This would check current queue depth
        return 0  # Placeholder
        
    alert_manager.add_alert_rule(AlertRule(
        name="Queue Backlog",
        description="Task queue is backing up",
        check_function=check_queue_depth,
        threshold=50,  # 50+ queued tasks
        severity="warning"
    ))
```

The metrics system provides **comprehensive observability** into orchestration engine performance, enabling data-driven optimization, proactive monitoring, and cost management across all aspects of multi-agent workflows.