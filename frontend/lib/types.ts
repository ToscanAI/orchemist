/**
 * Shared TypeScript interfaces and type aliases that mirror the backend's
 * Pydantic models and SSE event shapes emitted by the FastAPI REST API
 * (`/api/v1/*`).
 *
 * All fields are `readonly` because this is incoming data that the client
 * never mutates directly. Optional fields (`?`) are used only where the
 * backend legitimately omits them (verified against `src/orchestration_engine/web/api.py`).
 *
 * @module
 */

// ── Template types ────────────────────────────────────────────────────────────

/** Summary row returned by `GET /api/v1/templates`. */
export interface TemplateSummary {
  readonly id: string;
  readonly name: string;
  readonly version: string;
  readonly description: string;
  readonly phases_count: number;
  readonly category: string;
  readonly author: string;
}

/** Phase detail embedded inside `TemplateDetail`. */
export interface PhaseDetail {
  readonly id: string;
  readonly name: string;
  readonly description: string;
  readonly model_tier: string;
  readonly thinking_level: string;
  readonly depends_on: readonly string[];
  readonly task_type: string;
}

/** Full template record returned by `GET /api/v1/templates/{name}`. */
export interface TemplateDetail extends TemplateSummary {
  readonly phases: readonly PhaseDetail[];
  readonly example_input: Record<string, unknown> | null;
  readonly config_schema: Record<string, unknown>;
  readonly tags: readonly string[];
}

// ── Run types ─────────────────────────────────────────────────────────────────

/** Allowed execution modes for a pipeline run. */
export type RunMode = 'dry-run' | 'standalone' | 'openclaw';

/** Body for `POST /api/v1/runs`. */
export interface StartRunRequest {
  readonly template: string;
  readonly mode: RunMode;
  readonly input: Record<string, unknown>;
  readonly output_dir?: string;
  readonly gateway_url?: string;
  readonly skip_scoring?: boolean;
}

/** Possible run statuses. */
export type RunStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'failed'
  | 'cancelled'
  | 'crashed'
  | 'scoring_failed';

/** Full pipeline run record returned by run endpoints. */
export interface RunRecord {
  readonly run_id: string;
  readonly template_id: string;
  readonly template_path: string;
  readonly mode: string;
  readonly status: RunStatus;
  readonly current_phase: string | null;
  readonly completed_phases: readonly string[];
  readonly pid: number | null;
  readonly output_dir: string;
  readonly error_message: string | null;
  readonly gateway_url: string | null;
  readonly skip_scoring: boolean;
  readonly scoring_status: string | null;
  readonly scoring_score: number | null;
  readonly started_at: string | null;
  readonly completed_at: string | null;
  readonly created_at: string | null;
}

/** Paginated response from `GET /api/v1/runs`. */
export interface RunsListResponse {
  readonly items: readonly RunRecord[];
  readonly total: number;
  readonly limit: number;
  readonly offset: number;
}

/** Query parameters for `GET /api/v1/runs`. */
export interface ListRunsParams {
  readonly status?: RunStatus;
  readonly template_id?: string;
  readonly limit?: number;
  readonly offset?: number;
}

// ── SSE event types (discriminated union on `type`) ───────────────────────────

/**
 * Emitted when the daemon begins executing a phase.
 *
 * Corresponds to the `phase_started` SSE event from `GET /api/v1/runs/{run_id}/stream`.
 */
export interface SsePhaseStartedEvent {
  readonly type: 'phase_started';
  readonly run_id: string;
  readonly phase_id: string | null;
  readonly tokens_consumed: number | null;
  readonly cost_usd: number | null;
  readonly state: string | null;
  readonly created_at: string | null;
}

/**
 * Emitted when a phase completes (success or failure).
 *
 * Corresponds to the `phase_completed` SSE event.
 */
export interface SsePhaseCompletedEvent {
  readonly type: 'phase_completed';
  readonly run_id: string;
  readonly phase_id: string | null;
  readonly tokens_consumed: number | null;
  readonly cost_usd: number | null;
  readonly state: string | null;
  readonly created_at: string | null;
}

/**
 * Emitted once when the run reaches a terminal state
 * (`success`, `failed`, `cancelled`, `crashed`, `scoring_failed`).
 *
 * Corresponds to the `status_changed` SSE event.
 */
export interface SseStatusChangedEvent {
  readonly type: 'status_changed';
  readonly run_id: string;
  readonly phase_id: null;
  readonly status: RunStatus;
  readonly completed_at: string | null;
  readonly error_message: string | null;
}

/**
 * Emitted when the run ID is not found or an unexpected error occurs.
 *
 * Corresponds to the `error` SSE event.
 */
export interface SseStreamErrorEvent {
  readonly type: 'error';
  readonly error: string;
}

/** Discriminated union of all SSE events from the stream endpoint. */
export type SseEvent =
  | SsePhaseStartedEvent
  | SsePhaseCompletedEvent
  | SseStatusChangedEvent
  | SseStreamErrorEvent;

// ── API error type ────────────────────────────────────────────────────────────

/**
 * Structured error detail returned by the backend on 4xx / 5xx responses.
 * The `detail` field may be a string or a structured object with nested fields.
 */
export interface ApiErrorBody {
  readonly detail: string | Record<string, unknown>;
}

// ── Template CRUD types ───────────────────────────────────────────────────────

/** Body for `POST /api/v1/templates` and `PUT /api/v1/templates/{name}`. */
export interface TemplateWriteRequest {
  readonly content: string;
  readonly source?: 'user' | 'project';
  readonly overwrite?: boolean;
}

/** Response from `POST /api/v1/templates` and `PUT /api/v1/templates/{name}`. */
export interface TemplateWriteResponse {
  readonly id: string;
  readonly name: string;
  readonly version: string;
  readonly path: string;
  readonly source: string;
  readonly phases_count: number;
  readonly created: boolean;
}

/** Body for `POST /api/v1/templates/validate`. */
export interface TemplateValidateRequest {
  readonly content: string;
  readonly extended?: boolean;
}

/** Response from `POST /api/v1/templates/validate`. */
export interface TemplateValidateResponse {
  readonly valid: boolean;
  readonly errors: readonly string[];
  readonly warnings: readonly string[];
}

/** Response from `DELETE /api/v1/templates/{name}`. */
export interface TemplateDeleteResponse {
  readonly deleted: boolean;
  readonly id: string;
  readonly path: string;
  readonly source: string;
}

/** Response from `DELETE /api/v1/runs/{run_id}`. */
export interface CancelRunResponse {
  readonly run_id: string;
  readonly cancelled: boolean;
}

/** Response from `GET /api/v1/health`. */
export interface HealthResponse {
  readonly status: string;
  readonly version: string;
}
