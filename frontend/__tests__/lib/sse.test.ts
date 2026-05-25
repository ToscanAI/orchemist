/**
 * Unit tests for `frontend/lib/sse.ts` — the `useRunEvents` React hook.
 *
 * The `EventSource` global is replaced with a controllable `MockEventSource`
 * stub so no real network requests are made.  React state updates are wrapped
 * in `act()` as required by `@testing-library/react`.
 */

import { renderHook, act } from '@testing-library/react';
import { useRunEvents } from '@/lib/sse';
import type { RunEventStatus } from '@/lib/sse';
import type { SseEvent } from '@/lib/types';

// ── Mock EventSource ──────────────────────────────────────────────────────────

/**
 * Controllable `EventSource` stub.
 *
 * Stores named event listeners and exposes `emit()` / `triggerOpen()` /
 * `triggerError()` helpers for tests to simulate SSE traffic.
 */
class MockEventSource {
  readonly url: string;
  onopen: (() => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  private listeners: Map<string, ((e: MessageEvent) => void)[]> = new Map();
  /** Tracks whether `close()` has been called. */
  closed = false;

  constructor(url: string) {
    this.url = url;
  }

  addEventListener(type: string, handler: (e: MessageEvent) => void): void {
    const existing = this.listeners.get(type) ?? [];
    this.listeners.set(type, [...existing, handler]);
  }

  close(): void {
    this.closed = true;
    this.listeners.clear();
  }

  /** Test helper: simulate the connection being established. */
  triggerOpen(): void {
    this.onopen?.();
  }

  /** Test helper: simulate a connection error. */
  triggerError(event: Event = {} as Event): void {
    this.onerror?.(event);
  }

  /** Test helper: simulate a named SSE event arriving. */
  emit(eventType: string, data: unknown): void {
    const handlers = this.listeners.get(eventType) ?? [];
    const event = {
      data: JSON.stringify(data),
      type: eventType,
    } as unknown as MessageEvent;
    for (const h of handlers) h(event);
  }
}

// ── Fixtures ──────────────────────────────────────────────────────────────────

const PHASE_STARTED_PAYLOAD = {
  run_id: 'abc12345',
  phase_id: 'research',
  tokens_consumed: null,
  cost_usd: null,
  state: 'running',
  created_at: '2026-03-01T00:00:00',
};

const PHASE_COMPLETED_PAYLOAD = {
  run_id: 'abc12345',
  phase_id: 'research',
  tokens_consumed: 1500,
  cost_usd: 0.003,
  state: 'success',
  created_at: '2026-03-01T00:01:00',
};

const STATUS_CHANGED_SUCCESS_PAYLOAD = {
  run_id: 'abc12345',
  phase_id: null,
  status: 'success',
  completed_at: '2026-03-01T00:05:00',
  error_message: null,
};

const STATUS_CHANGED_FAILED_PAYLOAD = {
  run_id: 'abc12345',
  phase_id: null,
  status: 'failed',
  completed_at: '2026-03-01T00:05:00',
  error_message: 'Phase crashed',
};

const STATUS_CHANGED_CANCELLED_PAYLOAD = {
  run_id: 'abc12345',
  phase_id: null,
  status: 'cancelled',
  completed_at: '2026-03-01T00:05:00',
  error_message: null,
};

// ── Test setup ────────────────────────────────────────────────────────────────

let mockInstance: MockEventSource;
const originalEventSource = global.EventSource;

beforeEach(() => {
  const Spy = new Proxy(MockEventSource, {
    construct(_Target, args: [string]) {
      mockInstance = new MockEventSource(...args);
      return mockInstance;
    },
  });
  // @ts-expect-error — assigning mock to global
  global.EventSource = Spy;
});

afterEach(() => {
  global.EventSource = originalEventSource;
  jest.resetAllMocks();
});

// ── Initial state ─────────────────────────────────────────────────────────────

describe('initial state', () => {
  it('returns empty events array before any events arrive', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));
    expect(result.current.events).toEqual([]);
  });

  it('returns status "connecting" before onopen fires', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));
    expect(result.current.status).toBe<RunEventStatus>('connecting');
  });

  it('returns connected=false before onopen fires', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));
    expect(result.current.connected).toBe(false);
  });
});

