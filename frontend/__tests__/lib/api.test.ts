/**
 * Unit tests for `frontend/lib/api.ts`.
 *
 * All HTTP calls are intercepted by mocking `global.fetch`.  SSE streaming
 * tests mock the `EventSource` global.  No real network requests are made.
 */

import {
  ApiError,
  listTemplates,
  getTemplate,
  validateTemplate,
  createTemplate,
  updateTemplate,
  deleteTemplate,
  startRun,
  listRuns,
  getRun,
  getRunLogs,
  cancelRun,
  streamRun,
  getHealth,
} from '@/lib/api';
import type {
  TemplateSummary,
  TemplateDetail,
  RunRecord,
  SseEvent,
} from '@/lib/types';

// ── Helpers ───────────────────────────────────────────────────────────────────

/** Build a minimal `Response`-like object for mocking `fetch`. */
function makeResponse(
  body: unknown,
  status = 200,
  ok?: boolean,
): Response {
  const json = JSON.stringify(body);
  return {
    ok: ok ?? (status >= 200 && status < 300),
    status,
    statusText: status === 200 ? 'OK' : String(status),
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(json),
  } as unknown as Response;
}

/** Minimal TemplateSummary fixture. */
const TEMPLATE_SUMMARY: TemplateSummary = {
  id: 'content-pipeline-v1',
  name: 'Content Pipeline v1',
  version: '1.0.0',
  description: 'Produces articles.',
  phases_count: 3,
  category: 'content',
  author: 'test',
};

/** Minimal TemplateDetail fixture. */
const TEMPLATE_DETAIL: TemplateDetail = {
  ...TEMPLATE_SUMMARY,
  phases: [
    {
      id: 'research',
      name: 'Research',
      description: 'Gather information.',
      model_tier: 'tier2',
      thinking_level: 'low',
      depends_on: [],
      task_type: 'research',
    },
  ],
  example_input: null,
  config_schema: {},
  tags: ['content', 'article'],
};

/** Minimal RunRecord fixture. */
const RUN_RECORD: RunRecord = {
  run_id: 'abc12345',
  template_id: 'content-pipeline-v1',
  template_path: '/path/to/template.yaml',
  mode: 'dry-run',
  status: 'pending',
  current_phase: null,
  completed_phases: [],
  pid: null,
  output_dir: '/tmp/output',
  error_message: null,
  gateway_url: null,
  skip_scoring: false,
  scoring_status: null,
  scoring_score: null,
  started_at: null,
  completed_at: null,
  created_at: '2026-03-01T00:00:00',
};

// ── Setup / teardown ──────────────────────────────────────────────────────────

let fetchMock: jest.MockedFunction<typeof fetch>;

beforeEach(() => {
  fetchMock = jest.fn();
  global.fetch = fetchMock;
});

afterEach(() => {
  jest.resetAllMocks();
});

// ── ApiError ──────────────────────────────────────────────────────────────────

describe('ApiError', () => {
  it('stores status and detail', () => {
    const err = new ApiError(404, { detail: 'Not found' });
    expect(err.status).toBe(404);
    expect(err.detail).toEqual({ detail: 'Not found' });
    expect(err.name).toBe('ApiError');
  });

  it('uses custom message when provided', () => {
    const err = new ApiError(500, null, 'Custom message');
    expect(err.message).toBe('Custom message');
  });

  it('generates default message from status when no message provided', () => {
    const err = new ApiError(422, null);
    expect(err.message).toBe('API error 422');
  });

  it('is an instance of Error', () => {
    expect(new ApiError(400, null)).toBeInstanceOf(Error);
  });
});

// ── Network failure ───────────────────────────────────────────────────────────

describe('network failure', () => {
  it('propagates TypeError on network error', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(listTemplates()).rejects.toThrow(TypeError);
  });
});

// ── Health ────────────────────────────────────────────────────────────────────

describe('getHealth', () => {
  it('calls GET /api/v1/health and returns parsed body', async () => {
    const body = { status: 'ok', version: '1.2.3' };
    fetchMock.mockResolvedValue(makeResponse(body));

    const result = await getHealth();
    expect(result).toEqual(body);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/health',
      expect.objectContaining({ headers: expect.objectContaining({ Accept: 'application/json' }) }),
    );
  });
});

