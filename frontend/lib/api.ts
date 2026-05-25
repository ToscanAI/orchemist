/**
 * Typed HTTP client for the Orchestration Engine REST API (`/api/v1/*`).
 *
 * Usage:
 * ```ts
 * import { listTemplates, startRun, ApiError } from '@/lib/api';
 *
 * const templates = await listTemplates();
 * ```
 *
 * The `BASE_URL` defaults to an empty string (same-origin), so Next.js
 * API routes can proxy requests without CORS. Override via the
 * `NEXT_PUBLIC_API_BASE_URL` environment variable when the backend runs
 * on a different origin.
 *
 * @module
 */

import type {
  TemplateSummary,
  TemplateDetail,
  TemplateWriteRequest,
  TemplateWriteResponse,
  TemplateValidateRequest,
  TemplateValidateResponse,
  TemplateDeleteResponse,
  CreateTemplateRequest,
  UpdateTemplateRequest,
  StartRunRequest,
  RunRecord,
  RunsListResponse,
  ListRunsParams,
  CancelRunResponse,
  HealthResponse,
  SseEvent,
} from './types';

// ── Base URL ──────────────────────────────────────────────────────────────────

/** Base URL for all API requests. Defaults to same-origin. */
const BASE_URL: string =
  (typeof process !== 'undefined' &&
    process.env['NEXT_PUBLIC_API_BASE_URL']) ||
  '';

// ── ApiError ──────────────────────────────────────────────────────────────────

/**
 * Thrown by `_fetch` whenever the server returns a non-2xx HTTP status.
 *
 * Callers can inspect `.status` for the HTTP status code and `.detail`
 * for the structured error body (if any).
 */
export class ApiError extends Error {
  /** HTTP status code (e.g. 404, 422). */
  readonly status: number;
  /** Raw error detail from the backend (may be a string or object). */
  readonly detail: unknown;

