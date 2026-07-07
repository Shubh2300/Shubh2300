'use client';

import { useCallback, useEffect, useState } from 'react';

interface AsyncState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  refetch: () => void;
}

/**
 * Runs an async fetcher on mount (and whenever `deps` change) and exposes
 * loading/error/data state plus a manual refetch. Every list/detail page
 * in this app uses this so loading, error ("API unreachable"), and empty
 * states are handled consistently.
 */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const run = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fn()
      .then((result) => {
        if (cancelled) return;
        setData(result);
        setLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err?.message || 'API unreachable');
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => run(), [run]);

  return { data, error, loading, refetch: run };
}