// ── listTemplates ─────────────────────────────────────────────────────────────

describe('listTemplates', () => {
  it('calls GET /api/v1/templates and returns array', async () => {
    fetchMock.mockResolvedValue(makeResponse([TEMPLATE_SUMMARY]));

    const templates = await listTemplates();
    expect(templates).toHaveLength(1);
    expect(templates[0]).toMatchObject({ id: 'content-pipeline-v1' });
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/templates',
      expect.objectContaining({ headers: expect.objectContaining({ Accept: 'application/json' }) }),
    );
  });

  it('throws ApiError on 500', async () => {
    fetchMock.mockResolvedValue(makeResponse({ detail: 'Server error' }, 500));

    await expect(listTemplates()).rejects.toBeInstanceOf(ApiError);
    const err = await listTemplates().catch((e: ApiError) => e);
    expect(err.status).toBe(500);
  });
});

// ── getTemplate ───────────────────────────────────────────────────────────────

describe('getTemplate', () => {
  it('calls GET /api/v1/templates/{name} and returns detail', async () => {
    fetchMock.mockResolvedValue(makeResponse(TEMPLATE_DETAIL));

    const result = await getTemplate('content-pipeline-v1');
    expect(result).toMatchObject({ id: 'content-pipeline-v1', phases: expect.any(Array) });
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/templates/content-pipeline-v1',
      expect.anything(),
    );
  });

  it('URL-encodes the template name', async () => {
    fetchMock.mockResolvedValue(makeResponse(TEMPLATE_DETAIL));
    await getTemplate('my template/v1');

    const calledUrl = (fetchMock.mock.calls[0] as string[])[0];
    expect(calledUrl).toBe('/api/v1/templates/my%20template%2Fv1');
  });

  it('throws ApiError 404 when template not found', async () => {
    fetchMock.mockResolvedValue(
      makeResponse({ detail: "Template 'x' not found" }, 404),
    );

    await expect(getTemplate('x')).rejects.toBeInstanceOf(ApiError);
    const err = await getTemplate('x').catch((e: ApiError) => e);
    expect(err.status).toBe(404);
  });
});

// ── validateTemplate ──────────────────────────────────────────────────────────

describe('validateTemplate', () => {
  it('calls POST /api/v1/templates/validate', async () => {
    const body = { valid: true, errors: [], warnings: [] };
    fetchMock.mockResolvedValue(makeResponse(body));

    const result = await validateTemplate({ content: 'id: my-tpl\n' });
    expect(result.valid).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/templates/validate',
      expect.objectContaining({ method: 'POST' }),
    );
  });
});

// ── createTemplate ────────────────────────────────────────────────────────────

describe('createTemplate', () => {
  it('calls POST /api/v1/templates with 201 and returns body', async () => {
    const body = {
      id: 'new-tpl',
      name: 'New',
      version: '1.0',
      path: '/usr/.orch/templates/new-tpl.yaml',
      source: 'user',
      phases_count: 1,
      created: true,
    };
    fetchMock.mockResolvedValue(makeResponse(body, 201));

    const result = await createTemplate({ content: 'id: new-tpl\n' });
    expect(result.created).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/templates',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('throws ApiError 409 on conflict', async () => {
    fetchMock.mockResolvedValue(
      makeResponse({ detail: 'Template already exists' }, 409),
    );

    await expect(
      createTemplate({ content: 'id: existing\n' }),
    ).rejects.toBeInstanceOf(ApiError);
  });
});

// ── updateTemplate ────────────────────────────────────────────────────────────