  constructor(status: number, detail: unknown, message?: string) {
    super(message ?? `API error ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

// ── Internal fetch wrapper ────────────────────────────────────────────────────

/**
 * Typed fetch wrapper.
 *
 * Handles:
 * - URL construction (joining `BASE_URL` with `path`)
 * - JSON serialisation of the request body
 * - Throwing `ApiError` on non-2xx responses
 * - Returning the parsed JSON body as `T`
 *
 * @param path  Absolute path relative to `BASE_URL` (e.g. `/api/v1/templates`).
 * @param init  Standard `RequestInit` options. `Content-Type` is set to
 *              `application/json` automatically when `body` is provided.
 * @returns     The parsed JSON response body cast to `T`.
 * @throws      `ApiError` on HTTP errors.
 * @throws      `TypeError` on network failure (no internet, CORS, etc.).
 */
async function _fetch<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${BASE_URL}${path}`;

  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...(init?.headers as Record<string, string> | undefined),
  };

  if (init?.body !== undefined && init?.body !== null) {
    headers['Content-Type'] = 'application/json';
  }

  const response = await fetch(url, { ...init, headers });

  if (!response.ok) {
    let detail: unknown;
    try {
      detail = await response.json();
    } catch {
      detail = await response.text().catch(() => undefined);
    }
    throw new ApiError(
      response.status,
      detail,
      `API error ${response.status}: ${response.statusText}`,
    );
  }

  // 204 No Content — return empty object
  if (response.status === 204) {
    return {} as T;
  }

  return response.json() as Promise<T>;
}

// ── Health ────────────────────────────────────────────────────────────────────

/**
 * Check API server health.
 *
 * `GET /api/v1/health`
 */
export function getHealth(): Promise<HealthResponse> {
  return _fetch<HealthResponse>('/api/v1/health');
}

// ── Templates ─────────────────────────────────────────────────────────────────

/**
 * List all discoverable pipeline templates.
 *
 * `GET /api/v1/templates`
 *
 * @returns Array of template summaries.
 */
export function listTemplates(): Promise<TemplateSummary[]> {
  return _fetch<TemplateSummary[]>('/api/v1/templates');
}

/**
 * Get full detail for a single template by name or ID.
 *
 * `GET /api/v1/templates/{name}`
 *
 * Path parameter is URL-encoded to prevent path traversal.
 *
 * @param name  Template name (file stem) or template ID.
 * @throws      `ApiError` with status 404 when not found.
 */
export function getTemplate(name: string): Promise<TemplateDetail> {
  return _fetch<TemplateDetail>(
    `/api/v1/templates/${encodeURIComponent(name)}`,
  );
}

/**
 * Validate a template body without writing it to disk.
 *
 * `POST /api/v1/templates/validate`
 *
 * @param req  `{ content, extended? }` — raw YAML content and optional flag
 *             to enable extended linting warnings.
 */
export function validateTemplate(
  req: TemplateValidateRequest,
): Promise<TemplateValidateResponse> {
  return _fetch<TemplateValidateResponse>('/api/v1/templates/validate', {
    method: 'POST',
    body: JSON.stringify(req),
  });
}

/**
 * Create a new pipeline template.
 *
 * `POST /api/v1/templates`
 *
 * @param req  Template content and optional `source` / `overwrite` flags.
 * @throws     `ApiError` 409 when template already exists and `overwrite` is `false`.
 * @throws     `ApiError` 422 when content fails validation.
 */
export function createTemplate(
  req: TemplateWriteRequest,
): Promise<TemplateWriteResponse> {
  return _fetch<TemplateWriteResponse>('/api/v1/templates', {
    method: 'POST',
    body: JSON.stringify(req),
  });
}

/**
 * Update an existing user-owned template.
 *
 * `PUT /api/v1/templates/{name}`
 *
 * @param name  Template name (file stem) or template ID.
 * @param req   New template content (and optional source flag, which is
 *              ignored for PUT).
 * @throws      `ApiError` 403 when template is bundled or project-owned.
 * @throws      `ApiError` 404 when template is not found.
 */
export function updateTemplate(
  name: string,
  req: TemplateWriteRequest,
): Promise<TemplateWriteResponse> {
  return _fetch<TemplateWriteResponse>(
    `/api/v1/templates/${encodeURIComponent(name)}`,
    { method: 'PUT', body: JSON.stringify(req) },
  );
}

/**
 * Delete a user-owned pipeline template.
 *
 * `DELETE /api/v1/templates/{name}`
 *
 * @param name  Template name (file stem) or template ID.
 * @throws      `ApiError` 403 when template is bundled or project-owned.
 * @throws      `ApiError` 404 when template is not found.
 */
export function deleteTemplate(name: string): Promise<TemplateDeleteResponse> {
  return _fetch<TemplateDeleteResponse>(
    `/api/v1/templates/${encodeURIComponent(name)}`,
    { method: 'DELETE' },
  );
}

/**
 * Duplicate an existing template.
 *
 * `POST /api/v1/templates/{name}/duplicate`
 *
 * Creates a copy of the template with a `-copy` suffix in the project
 * templates directory.
 *
 * @param name  Template name (file stem) or template ID.
 * @returns     Full template detail of the new copy.
 * @throws      `ApiError` 404 when source template is not found.
 */
export function duplicateTemplate(name: string): Promise<TemplateDetail> {
  return _fetch<TemplateDetail>(
    `/api/v1/templates/${encodeURIComponent(name)}/duplicate`,
    { method: 'POST' },
  );
}

// ── Pipeline Runs ─────────────────────────────────────────────────────────────

/**
 * Launch a new pipeline run in the background.
 *
 * `POST /api/v1/runs`
 *
 * Equivalent to `orch launch` — returns immediately with the new run record.
 * Poll `getRun(run_id)` to track progress, or use `streamRun(run_id)` for
 * live SSE events.
 *
 * @param req  `{ template, mode, input, ... }`
 * @returns    Newly created `RunRecord`.
 */
export function startRun(req: StartRunRequest): Promise<RunRecord> {
  return _fetch<RunRecord>('/api/v1/runs', {
    method: 'POST',
    body: JSON.stringify(req),
  });
}

/**
 * List pipeline runs with optional filtering and pagination.
 *
 * `GET /api/v1/runs`
 *
 * @param params  Optional `{ status, template_id, limit, offset }`.
 * @returns       Paginated `{ items, total, limit, offset }`.
 */
export function listRuns(params?: ListRunsParams): Promise<RunsListResponse> {
  const qs = new URLSearchParams();
  if (params?.status !== undefined) qs.set('status', params.status);
  if (params?.template_id !== undefined)
    qs.set('template_id', params.template_id);
  if (params?.limit !== undefined) qs.set('limit', String(params.limit));
  if (params?.offset !== undefined) qs.set('offset', String(params.offset));

  const query = qs.toString();
  return _fetch<RunsListResponse>(`/api/v1/runs${query ? `?${query}` : ''}`);
}

/**
 * Return the current state of a pipeline run.
 *
 * `GET /api/v1/runs/{run_id}`
 *
 * @param runId  Pipeline run ID (8-char UUID prefix).
 * @throws       `ApiError` 404 when run is not found.
 */
export function getRun(runId: string): Promise<RunRecord> {
  return _fetch<RunRecord>(`/api/v1/runs/${encodeURIComponent(runId)}`);
}

/**
 * Return the daemon log file contents for a pipeline run.
 *
 * `GET /api/v1/runs/{run_id}/logs`
 *
 * @param runId  Pipeline run ID.
 * @throws       `ApiError` 404 when run or log file is not found.
 */
export function getRunLogs(runId: string): Promise<{ run_id: string; log: string }> {
  return _fetch<{ run_id: string; log: string }>(
    `/api/v1/runs/${encodeURIComponent(runId)}/logs`,
  );
}

// ── Run artifact endpoints (PR #825) ─────────────────────────────────────────

/** One file in a run's output_dir. */
export interface RunArtifactListEntry {
  readonly name: string;
  readonly size_bytes: number;
  readonly mtime: number;
}

/** Response from `GET /api/v1/runs/{id}/artifacts`. */
export interface RunArtifactList {
  readonly run_id: string;
  readonly output_dir: string;
  readonly files: readonly RunArtifactListEntry[];
}

/**
 * List files in a run's output_dir.
 *
 * `GET /api/v1/runs/{run_id}/artifacts`
 *
 * @throws `ApiError` 404 if the run or its output_dir is missing.
 */
export function listRunArtifacts(runId: string): Promise<RunArtifactList> {
  return _fetch<RunArtifactList>(
    `/api/v1/runs/${encodeURIComponent(runId)}/artifacts`,
  );
}

/** Response from `GET /api/v1/runs/{id}/artifacts/{filename}`. */
export interface RunArtifactContent {
  readonly run_id: string;
  readonly filename: string;
  readonly size_bytes: number;
  readonly content: string;
}

/**
 * Read a single artifact file from a run's output_dir.
 *
 * `GET /api/v1/runs/{run_id}/artifacts/{filename}`
 *
 * Body capped at 1 MiB server-side; oversize artifacts get a trailing
 * `[…truncated…]` marker appended.
 *
 * @throws `ApiError` 400 on path-traversal attempt; 404 if missing.
 */
export function getRunArtifact(
  runId: string,
  filename: string,
): Promise<RunArtifactContent> {
  return _fetch<RunArtifactContent>(
    `/api/v1/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(filename)}`,
  );
}

/** Per-section parsed Phase 0 inventory. */
export interface RunPhase0Section {
  readonly count: number;
  readonly entries: readonly string[];
}

/** Response from `GET /api/v1/runs/{id}/phase0`. */
export interface RunPhase0 {
  readonly run_id: string;
  readonly filename: string;
  readonly sections: {
    readonly ui_primitives: RunPhase0Section;
    readonly shared_libs: RunPhase0Section;
    readonly adjacent_patterns: RunPhase0Section;
    readonly workspace_barrels: RunPhase0Section;
  };
  readonly verdicts: {
    readonly CONSUME: number;
    readonly EXTEND: number;
    readonly DIVERGENT: number;
    readonly NEW_OK: number;
    readonly BLOCKED: number;
  };
  readonly raw: string;
}

/**
 * Parse the Phase 0 existing-symbols inventory for a run.
 *
 * `GET /api/v1/runs/{run_id}/phase0`
 *
 * @throws `ApiError` 404 when the run did not produce a Phase 0 artifact
 *         (e.g. `coding-pipeline-skip-spec` which has no Phase 0).
 */
export function getRunPhase0(runId: string): Promise<RunPhase0> {
  return _fetch<RunPhase0>(`/api/v1/runs/${encodeURIComponent(runId)}/phase0`);
}

/** One round in the cross-model dialogue artifact. */
export interface RunDialogueRound {
  readonly index: number;
  readonly side: 'drafter' | 'reviewer' | '';
  readonly model: string | null;
  readonly verdict: 'approve' | 'request_changes' | 'revise' | 'abort' | null;
  readonly content: string;
  readonly jaccard: number | null;
}

/** Response from `GET /api/v1/runs/{id}/dialogue`. */
export interface RunDialogue {
  readonly run_id: string;
  readonly filename: string;
  readonly rounds: readonly RunDialogueRound[];
  readonly raw: string;
}

/**
 * Return the cross-model dialogue artifact for a run, if present.
 *
 * `GET /api/v1/runs/{run_id}/dialogue`
 *
 * Only runs that used the Track B dialogue phase (PR #808) produce this
 * artifact — most runs return 404.
 *
 * @throws `ApiError` 404 when no dialogue artifact exists.
 */
export function getRunDialogue(runId: string): Promise<RunDialogue> {
  return _fetch<RunDialogue>(
    `/api/v1/runs/${encodeURIComponent(runId)}/dialogue`,
  );
}

/**
 * Cancel a running or pending pipeline run.
 *
 * `DELETE /api/v1/runs/{run_id}`
 *
 * @param runId  Pipeline run ID.
 * @throws       `ApiError` 404 when run is not found.
 * @throws       `ApiError` 409 when run is already in a terminal state.
 */
export function cancelRun(runId: string): Promise<CancelRunResponse> {
  return _fetch<CancelRunResponse>(
    `/api/v1/runs/${encodeURIComponent(runId)}`,
    { method: 'DELETE' },
  );
}

/**
 * Resume a paused pipeline run.
 *
 * `POST /api/v1/runs/{run_id}/resume`
 *
 * @param runId  Pipeline run ID.
 * @returns      Updated `RunRecord` with new status.
 * @throws       `ApiError` 404 when run is not found.
 * @throws       `ApiError` 409 when run is not in a paused state.
 */
export function resumeRun(runId: string): Promise<RunRecord> {
  return _fetch<RunRecord>(
    `/api/v1/runs/${encodeURIComponent(runId)}/resume`,
    { method: 'POST' },
  );
}

// ── Gate management (#743) ──────────────────────────────────────────────────

/**
 * Canonical backend gate-status values written by `daemon.py` and
 * `routing.py`. Anywhere a status filter is sent to the engine, it MUST be
 * one of these strings — the harness's user-facing filter labels
 * (pending / auto-merged / held / all) are mapped to one of these before
 * the API call.
 */
export type GateStatus = 'awaiting_approval' | 'approved' | 'merged' | 'rejected';

/**
 * Gate record from `/api/v1/gates`. Mirrors `_gate_to_dict()` on the
 * engine side (audited 2026-05-25). Optional fields are those the engine
 * omits on `null` values; types match the JSON shape exactly.
 */
export interface GateRecord {
  readonly run_id: string;
  readonly pipeline_id: string;
  readonly status: GateStatus | string;
  readonly branch: string;
  readonly base_branch: string;
  readonly diff_stats: string;
  readonly commits: readonly string[];
  readonly output_dir: string;
  readonly repo_path: string;
  readonly created_at: string;
  readonly approve_command: string;
  readonly reject_command: string;
  readonly create_pr: boolean;
  readonly issue_number: number | null;
  readonly scoring_status: string | null;
  readonly scoring_score: number | null;
  // Legacy fields the harness used to read; both retained as optional so
  // existing callers don't break. The engine may add these back later.
  readonly updated_at?: string;
  readonly message?: string | null;
}

/** Paginated gate list response. */
export interface GatesListResponse {
  readonly items: readonly GateRecord[];
  readonly total: number;
  readonly limit: number;
  readonly offset: number;
}

export interface ListGatesParams {
  status?: string;
  limit?: number;
  offset?: number;
}

/**
 * List all merge gates.
 *
 * `GET /api/v1/gates`
 */
export function listGates(params?: ListGatesParams): Promise<GatesListResponse> {
  const qs = new URLSearchParams();
  if (params?.status !== undefined) qs.set('status', params.status);
  if (params?.limit !== undefined) qs.set('limit', String(params.limit));
  if (params?.offset !== undefined) qs.set('offset', String(params.offset));
  const query = qs.toString();
  return _fetch<GatesListResponse>(`/api/v1/gates${query ? `?${query}` : ''}`);
}

/**
 * Get a single gate by run ID.
 *
 * `GET /api/v1/gates/{run_id}`
 */
export function getGate(runId: string): Promise<GateRecord> {
  return _fetch<GateRecord>(`/api/v1/gates/${encodeURIComponent(runId)}`);
}

/**
 * Approve a merge gate.
 *
 * `POST /api/v1/gates/{run_id}/approve`
 */
export function approveGate(
  runId: string,
  opts?: { message?: string; force?: boolean },
): Promise<GateRecord> {
  return _fetch<GateRecord>(
    `/api/v1/gates/${encodeURIComponent(runId)}/approve`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(opts ?? {}),
    },
  );
}

