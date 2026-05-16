/**
 * useRoute unit tests.
 *
 * On-demand hook — no polling, no AppState gating. We verify:
 *  - fetch(id) hits the right URL and stores the response
 *  - fetch(undefined) routes to the active dispatch target
 *  - fetch(null) clears the data
 *  - errors are surfaced
 *  - calls without serverUrl/token are no-ops
 */
import { act, renderHook, waitFor } from '@testing-library/react-native';

import { useRoute } from '../useRoute';
import type { RouteResponse } from '../api';

const URL = 'http://api.test';
const TOKEN = 'tok-abc';

function mockFetchOnce(body: RouteResponse): jest.Mock {
  const fn = jest.fn().mockResolvedValueOnce({
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  });
  global.fetch = fn as unknown as typeof fetch;
  return fn;
}

afterEach(() => {
  jest.restoreAllMocks();
});

describe('useRoute', () => {
  it('fetches and stores a route when fetch(id) is called', async () => {
    const route: RouteResponse = {
      waypoints: [
        { lat: 37.91, lon: -122.58 },
        { lat: 37.913, lon: -122.581 },
        { lat: 37.913, lon: -122.585 },
        { lat: 37.91, lon: -122.585 },
      ],
      snapped: true,
    };
    const fn = mockFetchOnce(route);

    const { result } = renderHook(() => useRoute(URL, TOKEN));
    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(false);

    await act(async () => {
      await result.current.fetch(42);
    });

    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn.mock.calls[0][0]).toContain('/field/me/route?segment_id=42');
    expect(result.current.data).toEqual(route);
    expect(result.current.error).toBeNull();
  });

  it('fetches the active dispatch route when fetch(undefined) is called', async () => {
    const route: RouteResponse = {
      waypoints: [{ lat: 37.91, lon: -122.58 }, { lat: 37.92, lon: -122.59 }],
      snapped: false,
    };
    const fn = mockFetchOnce(route);

    const { result } = renderHook(() => useRoute(URL, TOKEN));
    await act(async () => {
      await result.current.fetch(undefined);
    });

    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn.mock.calls[0][0]).toContain('/field/me/route');
    expect(fn.mock.calls[0][0]).not.toContain('segment_id=');
    expect(result.current.data).toEqual(route);
  });

  it('clears state when fetch(null) is called', async () => {
    const route: RouteResponse = {
      waypoints: [{ lat: 0, lon: 0 }, { lat: 1, lon: 1 }],
      snapped: false,
    };
    mockFetchOnce(route);

    const { result } = renderHook(() => useRoute(URL, TOKEN));
    await act(async () => {
      await result.current.fetch(1);
    });
    expect(result.current.data).not.toBeNull();

    await act(async () => {
      await result.current.fetch(null);
    });
    expect(result.current.data).toBeNull();
  });

  it('no-ops without serverUrl or token', async () => {
    const fn = jest.fn();
    global.fetch = fn as unknown as typeof fetch;

    const { result } = renderHook(() => useRoute(null, TOKEN));
    await act(async () => {
      await result.current.fetch(1);
    });
    expect(fn).not.toHaveBeenCalled();
    expect(result.current.data).toBeNull();
  });

  it('surfaces errors', async () => {
    global.fetch = jest.fn().mockRejectedValueOnce(new Error('network down')) as unknown as typeof fetch;

    const { result } = renderHook(() => useRoute(URL, TOKEN));
    await act(async () => {
      await result.current.fetch(1);
    });

    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.error?.message).toMatch(/network down|fetch failed/);
    expect(result.current.data).toBeNull();
  });
});
