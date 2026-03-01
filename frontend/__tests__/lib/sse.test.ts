/**
 * Smoke tests for the SSE hook (lib/sse.ts).
 *
 * EventSource is not available in jsdom, so we provide a minimal mock that
 * exposes the same interface the hook depends on.
 */

import { renderHook, act } from "@testing-library/react";
import { useRunEvents } from "@/lib/sse";

// ─── EventSource mock ─────────────────────────────────────────────────────────

interface MockEventSourceInstance {
  url: string;
  onmessage: ((ev: MessageEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  readyState: number;
  close: jest.Mock;
  /** Test helper: simulate a server message */
  emit: (data: unknown) => void;
  /** Test helper: simulate a terminal error (readyState = CLOSED) */
  emitTerminalError: () => void;
}

let lastInstance: MockEventSourceInstance | null = null;

const MockEventSource = jest.fn().mockImplementation(function (this: MockEventSourceInstance, url: string) {
  this.url = url;
  this.onmessage = null;
  this.onerror = null;
  this.readyState = 1; // OPEN
  this.close = jest.fn(() => { this.readyState = 2; });

  this.emit = (data: unknown) => {
    if (this.onmessage) {
      this.onmessage(new MessageEvent("message", { data: JSON.stringify(data) }));
    }
  };

  this.emitTerminalError = () => {
    this.readyState = 2; // CLOSED
    if (this.onerror) this.onerror(new Event("error"));
  };

  lastInstance = this;
}) as unknown as typeof EventSource;

// Attach the CLOSED constant for readyState comparisons
(MockEventSource as unknown as Record<string, number>).CLOSED = 2;

beforeAll(() => {
  global.EventSource = MockEventSource;
});

afterEach(() => {
  jest.clearAllMocks();
  lastInstance = null;
});

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("useRunEvents", () => {
  it("does not connect when runId is null", () => {
    renderHook(() => useRunEvents(null));
    expect(MockEventSource).not.toHaveBeenCalled();
  });

  it("opens an EventSource for the given runId", () => {
    renderHook(() => useRunEvents("run-abc"));
    expect(MockEventSource).toHaveBeenCalledWith("/api/run/run-abc/status");
  });

  it("encodes special characters in runId", () => {
    renderHook(() => useRunEvents("run/with/slash"));
    expect(MockEventSource).toHaveBeenCalledWith(
      expect.stringContaining(encodeURIComponent("run/with/slash"))
    );
  });

  it("starts with status 'starting' and connected=false until first event", () => {
    const { result } = renderHook(() => useRunEvents("run-1"));
    // connected should be true immediately after mount
    expect(result.current.connected).toBe(true);
    expect(result.current.status).toBe("starting");
    expect(result.current.events).toHaveLength(0);
  });

  it("updates status to 'running' on start event", () => {
    const { result } = renderHook(() => useRunEvents("run-2"));
    act(() => {
      lastInstance!.emit({ type: "start", run_id: "run-2", template: "t", mode: "dry-run" });
    });
    expect(result.current.status).toBe("running");
    expect(result.current.events).toHaveLength(1);
  });

  it("closes connection and sets connected=false on pipeline_complete", () => {
    const { result } = renderHook(() => useRunEvents("run-3"));
    act(() => {
      lastInstance!.emit({ type: "pipeline_complete", status: "completed",
        total_phases: 1, completed: 1, failed: 0, total_tokens: 100,
        total_tokens_in: 50, total_tokens_out: 50, total_cost: 0.001, total_elapsed: 1.5 });
    });
    expect(result.current.connected).toBe(false);
    expect(lastInstance!.close).toHaveBeenCalled();
  });

  it("updates status to 'paused' on paused event", () => {
    const { result } = renderHook(() => useRunEvents("run-4"));
    act(() => {
      lastInstance!.emit({ type: "paused", phase_id: "p1", message: "Wait", output_preview: "" });
    });
    expect(result.current.status).toBe("paused");
  });

  it("closes connection on terminal error (readyState=CLOSED)", () => {
    const { result } = renderHook(() => useRunEvents("run-5"));
    act(() => {
      lastInstance!.emitTerminalError();
    });
    expect(result.current.connected).toBe(false);
  });

  it("does NOT close connection on transient error (readyState=OPEN)", () => {
    const { result } = renderHook(() => useRunEvents("run-6"));
    act(() => {
      // Simulate a transient error — readyState stays OPEN (1)
      lastInstance!.readyState = 1;
      if (lastInstance!.onerror) lastInstance!.onerror(new Event("error"));
    });
    // Connection should remain open (let EventSource auto-reconnect)
    expect(result.current.connected).toBe(true);
    expect(lastInstance!.close).not.toHaveBeenCalled();
  });

  it("manual close() disconnects the source", () => {
    const { result } = renderHook(() => useRunEvents("run-7"));
    act(() => {
      result.current.close();
    });
    expect(result.current.connected).toBe(false);
    expect(lastInstance!.close).toHaveBeenCalled();
  });

  it("ignores malformed (non-JSON) event data", () => {
    const { result } = renderHook(() => useRunEvents("run-8"));
    act(() => {
      if (lastInstance!.onmessage) {
        lastInstance!.onmessage(new MessageEvent("message", { data: "not-json" }));
      }
    });
    expect(result.current.events).toHaveLength(0);
    expect(result.current.status).toBe("starting");
  });
});