/**
 * Reject a merge gate.
 *
 * `POST /api/v1/gates/{run_id}/reject`
 */
export function rejectGate(
  runId: string,
  opts?: { reason?: string },
): Promise<GateRecord> {
  return _fetch<GateRecord>(
    `/api/v1/gates/${encodeURIComponent(runId)}/reject`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(opts ?? {}),
    },
  );
}

// ── SSE streaming ─────────────────────────────────────────────────────────────

/**
 * Callback invoked for each SSE event received from the stream.
 *
 * @param event  Typed and discriminated `SseEvent` object.
 */
export type SseEventCallback = (event: SseEvent) => void;

/**
 * Callback invoked when the SSE connection encounters an error or closes.
 *
 * @param error  The underlying `Event` from the `EventSource`.
 */
export type SseErrorCallback = (error: Event) => void;

/**
 * Stream live phase-transition events for a pipeline run via SSE.
 *
 * `GET /api/v1/runs/{run_id}/stream`
 *
 * Opens a browser `EventSource` and invokes `onEvent` for each SSE message.
 * The stream closes automatically when the run reaches a terminal state.
 *
 * @param runId    Pipeline run ID.
 * @param onEvent  Called for each typed SSE event.
 * @param onError  Optional callback for connection errors.
 * @returns        A cleanup function — call it to close the `EventSource`
 *                 before the run completes (e.g. on component unmount).
 *
 * @example
 * ```ts
 * const stop = streamRun('abc12345', (event) => {
 *   if (event.type === 'status_changed') console.log('Done:', event.status);
 * });
 * // Later:
 * stop();
 * ```
 */
