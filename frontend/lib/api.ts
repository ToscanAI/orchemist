/**
 * Typed API client for the Orchestration Engine REST API.
 *
 * All functions return plain JavaScript objects typed against the shapes in
 * `lib/types.ts`.  Errors are thrown as `ApiError` instances so callers can
 * distinguish HTTP errors from network failures.
 *
 * Base URL resolution:
 *   - In development (`next dev`), requests go to `/api/*` which are proxied
 *     by Next.js rewrites to `http://localhost:8374/api/*`.
 *   - In production (static export), the page is served by the same FastAPI
 *     origin, so relative `/api/*` URLs resolve correctly.
 */

import type {
  HealthResponse,
  PhaseOutputs,
  RunRequest,
  RunResponse,
  TemplateDetail,
  TemplateSummary,
} from "./types";

// ─── Error class ─────────────────────────────────────────────────────────────

export class ApiError extends Error {
  /** HTTP status code, or 0 for network errors */
  public readonly status: number;
  /** Raw response body (JSON string or plain text) */
  public readonly body: string;

  constructor(status: number, body: string, message?: string) {
    super(message ?? `API error ${status}: ${body}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

// ─── Internal helper ─────────────────────────────────────────────────────────

async function _fetch<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch (err) {
    throw new ApiError(0, String(err), `Network error: ${err}`);
  }

  const text = await response.text();
  if (!response.ok) {
    throw new ApiError(response.status, text);
  }

  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(response.status, text, "Failed to parse JSON response");
  }
}

// ─── Health ───────────────────────────────────────────────────────────────────

/**
 * GET /api/health
 * Returns server status and version string.
 */
export async function getHealth(): Promise<HealthResponse> {
  return _fetch<HealthResponse>("/api/health");
}

// ─── Templates ───────────────────────────────────────────────────────────────

/**
 * GET /api/templates
 * Returns a summary list of all discoverable pipeline templates.
 */
export async function listTemplates(): Promise<TemplateSummary[]> {
  return _fetch<TemplateSummary[]>("/api/templates");
}

/**
 * GET /api/templates/:id
 * Returns full detail for a single template including all phase definitions.
 */
export async function getTemplate(id: string): Promise<TemplateDetail> {
  return _fetch<TemplateDetail>(`/api/templates/${encodeURIComponent(id)}`);
}

// ─── Runs ─────────────────────────────────────────────────────────────────────

/**
 * POST /api/run
 * Starts a new pipeline run and returns a run_id for SSE polling.
 */
export async function startRun(request: RunRequest): Promise<RunResponse> {
  return _fetch<RunResponse>("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
}

/**
 * GET /api/run/:id/outputs
 * Returns all stored phase outputs for a run (completed or in progress).
 * Keys are phase IDs, values are output text strings.
 */
export async function getRunOutputs(runId: string): Promise<PhaseOutputs> {
  return _fetch<PhaseOutputs>(
    `/api/run/${encodeURIComponent(runId)}/outputs`
  );
}

/**
 * POST /api/run/:id/resume
 * Resumes a paused pipeline run without editing any output.
 */
export async function resumeRun(runId: string): Promise<{ ok: boolean; run_id: string }> {
  return _fetch(`/api/run/${encodeURIComponent(runId)}/resume`, {
    method: "POST",
  });
}

/**
 * POST /api/run/:id/edit
 * Edits a phase output and resumes the paused pipeline.
 */
export async function editAndResume(
  runId: string,
  phaseId: string,
  output: string
): Promise<{ ok: boolean; run_id: string; phase_id: string }> {
  return _fetch(`/api/run/${encodeURIComponent(runId)}/edit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ phase_id: phaseId, output }),
  });
}
