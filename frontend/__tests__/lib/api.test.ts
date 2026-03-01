/**
 * Smoke tests for the typed API client (lib/api.ts).
 *
 * These tests verify:
 *   - ApiError is thrown with the correct status/body on non-2xx responses
 *   - ApiError is thrown with status 0 on network failures
 *   - Successful responses are parsed and returned correctly
 *   - URL encoding is applied consistently (no path traversal)
 */

import { ApiError, getHealth, listTemplates, getTemplate, startRun, getRunOutputs, resumeRun } from "@/lib/api";

// ─── Helpers ─────────────────────────────────────────────────────────────────

function mockFetch(status: number, body: unknown): jest.Mock {
  const mock = jest.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(typeof body === "string" ? body : JSON.stringify(body)),
  });
  global.fetch = mock;
  return mock;
}

function mockFetchNetworkError(): jest.Mock {
  const mock = jest.fn().mockRejectedValue(new Error("Network failure"));
  global.fetch = mock;
  return mock;
}

afterEach(() => {
  jest.restoreAllMocks();
});

// ─── ApiError class ──────────────────────────────────────────────────────────

describe("ApiError", () => {
  it("stores status and body", () => {
    const err = new ApiError(404, "not found");
    expect(err.status).toBe(404);
    expect(err.body).toBe("not found");
    expect(err.name).toBe("ApiError");
    expect(err instanceof Error).toBe(true);
  });

  it("uses custom message when provided", () => {
    const err = new ApiError(500, "{}", "Custom message");
    expect(err.message).toBe("Custom message");
  });

  it("generates default message when no custom message given", () => {
    const err = new ApiError(422, "bad input");
    expect(err.message).toContain("422");
  });
});

// ─── GET /api/health ─────────────────────────────────────────────────────────

describe("getHealth", () => {
  it("returns parsed health response on success", async () => {
    mockFetch(200, { status: "ok", version: "1.2.3" });
    const result = await getHealth();
    expect(result.status).toBe("ok");
    expect(result.version).toBe("1.2.3");
  });

  it("throws ApiError on non-2xx response", async () => {
    mockFetch(503, "Service Unavailable");
    await expect(getHealth()).rejects.toThrow(ApiError);
    await expect(getHealth()).rejects.toMatchObject({ status: 503 });
  });

  it("throws ApiError with status 0 on network error", async () => {
    mockFetchNetworkError();
    await expect(getHealth()).rejects.toMatchObject({ status: 0 });
  });
});

// ─── GET /api/templates ───────────────────────────────────────────────────────

describe("listTemplates", () => {
  it("returns an array of template summaries", async () => {
    const payload = [
      { id: "t1", name: "Template 1", version: "1.0", phases_count: 3,
        description: "", source: "", category: "general", author: "", phases: [] },
    ];
    mockFetch(200, payload);
    const result = await listTemplates();
    expect(Array.isArray(result)).toBe(true);
    expect(result[0].id).toBe("t1");
  });

  it("throws ApiError on 500", async () => {
    mockFetch(500, "Internal Server Error");
    await expect(listTemplates()).rejects.toThrow(ApiError);
  });
});

// ─── GET /api/templates/:id ──────────────────────────────────────────────────

describe("getTemplate", () => {
  it("encodes the template id in the URL", async () => {
    const mock = mockFetch(200, { id: "tpl/slash", name: "T", version: "1",
      description: "", author: "", tags: [], phases: [], example_input: {}, config_schema: {} });
    await getTemplate("tpl/slash");
    // Verify the first arg (URL) contains the percent-encoded id.
    // The second arg (RequestInit) may be undefined for GET calls with no extra options.
    const [[calledUrl]] = mock.mock.calls;
    expect(calledUrl).toContain(encodeURIComponent("tpl/slash"));
  });

  it("throws ApiError on 404", async () => {
    mockFetch(404, "Not found");
    await expect(getTemplate("missing")).rejects.toMatchObject({ status: 404 });
  });
});

// ─── POST /api/run ────────────────────────────────────────────────────────────

describe("startRun", () => {
  it("posts JSON body and returns run_id", async () => {
    const mock = mockFetch(200, { run_id: "abc-123" });
    const result = await startRun({ template: "coding-pipeline", mode: "dry-run", input: {} });
    expect(result.run_id).toBe("abc-123");
    expect(mock).toHaveBeenCalledWith("/api/run", expect.objectContaining({
      method: "POST",
      headers: expect.objectContaining({ "Content-Type": "application/json" }),
    }));
  });

  it("throws ApiError on 422 validation error", async () => {
    mockFetch(422, { detail: "Validation error" });
    await expect(startRun({ template: "", mode: "dry-run", input: {} })).rejects.toMatchObject({ status: 422 });
  });
});

// ─── POST /api/run/:id/resume ─────────────────────────────────────────────────

describe("resumeRun", () => {
  it("encodes run_id in URL", async () => {
    const mock = mockFetch(200, { ok: true, run_id: "run/id" });
    await resumeRun("run/id");
    expect(mock).toHaveBeenCalledWith(
      expect.stringContaining(encodeURIComponent("run/id")),
      expect.objectContaining({ method: "POST" })
    );
  });
});