export function streamRun(
  runId: string,
  onEvent: SseEventCallback,
  onError?: SseErrorCallback,
): () => void {
  const url = `${BASE_URL}/api/v1/runs/${encodeURIComponent(runId)}/stream`;
  const es = new EventSource(url);

  /**
   * Parse the raw SSE `event` name and `data` JSON into a typed `SseEvent`.
   * Returns `null` when the event name is unrecognised.
   */
  function parseEvent(eventType: string, data: string): SseEvent | null {
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(data) as Record<string, unknown>;
    } catch {
      return null;
    }
    switch (eventType) {
      case 'phase_started':
        return { ...parsed, type: 'phase_started' } as SsePhaseStartedEvent;
      case 'phase_completed':
        return { ...parsed, type: 'phase_completed' } as SsePhaseCompletedEvent;
      case 'status_changed':
        return { ...parsed, type: 'status_changed' } as SseStatusChangedEvent;
      case 'error':
        return { ...parsed, type: 'error' } as SseStreamErrorEvent;
      default:
        return null;
    }
  }

  /** Listeners for each named SSE event type. */
  const eventTypes = [
    'phase_started',
    'phase_completed',
    'status_changed',
    'error',
  ] as const;

  for (const eventType of eventTypes) {
    es.addEventListener(eventType, (e: MessageEvent) => {
      const typed = parseEvent(eventType, e.data as string);
      if (typed !== null) onEvent(typed);
    });
  }

  if (onError !== undefined) {
    es.onerror = onError;
  }

  /** Close the EventSource and remove all listeners. */
  return () => {
    es.close();
  };
}

// Local type aliases used inside streamRun for narrowing without re-importing
// (avoids the need for a direct import cycle check by TypeScript).
type SsePhaseStartedEvent = Extract<SseEvent, { type: 'phase_started' }>;
type SsePhaseCompletedEvent = Extract<SseEvent, { type: 'phase_completed' }>;
type SseStatusChangedEvent = Extract<SseEvent, { type: 'status_changed' }>;
type SseStreamErrorEvent = Extract<SseEvent, { type: 'error' }>;
