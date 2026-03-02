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