describe('updateTemplate', () => {
  it('calls PUT /api/v1/templates/{name} and URL-encodes name', async () => {
    const body = {
      id: 'content-pipeline-v1',
      name: 'Content Pipeline v1',
      version: '1.1',
      path: '/path',
      source: 'user',
      phases_count: 2,
      created: false,
    };
    fetchMock.mockResolvedValue(makeResponse(body));

    await updateTemplate('content pipeline/v1', { content: 'id: content-pipeline-v1\n' });

    const calledUrl = (fetchMock.mock.calls[0] as string[])[0];
    expect(calledUrl).toBe('/api/v1/templates/content%20pipeline%2Fv1');
    expect(fetchMock).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({ method: 'PUT' }),
    );
  });

  it('throws ApiError 403 on bundled template', async () => {
    fetchMock.mockResolvedValue(
      makeResponse({ detail: 'Bundled template' }, 403),
    );
    await expect(
      updateTemplate('bundled', { content: 'x' }),
    ).rejects.toBeInstanceOf(ApiError);
  });
});

// ── deleteTemplate ────────────────────────────────────────────────────────────

describe('deleteTemplate', () => {
  it('calls DELETE /api/v1/templates/{name}', async () => {
    const body = {
      deleted: true,
      id: 'old-tpl',
      path: '/path/to/old-tpl.yaml',
      source: 'user',
    };
    fetchMock.mockResolvedValue(makeResponse(body));

    const result = await deleteTemplate('old-tpl');
    expect(result.deleted).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/templates/old-tpl',
      expect.objectContaining({ method: 'DELETE' }),
    );
  });

  it('URL-encodes the template name', async () => {
    fetchMock.mockResolvedValue(
      makeResponse({ deleted: true, id: 'x', path: 'x', source: 'user' }),
    );
    await deleteTemplate('my template');

    const calledUrl = (fetchMock.mock.calls[0] as string[])[0];
    expect(calledUrl).toBe('/api/v1/templates/my%20template');
  });
});

// ── startRun ──────────────────────────────────────────────────────────────────

describe('startRun', () => {
  it('calls POST /api/v1/runs with body and returns run record', async () => {
    fetchMock.mockResolvedValue(makeResponse(RUN_RECORD, 201));

    const result = await startRun({
      template: 'content-pipeline-v1',
      mode: 'dry-run',
      input: { topic: 'test' },
    });
    expect(result.run_id).toBe('abc12345');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/runs',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
      }),
    );
  });

  it('sends input JSON correctly', async () => {
    fetchMock.mockResolvedValue(makeResponse(RUN_RECORD, 201));

    await startRun({
      template: 'tpl',
      mode: 'standalone',
      input: { key: 'value' },
    });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body).toMatchObject({
      template: 'tpl',
      mode: 'standalone',
      input: { key: 'value' },
    });
  });

  it('throws ApiError on 422 validation error', async () => {
    fetchMock.mockResolvedValue(
      makeResponse({ detail: { message: 'Template has errors' } }, 422),
    );
    await expect(
      startRun({ template: 'bad-tpl', mode: 'dry-run', input: {} }),
    ).rejects.toBeInstanceOf(ApiError);
  });
});

// ── listRuns ──────────────────────────────────────────────────────────────────

describe('listRuns', () => {
  it('calls GET /api/v1/runs without params', async () => {
    fetchMock.mockResolvedValue(
      makeResponse({ items: [RUN_RECORD], total: 1, limit: 20, offset: 0 }),
    );

    const result = await listRuns();
    expect(result.items).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/runs',
      expect.anything(),
    );
  });

  it('appends query params when provided', async () => {
    fetchMock.mockResolvedValue(
      makeResponse({ items: [], total: 0, limit: 5, offset: 10 }),
    );

    await listRuns({ status: 'running', template_id: 'my-tpl', limit: 5, offset: 10 });

    const calledUrl = (fetchMock.mock.calls[0] as string[])[0];
    expect(calledUrl).toContain('status=running');
    expect(calledUrl).toContain('template_id=my-tpl');
    expect(calledUrl).toContain('limit=5');
    expect(calledUrl).toContain('offset=10');
  });
});

// ── getRun ────────────────────────────────────────────────────────────────────

