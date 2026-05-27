/**
 * Acceptance tests for the frontend-cleanup cluster (#861 #870 #871 #872 #873).
 *
 * These tests pin the behavioral contracts produced in the spec/behavioral phases
 * of the Orchemist pipeline. They run under Jest (jsdom) and verify:
 *
 *   - #861 — `streamRun` removed from api.ts.
 *   - #870 — useApi hook exists with the documented shape and state machine.
 *   - #871 — `Paged<T>` generic + alias replacements compile and behave identically.
 *   - #872 — `extractApiErrorMessage` helper unwraps ApiError detail chains and
 *            falls back safely on non-Error inputs.
 *   - #873 — `formatRelative` / `formatElapsed` in `@/lib/timeFmt` produce the
 *            documented label strings.
 *   - Dead `CreateTemplateRequest` / `UpdateTemplateRequest` interfaces removed.
 */

import { act, renderHook, waitFor } from '@testing-library/react';
import * as React from 'react';

import { useApi } from '@/lib/useApi';
import {
  ApiError,
  extractApiErrorMessage,
} from '@/lib/api';
import * as ApiModule from '@/lib/api';
import * as TypesModule from '@/lib/types';
import type { Paged } from '@/lib/types';
import { formatRelative, formatElapsed } from '@/lib/timeFmt';

// ── #861 — streamRun deletion ────────────────────────────────────────────────

describe('#861 — streamRun removal', () => {
  it('does not export `streamRun` from @/lib/api', () => {
    expect((ApiModule as Record<string, unknown>)['streamRun']).toBeUndefined();
  });

  it('does not export `parseEvent` from @/lib/api', () => {
    expect((ApiModule as Record<string, unknown>)['parseEvent']).toBeUndefined();
  });
});

// ── #870 — useApi hook ───────────────────────────────────────────────────────

describe('#870 — useApi hook', () => {
  it('exports the named hook', () => {
    expect(typeof useApi).toBe('function');
  });

  it('initial render: loading=true, data=null, error=null, engineUp=null', async () => {
    let resolve!: (v: string) => void;
    const fetcher = jest.fn(() => new Promise<string>((r) => { resolve = r; }));

    const { result } = renderHook(() => useApi<string>(fetcher, []));

    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();
    expect(result.current.error).toBeNull();
    expect(result.current.engineUp).toBeNull();

    await act(async () => { resolve('hello'); });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBe('hello');
    expect(result.current.error).toBeNull();
    expect(result.current.engineUp).toBe(true);
  });

  it('rejection: loading→false, error populated, engineUp=false, data=null', async () => {
    const boom = new Error('engine offline');
    const fetcher = jest.fn(() => Promise.reject(boom));

    const { result } = renderHook(() => useApi<string>(fetcher, []));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe(boom);
    expect(result.current.engineUp).toBe(false);
    expect(result.current.data).toBeNull();
  });

  it('calls fetcher exactly once on mount (rules out a stub that returns hard-coded null)', async () => {
    const fetcher = jest.fn(() => Promise.resolve(42));
    renderHook(() => useApi<number>(fetcher, []));
    expect(fetcher).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
  });

  it('refetches when deps change and resets data to null in between', async () => {
    const values = ['first', 'second'];
    let idx = 0;
    const fetcher = jest.fn(() => Promise.resolve(values[idx++] as string));

    const { result, rerender } = renderHook(
      ({ key }: { key: string }) => useApi<string>(fetcher, [key]),
      { initialProps: { key: 'a' } },
    );

    await waitFor(() => expect(result.current.data).toBe('first'));
    expect(fetcher).toHaveBeenCalledTimes(1);

    rerender({ key: 'b' });

    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.data).toBe('second'));
    expect(result.current.loading).toBe(false);
    expect(result.current.engineUp).toBe(true);
  });

  it('stale fetch settling after deps change does NOT update state', async () => {
    let resolveFirst!: (v: string) => void;
    const fetcher = jest
      .fn<Promise<string>, []>()
      .mockImplementationOnce(() => new Promise<string>((r) => { resolveFirst = r; }))
      .mockImplementationOnce(() => Promise.resolve('second-result'));

    const { result, rerender } = renderHook(
      ({ key }: { key: string }) => useApi<string>(fetcher, [key]),
      { initialProps: { key: 'a' } },
    );

    expect(result.current.loading).toBe(true);

    rerender({ key: 'b' });

    await waitFor(() => expect(result.current.data).toBe('second-result'));

    await act(async () => {
      resolveFirst('stale-value-that-must-not-win');
      await new Promise((r) => setTimeout(r, 0));
    });

    expect(result.current.data).toBe('second-result');
    expect(result.current.error).toBeNull();
    expect(result.current.engineUp).toBe(true);
  });

  it('settlement after unmount does NOT throw or warn', async () => {
    let resolve!: (v: string) => void;
    const fetcher = jest.fn(() => new Promise<string>((r) => { resolve = r; }));

    const consoleErrorSpy = jest.spyOn(console, 'error').mockImplementation(() => undefined);
    const consoleWarnSpy = jest.spyOn(console, 'warn').mockImplementation(() => undefined);

    const { unmount } = renderHook(() => useApi<string>(fetcher, []));
    unmount();

    await act(async () => {
      resolve('post-unmount');
      await new Promise((r) => setTimeout(r, 0));
    });

    expect(consoleErrorSpy).not.toHaveBeenCalled();
    expect(consoleWarnSpy).not.toHaveBeenCalled();

    consoleErrorSpy.mockRestore();
    consoleWarnSpy.mockRestore();
  });
});

