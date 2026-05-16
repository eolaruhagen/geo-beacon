/**
 * useRoute — on-demand fetcher for GET /field/me/route?segment_id=X.
 *
 * Not polled. The caller triggers `fetch(segmentId)` when:
 *   - the active dispatch transitions to `acked` or `in_progress` (parent
 *     can watch active_dispatch.status and call this), or
 *   - the user explicitly taps a "show route" / "refresh" button.
 *
 * Cleared when `segmentId === null` is passed (e.g. dispatch completed).
 * Pass `undefined` to route to the current active dispatch entry point.
 */
import { useCallback, useState } from 'react';

import { getRoute, type RouteResponse } from './api';

export type UseRouteResult = {
  data: RouteResponse | null;
  loading: boolean;
  error: Error | null;
  /** Fetch the route for `segmentId`. Pass `undefined` for active target, `null` to clear. */
  fetch: (segmentId: number | null | undefined) => Promise<void>;
};

export function useRoute(
  serverUrl: string | null,
  bearerToken: string | null,
): UseRouteResult {
  const [data, setData] = useState<RouteResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<Error | null>(null);

  const fetch = useCallback(
    async (segmentId: number | null | undefined) => {
      if (segmentId === null) {
        setData(null);
        setError(null);
        return;
      }
      if (!serverUrl || !bearerToken) return;
      setLoading(true);
      try {
        const fresh = await getRoute(serverUrl, bearerToken, segmentId);
        setData(fresh);
        setError(null);
      } catch (e) {
        setError(e instanceof Error ? e : new Error(String(e)));
      } finally {
        setLoading(false);
      }
    },
    [serverUrl, bearerToken],
  );

  return { data, loading, error, fetch };
}
