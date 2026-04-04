'use client';

/**
 * LogViewer — monospace log display with truncation and refresh.
 *
 * Fetches daemon logs for a pipeline run and renders them in a scrollable
 * pre-formatted block. Truncates to last 5000 lines for large logs.
 *
 * @module
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import { getRunLogs, ApiError } from '@/lib/api';

const MAX_LINES = 5000;

interface LogViewerProps {
  runId: string;
}

export function LogViewer({ runId }: LogViewerProps) {
  const [logText, setLogText] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const preRef = useRef<HTMLPreElement>(null);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const fetchLogs = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getRunLogs(runId);
      const log = data.log || '';
      const lines = log.split('\n');
      if (lines.length > MAX_LINES) {
        setLogText(lines.slice(-MAX_LINES).join('\n'));
        setTruncated(true);
      } else {
        setLogText(log);
        setTruncated(false);
      }
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 404) {
        setError('Log file not available — the run may not have started yet.');
      } else {
        setError('Failed to load logs.');
      }
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    fetchLogs();
  }, [fetchLogs]);

  // Auto-scroll to bottom on new content
  useEffect(() => {
    if (preRef.current) {
      preRef.current.scrollTop = preRef.current.scrollHeight;
    }
  }, [logText]);

  if (loading && logText === null) {
    return <div className="py-8 text-center text-zinc-500">Loading logs...</div>;
  }

  if (error) {
    return (
      <div className="rounded-lg border border-zinc-800 p-6 text-center">
        <p className="text-zinc-400">{error}</p>
        <button
          onClick={fetchLogs}
          className="mt-3 rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800"
        >
          Retry
        </button>
      </div>
    );
  }

  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        {truncated && (
          <span className="text-xs text-amber-400">
            Showing last {MAX_LINES.toLocaleString()} lines
          </span>
        )}
        <button
          onClick={fetchLogs}
          disabled={loading}
          className="ml-auto rounded-md border border-zinc-700 bg-zinc-900 px-2.5 py-1 text-xs text-zinc-400 hover:bg-zinc-800 disabled:opacity-50"
        >
          {loading ? 'Loading...' : 'Refresh'}
        </button>
      </div>
      <pre
        ref={preRef}
        className="max-h-[600px] overflow-auto rounded-lg border border-zinc-800 bg-zinc-950 p-4 font-mono text-xs leading-5 text-zinc-300"
      >
        {logText || 'No log output yet.'}
      </pre>
    </div>
  );
}