describe('getRun', () => {
  it('calls GET /api/v1/runs/{run_id}', async () => {
    fetchMock.mockResolvedValue(makeResponse(RUN_RECORD));

    const result = await getRun('abc12345');
    expect(result.run_id).toBe('abc12345');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/runs/abc12345',
      expect.anything(),
    );
  });

  it('URL-encodes run_id', async () => {
    fetchMock.mockResolvedValue(makeResponse(RUN_RECORD));
    await getRun('run/with/slash');

    const calledUrl = (fetchMock.mock.calls[0] as string[])[0];
    expect(calledUrl).toBe('/api/v1/runs/run%2Fwith%2Fslash');
  });

  it('throws ApiError 404 for unknown run', async () => {
    fetchMock.mockResolvedValue(makeResponse({ detail: 'Not found' }, 404));
    await expect(getRun('unknown')).rejects.toBeInstanceOf(ApiError);
    const err = await getRun('unknown').catch((e: ApiError) => e);
    expect(err.status).toBe(404);
  });
});

// ── getRunLogs ────────────────────────────────────────────────────────────────

describe('getRunLogs', () => {
  it('calls GET /api/v1/runs/{run_id}/logs', async () => {
    fetchMock.mockResolvedValue(
      makeResponse({ run_id: 'abc12345', log: 'daemon log line 1\n' }),
    );

    const result = await getRunLogs('abc12345');
    expect(result.log).toContain('daemon log line 1');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/runs/abc12345/logs',
      expect.anything(),
    );
  });
});

// ── cancelRun ─────────────────────────────────────────────────────────────────

describe('cancelRun', () => {
  it('calls DELETE /api/v1/runs/{run_id}', async () => {
    fetchMock.mockResolvedValue(
      makeResponse({ run_id: 'abc12345', cancelled: true }),
    );

    const result = await cancelRun('abc12345');
    expect(result.cancelled).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/runs/abc12345',
      expect.objectContaining({ method: 'DELETE' }),
    );
  });

  it('throws ApiError 409 when run is already terminal', async () => {
    fetchMock.mockResolvedValue(
      makeResponse({ detail: 'Already in terminal state' }, 409),
    );
    await expect(cancelRun('done-run')).rejects.toBeInstanceOf(ApiError);
    const err = await cancelRun('done-run').catch((e: ApiError) => e);
    expect(err.status).toBe(409);
  });
});

// ── URL encoding (general) ────────────────────────────────────────────────────

describe('URL encoding', () => {
  it.each([
    ['hello world', 'hello%20world'],
    ['a/b/c', 'a%2Fb%2Fc'],
    ['tpl@2.0', 'tpl%402.0'],
    ['tpl#v2', 'tpl%23v2'],
  ])('encodes "%s" as "%s" in template name', async (input, encoded) => {
    fetchMock.mockResolvedValue(makeResponse(TEMPLATE_DETAIL));
    await getTemplate(input);
    const calledUrl = (fetchMock.mock.calls[0] as string[])[0];
    expect(calledUrl).toBe(`/api/v1/templates/${encoded}`);
  });
});

// ── streamRun (SSE) ───────────────────────────────────────────────────────────

/** Minimal EventSource mock. */
class MockEventSource {
  readonly url: string;
  onerror: ((e: Event) => void) | null = null;
  private listeners: Map<string, ((e: MessageEvent) => void)[]> = new Map();

  constructor(url: string) {
    this.url = url;
  }

  addEventListener(type: string, handler: (e: MessageEvent) => void): void {
    const existing = this.listeners.get(type) ?? [];
    this.listeners.set(type, [...existing, handler]);
  }

  close(): void {
    this.listeners.clear();
  }

  /** Test helper: simulate an SSE event arriving. */
  emit(eventType: string, data: unknown): void {
    const handlers = this.listeners.get(eventType) ?? [];
    const event = { data: JSON.stringify(data), type: eventType } as unknown as MessageEvent;
    for (const h of handlers) h(event);
  }
}