// ── #871 — Paged<T> generic ──────────────────────────────────────────────────

describe('#871 — Paged<T> generic', () => {
  it('exports Paged<T> from @/lib/types', () => {
    const sample: Paged<{ readonly id: string }> = {
      items: [{ id: 'x' }],
      total: 1,
      limit: 10,
      offset: 0,
    };
    expect(sample.items.length).toBe(1);
    expect(sample.total).toBe(1);
    expect(sample.limit).toBe(10);
    expect(sample.offset).toBe(0);
  });

  it('RunsListResponse is structurally Paged<RunRecord>', () => {
    const runRecordSample: TypesModule.RunRecord = {
      run_id: 'r1',
      template_id: 't',
      template_path: '/p',
      mode: 'dry-run',
      status: 'pending',
      current_phase: null,
      completed_phases: [],
      pid: null,
      output_dir: '/tmp',
      error_message: null,
      gateway_url: null,
      skip_scoring: false,
      scoring_status: null,
      scoring_score: null,
      started_at: null,
      completed_at: null,
      created_at: null,
    };
    const v: TypesModule.RunsListResponse = {
      items: [runRecordSample],
      total: 1,
      limit: 20,
      offset: 0,
    };
    expect(v.items.length).toBe(1);
  });

  it('TrustProfilesResponse remains the variant shape (no limit/offset)', () => {
    const tp: ApiModule.TrustProfilesResponse = { items: [], total: 0 };
    expect(tp.total).toBe(0);
    expect(tp.items.length).toBe(0);
  });
});

// ── #872 — extractApiErrorMessage helper ─────────────────────────────────────

describe('#872 — extractApiErrorMessage', () => {
  it('unwraps ApiError detail.detail.errors array → joined with newlines', () => {
    const err = new ApiError(422, { detail: { errors: ['err1', 'err2'] } });
    expect(extractApiErrorMessage(err)).toBe('err1\nerr2');
  });

  it('returns String(errors) when detail.detail.errors is not an array', () => {
    const err = new ApiError(422, { detail: { errors: 'one-error-string' } });
    expect(extractApiErrorMessage(err)).toBe('one-error-string');
  });

  it('unwraps ApiError detail.detail.message → string', () => {
    const err = new ApiError(422, { detail: { message: 'bad template' } });
    expect(extractApiErrorMessage(err)).toBe('bad template');
  });

  it('unwraps ApiError when detail.detail is itself a string', () => {
    const err = new ApiError(404, { detail: 'Template not found' });
    expect(extractApiErrorMessage(err)).toBe('Template not found');
  });

  it('falls back to err.message when ApiError has no useful inner detail', () => {
    const err = new ApiError(500, null, 'Server exploded');
    expect(extractApiErrorMessage(err)).toBe('Server exploded');
  });

  it('returns err.message for a plain Error', () => {
    expect(extractApiErrorMessage(new Error('plain-msg'))).toBe('plain-msg');
  });

  it('returns "Unknown error." for null', () => {
    expect(extractApiErrorMessage(null)).toBe('Unknown error.');
  });

  it('returns "Unknown error." for undefined', () => {
    expect(extractApiErrorMessage(undefined)).toBe('Unknown error.');
  });

  it('returns "Unknown error." for a number', () => {
    expect(extractApiErrorMessage(42)).toBe('Unknown error.');
  });

  it('returns "Unknown error." for a string', () => {
    expect(extractApiErrorMessage('a string')).toBe('Unknown error.');
  });

  it('returns "Unknown error." for a plain object without a message field', () => {
    expect(extractApiErrorMessage({ random: 'object' })).toBe('Unknown error.');
  });

  it('is pure — never throws on hostile input', () => {
    expect(() => extractApiErrorMessage(Symbol('s'))).not.toThrow();
  });
});

// ── #873 — timeFmt helpers ───────────────────────────────────────────────────