// ── Connection lifecycle ──────────────────────────────────────────────────────

describe('connection lifecycle', () => {
  it('constructs EventSource with the correct URL', () => {
    renderHook(() => useRunEvents('abc12345'));
    expect(mockInstance.url).toBe('/api/v1/runs/abc12345/stream');
  });

  it('URL-encodes the runId', () => {
    renderHook(() => useRunEvents('run/with/slash'));
    expect(mockInstance.url).toContain('run%2Fwith%2Fslash');
  });

  it('sets connected=true when onopen fires', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.triggerOpen();
    });

    expect(result.current.connected).toBe(true);
  });

  it('sets connected=false when onerror fires', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.triggerOpen();
    });
    expect(result.current.connected).toBe(true);

    act(() => {
      mockInstance.triggerError();
    });
    expect(result.current.connected).toBe(false);
  });

  it('does not change status on onerror (status stays connecting)', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.triggerError();
    });

    expect(result.current.status).toBe<RunEventStatus>('connecting');
  });

  it('closes the EventSource on unmount', () => {
    const { unmount } = renderHook(() => useRunEvents('abc12345'));
    unmount();
    expect(mockInstance.closed).toBe(true);
  });

  it('resets state and opens a new EventSource when runId changes', () => {
    const { result, rerender } = renderHook(
      ({ runId }: { runId: string }) => useRunEvents(runId),
      { initialProps: { runId: 'run-a' } },
    );

    // Emit an event so state is non-initial.
    act(() => {
      mockInstance.triggerOpen();
      mockInstance.emit('phase_started', PHASE_STARTED_PAYLOAD);
    });

    expect(result.current.events).toHaveLength(1);
    expect(result.current.connected).toBe(true);

    // Capture the first instance before rerender replaces it.
    const firstInstance = mockInstance;

    // Change runId — the effect should re-run.
    rerender({ runId: 'run-b' });

    // Old connection should be closed.
    expect(firstInstance.closed).toBe(true);

    // New EventSource should have been created with the new URL.
    expect(mockInstance.url).toBe('/api/v1/runs/run-b/stream');

    // State should be reset.
    expect(result.current.events).toHaveLength(0);
    expect(result.current.status).toBe<RunEventStatus>('connecting');
    expect(result.current.connected).toBe(false);
  });
});

// ── Event accumulation ────────────────────────────────────────────────────────

describe('event accumulation', () => {
  it('appends phase_started events to events[]', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.emit('phase_started', PHASE_STARTED_PAYLOAD);
    });

    expect(result.current.events).toHaveLength(1);
    expect(result.current.events[0]?.type).toBe('phase_started');
  });

  it('appends phase_completed events to events[]', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.emit('phase_completed', PHASE_COMPLETED_PAYLOAD);
    });

    expect(result.current.events).toHaveLength(1);
    expect(result.current.events[0]?.type).toBe('phase_completed');
    const ev = result.current.events[0];
    if (ev?.type === 'phase_completed') {
      expect(ev.tokens_consumed).toBe(1500);
    }
  });

  it('appends status_changed events to events[]', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.emit('status_changed', STATUS_CHANGED_SUCCESS_PAYLOAD);
    });

    expect(result.current.events).toHaveLength(1);
    expect(result.current.events[0]?.type).toBe('status_changed');
  });

  it('appends error events to events[]', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.emit('error', { error: "Run 'abc12345' not found" });
    });

    expect(result.current.events).toHaveLength(1);
    const ev = result.current.events[0];
    if (ev?.type === 'error') {
      expect(ev.error).toContain('not found');
    }
  });

  it('accumulates multiple events in arrival order', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.emit('phase_started', PHASE_STARTED_PAYLOAD);
      mockInstance.emit('phase_completed', PHASE_COMPLETED_PAYLOAD);
      mockInstance.emit('status_changed', STATUS_CHANGED_SUCCESS_PAYLOAD);
    });

    expect(result.current.events).toHaveLength(3);
    expect(result.current.events.map((e: SseEvent) => e.type)).toEqual([
      'phase_started',
      'phase_completed',
      'status_changed',
    ]);
  });

  it('ignores unrecognised event types without throwing', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.emit('unknown_event', { data: 'test' });
    });

    expect(result.current.events).toHaveLength(0);
  });

  it('ignores events with malformed JSON data', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));

    act(() => {
      // Simulate invalid JSON by directly invoking the listener.
      const badEvent = { data: 'not-json', type: 'phase_started' } as unknown as MessageEvent;
      // We can't call emit() because it JSON.stringifies — manually dispatch instead.
      // Instead, verify the hook doesn't crash when parseRawEvent gets bad input.
      // Since MockEventSource.emit JSON.stringify's the data, test via a custom emit:
      const listeners = (mockInstance as unknown as {
        listeners: Map<string, ((e: MessageEvent) => void)[]>
      }).listeners;
      const handlers = listeners.get('phase_started') ?? [];
      for (const h of handlers) h(badEvent);
    });

    expect(result.current.events).toHaveLength(0);
  });
});