describe('streamRun', () => {
  let EventSourceMock: typeof MockEventSource;
  let mockInstance: MockEventSource;
  const originalEventSource = global.EventSource;

  beforeEach(() => {
    EventSourceMock = class extends MockEventSource {};
    // Keep reference to the instance created inside streamRun
    const Spy = new Proxy(EventSourceMock, {
      construct(Target, args: [string]) {
        mockInstance = new Target(...args);
        return mockInstance;
      },
    });
    // @ts-expect-error — assigning mock to global
    global.EventSource = Spy;
  });

  afterEach(() => {
    global.EventSource = originalEventSource;
  });

  it('constructs an EventSource with the correct URL', () => {
    const stop = streamRun('abc12345', jest.fn());
    expect(mockInstance.url).toBe('/api/v1/runs/abc12345/stream');
    stop();
  });

  it('URL-encodes the run_id in the stream URL', () => {
    const stop = streamRun('run/with/slash', jest.fn());
    expect(mockInstance.url).toContain('run%2Fwith%2Fslash');
    stop();
  });

  it('delivers phase_started events to onEvent callback', () => {
    const received: SseEvent[] = [];
    const stop = streamRun('abc12345', (e) => received.push(e));

    mockInstance.emit('phase_started', {
      run_id: 'abc12345',
      phase_id: 'research',
      tokens_consumed: null,
      cost_usd: null,
      state: 'running',
      created_at: '2026-03-01T00:00:00',
    });

    expect(received).toHaveLength(1);
    expect(received[0]?.type).toBe('phase_started');
    if (received[0]?.type === 'phase_started') {
      expect(received[0].phase_id).toBe('research');
    }
    stop();
  });

  it('delivers phase_completed events to onEvent callback', () => {
    const received: SseEvent[] = [];
    const stop = streamRun('abc12345', (e) => received.push(e));

    mockInstance.emit('phase_completed', {
      run_id: 'abc12345',
      phase_id: 'research',
      tokens_consumed: 1500,
      cost_usd: 0.003,
      state: 'success',
      created_at: '2026-03-01T00:01:00',
    });

    expect(received).toHaveLength(1);
    expect(received[0]?.type).toBe('phase_completed');
    if (received[0]?.type === 'phase_completed') {
      expect(received[0].tokens_consumed).toBe(1500);
    }
    stop();
  });

  it('delivers status_changed events to onEvent callback', () => {
    const received: SseEvent[] = [];
    const stop = streamRun('abc12345', (e) => received.push(e));

    mockInstance.emit('status_changed', {
      run_id: 'abc12345',
      phase_id: null,
      status: 'success',
      completed_at: '2026-03-01T00:05:00',
      error_message: null,
    });

    expect(received).toHaveLength(1);
    expect(received[0]?.type).toBe('status_changed');
    if (received[0]?.type === 'status_changed') {
      expect(received[0].status).toBe('success');
    }
    stop();
  });

  it('delivers error events to onEvent callback', () => {
    const received: SseEvent[] = [];
    const stop = streamRun('abc12345', (e) => received.push(e));

    mockInstance.emit('error', { error: "Run 'abc12345' not found" });

    expect(received).toHaveLength(1);
    expect(received[0]?.type).toBe('error');
    if (received[0]?.type === 'error') {
      expect(received[0].error).toContain('not found');
    }
    stop();
  });

  it('calls onError when onerror is triggered', () => {
    const onError = jest.fn();
    const stop = streamRun('abc12345', jest.fn(), onError);

    const fakeEvent = {} as Event;
    if (mockInstance.onerror) mockInstance.onerror(fakeEvent);

    expect(onError).toHaveBeenCalledWith(fakeEvent);
    stop();
  });

  it('does not throw if onError is not provided', () => {
    const stop = streamRun('abc12345', jest.fn());
    // Should not throw even if onerror fires (no callback registered)
    expect(() => stop()).not.toThrow();
  });

  it('closes the EventSource when the cleanup function is called', () => {
    const closeSpy = jest.spyOn(MockEventSource.prototype, 'close');
    const stop = streamRun('abc12345', jest.fn());
    stop();
    expect(closeSpy).toHaveBeenCalledTimes(1);
  });

  it('ignores unknown event types without throwing', () => {
    const received: SseEvent[] = [];
    const stop = streamRun('abc12345', (e) => received.push(e));

    // Emit a type that doesn't match any known event
    mockInstance.emit('unknown_event', { data: 'test' });

    expect(received).toHaveLength(0);
    stop();
  });
});
