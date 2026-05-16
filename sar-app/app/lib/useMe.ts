/**
 * useMe — polls GET /field/me at MISSION_STATE_POLL_INTERVAL_MS while the
 * app is foregrounded. Returns the latest MeResponse + a manual refresh fn.
 *
 * Mirrors the gating pattern in useMissionState (pause on AppState change,
 * resume on 'active'). One in-flight fetch at a time to avoid request
 * stacking when the network is slow.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { AppState } from 'react-native';

import { getMe, type MeResponse } from './api';
import { MISSION_STATE_POLL_INTERVAL_MS } from '../config';

export type UseMeResult = {
  data: MeResponse | null;
  /** True until the first successful response. */
  loading: boolean;
  /** Last error encountered (cleared on the next success). */
  error: Error | null;
  /** Manually re-fetch — used when an action (ack/start/complete) should
   *  reflect immediately without waiting for the next poll tick. */
  refresh: () => Promise<void>;
};

export function useMe(
  serverUrl: string | null,
  bearerToken: string | null,
  intervalMs: number = MISSION_STATE_POLL_INTERVAL_MS,
): UseMeResult {
  const [data, setData] = useState<MeResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<Error | null>(null);

  // Live refs so the polling closure and the exposed `refresh` callback
  // always see the latest URL/token without restarting the effect on
  // every render.
  const urlRef = useRef<string | null>(serverUrl);
  const tokenRef = useRef<string | null>(bearerToken);
  urlRef.current = serverUrl;
  tokenRef.current = bearerToken;

  const inflightRef = useRef<boolean>(false);

  const fetchOnce = useCallback(async () => {
    const u = urlRef.current;
    const t = tokenRef.current;
    if (!u || !t) return;
    if (inflightRef.current) return;
    inflightRef.current = true;
    try {
      const fresh = await getMe(u, t);
      setData(fresh);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e : new Error(String(e)));
    } finally {
      inflightRef.current = false;
      setLoading(false);
    }
  }, []);

  const refresh = useCallback(async () => {
    await fetchOnce();
  }, [fetchOnce]);

  useEffect(() => {
    if (!serverUrl || !bearerToken) {
      setData(null);
      setLoading(false);
      return;
    }

    let cancelled = false;
    let intervalId: ReturnType<typeof setInterval> | null = null;

    const tick = () => {
      if (cancelled) return;
      void fetchOnce();
    };

    const start = () => {
      if (intervalId) return;
      tick();
      intervalId = setInterval(tick, intervalMs);
    };
    const stop = () => {
      if (intervalId) {
        clearInterval(intervalId);
        intervalId = null;
      }
    };

    if (AppState.currentState === 'active') start();
    const sub = AppState.addEventListener('change', (s) => {
      if (s === 'active') start();
      else stop();
    });

    return () => {
      cancelled = true;
      stop();
      sub.remove();
    };
  }, [serverUrl, bearerToken, intervalMs, fetchOnce]);

  return { data, loading, error, refresh };
}