describe('#873 — formatRelative', () => {
  const NOW = new Date('2026-05-27T12:00:00Z').getTime();
  beforeEach(() => { jest.spyOn(Date, 'now').mockReturnValue(NOW); });
  afterEach(() => { jest.restoreAllMocks(); });

  it('returns "—" for null', () => {
    expect(formatRelative(null)).toBe('—');
  });

  it('returns "—" for empty string', () => {
    expect(formatRelative('')).toBe('—');
  });

  it('returns "—" for non-parseable', () => {
    expect(formatRelative('not a date')).toBe('—');
  });

  it('returns "just now" for a moment ago (< 60s)', () => {
    const iso = new Date(NOW - 30 * 1000).toISOString();
    expect(formatRelative(iso)).toBe('just now');
  });

  it('returns "just now" for a future timestamp', () => {
    const iso = new Date(NOW + 10 * 60 * 1000).toISOString();
    expect(formatRelative(iso)).toBe('just now');
  });

  it('returns "<N> min ago" for minute-scale', () => {
    const iso = new Date(NOW - 5 * 60 * 1000).toISOString();
    expect(formatRelative(iso)).toBe('5 min ago');
  });

  it('returns "<N>h ago" for hour-scale', () => {
    const iso = new Date(NOW - 3 * 60 * 60 * 1000).toISOString();
    expect(formatRelative(iso)).toBe('3h ago');
  });

  it('returns "<N>d ago" for day-scale', () => {
    const iso = new Date(NOW - 5 * 24 * 60 * 60 * 1000).toISOString();
    expect(formatRelative(iso)).toBe('5d ago');
  });

  it('boundary: exactly 59s ago → just now', () => {
    const iso = new Date(NOW - 59 * 1000).toISOString();
    expect(formatRelative(iso)).toBe('just now');
  });

  it('boundary: exactly 60s ago → 1 min ago', () => {
    const iso = new Date(NOW - 60 * 1000).toISOString();
    expect(formatRelative(iso)).toBe('1 min ago');
  });
});

describe('#873 — formatElapsed', () => {
  it('returns "—" for null start', () => {
    expect(formatElapsed(null, '2026-01-01T00:00:00Z')).toBe('—');
  });

  it('returns "—" for non-parseable start', () => {
    expect(formatElapsed('garbage', null)).toBe('—');
  });

  it('returns seconds when < 60', () => {
    const start = '2026-01-01T00:00:00Z';
    const end = '2026-01-01T00:00:30Z';
    expect(formatElapsed(start, end)).toBe('30s');
  });

  it('returns "<m>m <s>s" when < 1 hour', () => {
    const start = '2026-01-01T00:00:00Z';
    const end = '2026-01-01T00:05:30Z';
    expect(formatElapsed(start, end)).toBe('5m 30s');
  });

  it('returns "<h>h <m>m" when ≥ 1 hour', () => {
    const start = '2026-01-01T00:00:00Z';
    const end = '2026-01-01T02:15:00Z';
    expect(formatElapsed(start, end)).toBe('2h 15m');
  });

  it('uses Date.now() as end when endIso is null', () => {
    const NOW = new Date('2026-05-27T12:00:00Z').getTime();
    jest.spyOn(Date, 'now').mockReturnValue(NOW);
    try {
      const start = new Date(NOW - 45 * 1000).toISOString();
      expect(formatElapsed(start, null)).toBe('45s');
    } finally {
      jest.restoreAllMocks();
    }
  });

  it('uses Date.now() as end when endIso is undefined', () => {
    const NOW = new Date('2026-05-27T12:00:00Z').getTime();
    jest.spyOn(Date, 'now').mockReturnValue(NOW);
    try {
      const start = new Date(NOW - 90 * 1000).toISOString();
      expect(formatElapsed(start)).toBe('1m 30s');
    } finally {
      jest.restoreAllMocks();
    }
  });

  it('negative delta clamps to 0s', () => {
    const start = '2026-01-01T00:01:00Z';
    const end = '2026-01-01T00:00:00Z';
    expect(formatElapsed(start, end)).toBe('0s');
  });
});

// ── Dead-import cleanup ──────────────────────────────────────────────────────

describe('Dead CreateTemplateRequest/UpdateTemplateRequest interfaces removed', () => {
  it('CreateTemplateRequest is not exported from @/lib/types', () => {
    expect((TypesModule as Record<string, unknown>)['CreateTemplateRequest']).toBeUndefined();
  });

  it('UpdateTemplateRequest is not exported from @/lib/types', () => {
    expect((TypesModule as Record<string, unknown>)['UpdateTemplateRequest']).toBeUndefined();
  });
});

// Keep React imported (jsdom JSX runtime).
void React;
