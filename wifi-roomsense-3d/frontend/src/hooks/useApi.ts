import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage } from '../api/client';

export interface ApiState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** Re-run the request now. */
  reload: () => void;
}

/**
 * Run an API request on mount (and optionally poll). Errors are kept as text
 * for display; data is never substituted with placeholders.
 */
export function useApi<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[] = [],
  pollMs: number | null = null,
): ApiState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    fetcherRef
      .current(ctrl.signal)
      .then((d) => {
        if (ctrl.signal.aborted) return;
        setData(d);
        setError(null);
      })
      .catch((err: unknown) => {
        if (ctrl.signal.aborted) return;
        // Keep the previous data visible but clearly marked as failed to refresh.
        setError(errorMessage(err));
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
      });
    return () => ctrl.abort();
  }, [tick, ...deps]);

  useEffect(() => {
    if (!pollMs) return undefined;
    const id = window.setInterval(() => setTick((t) => t + 1), pollMs);
    return () => window.clearInterval(id);
  }, [pollMs]);

  return { data, error, loading, reload };
}

/** Run a one-off action (POST/PUT/DELETE) with busy/error/result state. */
export function useAction<A extends unknown[], R>(
  action: (...args: A) => Promise<R>,
): {
  run: (...args: A) => Promise<R | undefined>;
  busy: boolean;
  error: string | null;
  result: R | undefined;
  clear: () => void;
} {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<R | undefined>(undefined);
  const actionRef = useRef(action);
  actionRef.current = action;
  const run = useCallback(async (...args: A): Promise<R | undefined> => {
    setBusy(true);
    setError(null);
    try {
      const r = await actionRef.current(...args);
      setResult(r);
      return r;
    } catch (err) {
      setError(errorMessage(err));
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);
  const clear = useCallback(() => {
    setError(null);
    setResult(undefined);
  }, []);
  return { run, busy, error, result, clear };
}
