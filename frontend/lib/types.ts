/**
 * TypeScript types mirroring the shapes returned by the Orchestration Engine
 * REST API (`/api/*`).
 *
 * Keep these in sync with the Pydantic models in `web/app.py`.
 */

// ─── Templates ───────────────────────────────────────────────────────────────

/** Summary entry returned by GET /api/templates */
export interface TemplateSummary {
  id: string;
  name: string;
  version: string;
  phases_count: number;
  description: string;
  source: string;
  category: string;
  author: string;
  phases: PhaseSummary[];
}

/** Phase summary included in TemplateSummary */
export interface PhaseSummary {
  id: string;
  name: string;
  model_tier: string;
}

/** Full template detail returned by GET /api/templates/:id */
export interface TemplateDetail {
  id: string;
  name: string;
  version: string;
  description: string;
  author: string;
  tags: string[];
  phases: PhaseDetail[];
  example_input: Record<string, unknown>;
  config_schema: Record<string, unknown>;
}

/** Full phase detail included in TemplateDetail */
export interface PhaseDetail {
  id: string;
  name: string;
  description: string;
  model_tier: string;
  thinking_level: string;
  depends_on: string[];
  task_type: string;
}

// ─── Run lifecycle ────────────────────────────────────────────────────────────

/** Execution mode for a pipeline run */
export type RunMode = "dry-run" | "standalone" | "openclaw";

/** Request body for POST /api/run */
export interface RunRequest {
  template: string;
  mode: RunMode;
  input: Record<string, unknown>;
  pause_after?: string[];
}

/** Response body from POST /api/run */
export interface RunResponse {
  run_id: string;
}

/** Current status of a run (local state built from SSE events) */
export type RunStatus =
  | "starting"
  | "running"
  | "paused"
  | "completed"
  | "aborted"
  | "error"
  | "cancelled";

// ─── SSE Events ───────────────────────────────────────────────────────────────

/** Base shape shared by all SSE event payloads */
interface SseEventBase {
  type: string;
}

export interface SseStartEvent extends SseEventBase {
  type: "start";
  run_id: string;
  template: string;
  mode: string;
}

export interface SsePhaseStartEvent extends SseEventBase {
  type: "phase_start";
  phase_id: string;
  phase_name: string;
  model_tier: string;
  wave: number;
}

export interface SsePhaseCompleteEvent extends SseEventBase {
  type: "phase_complete";
  phase_id: string;
  phase_name: string;
  status: string;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  elapsed_seconds: number;
  output_preview: string;
}

export interface SsePhaseErrorEvent extends SseEventBase {
  type: "phase_error";
  phase_id: string;
  phase_name: string;
  status: string;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  elapsed_seconds: number;
  error_message: string;
}

export interface SsePausedEvent extends SseEventBase {
  type: "paused";
  phase_id: string;
  message: string;
  output_preview: string;
}

export interface SseCompleteEvent extends SseEventBase {
  type: "complete";
  phases: number;
}

export interface SseAbortedEvent extends SseEventBase {
  type: "aborted";
  failed_phase: string;
}

export interface SseErrorEvent extends SseEventBase {
  type: "error";
  message: string;
}

export interface SsePipelineCompleteEvent extends SseEventBase {
  type: "pipeline_complete";
  status: RunStatus;
  total_phases: number;
  completed: number;
  failed: number;
  total_tokens: number;
  total_tokens_in: number;
  total_tokens_out: number;
  total_cost: number;
  total_elapsed: number;
}

/** Union of all known SSE event payloads */
export type SseEvent =
  | SseStartEvent
  | SsePhaseStartEvent
  | SsePhaseCompleteEvent
  | SsePhaseErrorEvent
  | SsePausedEvent
  | SseCompleteEvent
  | SseAbortedEvent
  | SseErrorEvent
  | SsePipelineCompleteEvent;

// ─── Phase output map ─────────────────────────────────────────────────────────

/** Response from GET /api/run/:id/outputs — phase_id → output text */
export type PhaseOutputs = Record<string, string>;

// ─── API health ───────────────────────────────────────────────────────────────

export interface HealthResponse {
  status: string;
  version: string;
}