// ── Derived status ────────────────────────────────────────────────────────────

describe('derived status', () => {
  it('transitions to "running" on phase_started', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.emit('phase_started', PHASE_STARTED_PAYLOAD);
    });

    expect(result.current.status).toBe<RunEventStatus>('running');
  });

  it('transitions to "running" on phase_completed', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.emit('phase_completed', PHASE_COMPLETED_PAYLOAD);
    });

    expect(result.current.status).toBe<RunEventStatus>('running');
  });

  it('transitions to "completed" on status_changed with success', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.emit('status_changed', STATUS_CHANGED_SUCCESS_PAYLOAD);
    });

    expect(result.current.status).toBe<RunEventStatus>('completed');
  });

  it('transitions to "error" on status_changed with failed', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.emit('status_changed', STATUS_CHANGED_FAILED_PAYLOAD);
    });

    expect(result.current.status).toBe<RunEventStatus>('error');
  });

  it('transitions to "error" on status_changed with budget_exceeded', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.emit('status_changed', {
        ...STATUS_CHANGED_FAILED_PAYLOAD,
        status: 'budget_exceeded',
      });
    });

    expect(result.current.status).toBe<RunEventStatus>('error');
  });

  it('transitions to "error" on status_changed with scoring_failed', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.emit('status_changed', {
        ...STATUS_CHANGED_FAILED_PAYLOAD,
        status: 'scoring_failed',
      });
    });

    expect(result.current.status).toBe<RunEventStatus>('error');
  });

  it('transitions to "aborted" on status_changed with cancelled', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.emit('status_changed', STATUS_CHANGED_CANCELLED_PAYLOAD);
    });

    expect(result.current.status).toBe<RunEventStatus>('aborted');
  });
});

// ── Terminal state auto-close ─────────────────────────────────────────────────

describe('auto-close on terminal status', () => {
  it('closes the EventSource on success', () => {
    renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.emit('status_changed', STATUS_CHANGED_SUCCESS_PAYLOAD);
    });

    expect(mockInstance.closed).toBe(true);
  });

  it('closes the EventSource on failed', () => {
    renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.emit('status_changed', STATUS_CHANGED_FAILED_PAYLOAD);
    });

    expect(mockInstance.closed).toBe(true);
  });

  it('closes the EventSource on cancelled', () => {
    renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.emit('status_changed', STATUS_CHANGED_CANCELLED_PAYLOAD);
    });

    expect(mockInstance.closed).toBe(true);
  });

  it('sets connected=false on terminal status_changed', () => {
    const { result } = renderHook(() => useRunEvents('abc12345'));

    act(() => {
      mockInstance.triggerOpen();
    });
    expect(result.current.connected).toBe(true);

    act(() => {
      mockInstance.emit('status_changed', STATUS_CHANGED_SUCCESS_PAYLOAD);
    });

    expect(result.current.connected).toBe(false);
  });
});
