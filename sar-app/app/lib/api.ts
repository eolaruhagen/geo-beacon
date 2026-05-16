import type { FindingKind, SweepType } from './missionState';

// Keep these literal unions in sync with api/schemas.py. Source of truth for
// the API contract lives in docs/CONTRACTS.md.
export type DispatchStatus =
  | 'pending'
  | 'acked'
  | 'in_progress'
  | 'completed'
  | 'cancelled'
  | 'superseded';

export type BroadcastKind =
  | 'info'
  | 'warning'
  | 'recall'
  | 'finding_alert'
  | 'route_correction';

export type ActiveDispatch = {
  id: number;
  mission_id: number;
  user_id: number;
  segment_id: number | null;
  sweep_type: SweepType | null;
  entry_lat: number | null;
  entry_lon: number | null;
  instruction: string;
  reasoning: string;
  status: DispatchStatus;
  issued_ts: number;
  acked_ts: number | null;
  started_ts: number | null;
  completed_ts: number | null;
};

export type BroadcastDTO = {
  id: number;
  scope: string;       // 'all' | f'user:{id}'  — already scope-filtered server-side
  kind: BroadcastKind;
  message: string;
  ts: number;
};

// /field/me's payload. segment_geojson is a full GeoJSON Feature, kept
// `any` here so the UI can hand it straight to MapView without retyping.
export type MeResponse = {
  user: {
    id: number;
    display_name: string;
    callsign: string | null;
    role: 'searcher' | 'observer';
    status: string;
    current_mission_id: number | null;
  };
  mission_id: number | null;
  active_dispatch: ActiveDispatch | null;
  segment_geojson: { type: 'Feature'; geometry: any; properties: any } | null;
  nearby_hazards: unknown[];
  recent_broadcasts: BroadcastDTO[];
};

export type DispatchActionResponse = {
  dispatch_id: number;
  status: DispatchStatus;
  user_status: string;
};

export type RouteWaypoint = { lat: number; lon: number };
export type RouteResponse = { waypoints: RouteWaypoint[]; snapped: boolean };

export type AnnouncementsResponse = {
  broadcasts: BroadcastDTO[];
  cursor_ts: number;
};

export class NetworkError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'NetworkError';
  }
}

export class ServerError extends Error {
  constructor(public status: number, public detail: string) {
    super(detail);
    this.name = 'ServerError';
  }
}

export type JoinMissionBody = {
  join_code: string;
  display_name: string;
  callsign?: string;
  role?: 'searcher' | 'observer';
};

export type JoinMissionResponse = {
  mission_id: number;
  bearer_token: string;
  user_id: number;
  callsign: string | null;
};

export type PingBody = {
  lat: number;
  lon: number;
  ts: number;
  accuracy_m?: number;
  speed_mps?: number;
  battery_pct?: number;
};

function normalize(serverUrl: string): string {
  return serverUrl.replace(/\/+$/, '');
}

async function parseDetail(resp: Response): Promise<string> {
  try {
    const json = await resp.json();
    if (typeof json?.detail === 'string') return json.detail;
    if (Array.isArray(json?.detail)) {
      return json.detail.map((d: { msg?: string }) => d.msg ?? '').filter(Boolean).join('; ');
    }
    return JSON.stringify(json);
  } catch {
    return resp.statusText || `HTTP ${resp.status}`;
  }
}

export async function request<T>(
  serverUrl: string,
  path: string,
  init: RequestInit,
): Promise<T> {
  const url = `${normalize(serverUrl)}${path}`;
  let resp: Response;
  try {
    resp = await fetch(url, init);
  } catch (e) {
    throw new NetworkError(e instanceof Error ? e.message : 'fetch failed');
  }
  if (!resp.ok) {
    throw new ServerError(resp.status, await parseDetail(resp));
  }
  const text = await resp.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export async function joinMission(
  serverUrl: string,
  body: JoinMissionBody,
): Promise<JoinMissionResponse> {
  return request<JoinMissionResponse>(serverUrl, '/missions/join', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function postPing(
  serverUrl: string,
  bearerToken: string,
  body: PingBody,
): Promise<void> {
  await request<void>(serverUrl, '/field/ping', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Bearer-Token': bearerToken,
    },
    body: JSON.stringify(body),
  });
}

export type FindingBody = {
  lat: number;
  lon: number;
  kind: FindingKind;
  description?: string;
  confidence: number;
};

export type FindingResp = {
  finding_id: number;
  hex_id: number;
};

export async function postFinding(
  serverUrl: string,
  bearerToken: string,
  body: FindingBody,
): Promise<FindingResp> {
  return request<FindingResp>(serverUrl, '/field/findings', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Bearer-Token': bearerToken,
    },
    body: JSON.stringify(body),
  });
}

function authedGet<T>(serverUrl: string, bearerToken: string, path: string): Promise<T> {
  return request<T>(serverUrl, path, {
    method: 'GET',
    headers: { 'X-Bearer-Token': bearerToken },
  });
}

function authedPost<T>(
  serverUrl: string,
  bearerToken: string,
  path: string,
  body?: unknown,
): Promise<T> {
  return request<T>(serverUrl, path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Bearer-Token': bearerToken,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

// ─── /field/me ───────────────────────────────────────────────────────────
// Polled. Returns current user state + active dispatch + last few visible
// broadcasts. Scope-filtering for broadcasts is enforced server-side.
export function getMe(serverUrl: string, bearerToken: string): Promise<MeResponse> {
  return authedGet<MeResponse>(serverUrl, bearerToken, '/field/me');
}

// ─── /field/dispatch/{id}/{ack,start,complete} ───────────────────────────
// One-off state transitions. The next /field/me poll picks up the change.
export function ackDispatch(
  serverUrl: string,
  bearerToken: string,
  dispatchId: number,
): Promise<DispatchActionResponse> {
  return authedPost<DispatchActionResponse>(
    serverUrl, bearerToken, `/field/dispatch/${dispatchId}/ack`, {},
  );
}

export function startDispatch(
  serverUrl: string,
  bearerToken: string,
  dispatchId: number,
): Promise<DispatchActionResponse> {
  return authedPost<DispatchActionResponse>(
    serverUrl, bearerToken, `/field/dispatch/${dispatchId}/start`, {},
  );
}

export function completeDispatch(
  serverUrl: string,
  bearerToken: string,
  dispatchId: number,
  notes?: string,
): Promise<DispatchActionResponse> {
  return authedPost<DispatchActionResponse>(
    serverUrl, bearerToken, `/field/dispatch/${dispatchId}/complete`,
    notes ? { notes } : {},
  );
}

// ─── /field/me/route ─────────────────────────────────────────────────────
// On-demand: fetch when the user has an active dispatch and wants to see
// the snap-to-trail route to it. Two waypoints (bee-line) if no trails.
export function getRoute(
  serverUrl: string,
  bearerToken: string,
  segmentId?: number,
): Promise<RouteResponse> {
  const path = segmentId == null ? '/field/me/route' : `/field/me/route?segment_id=${segmentId}`;
  return authedGet<RouteResponse>(
    serverUrl, bearerToken, path,
  );
}

// ─── /field/announcements?since={ts} ─────────────────────────────────────
// On-demand: app stores cursor_ts from the response and re-polls with
// ?since=cursor_ts for incremental delivery. Used by the "inbox" view.
export function getAnnouncements(
  serverUrl: string,
  bearerToken: string,
  sinceTs = 0,
): Promise<AnnouncementsResponse> {
  return authedGet<AnnouncementsResponse>(
    serverUrl, bearerToken, `/field/announcements?since=${sinceTs}`,
  );
}

// ─── /debug/dispatch ─────────────────────────────────────────────────────
// Dev-only: lets the app create dispatches without the agent. Mirrors
// dispatch_searcher side effects. Strip when the agent lands.
export type DebugDispatchBody = {
  segment_id: number;
  sweep_type?: SweepType;
  instruction?: string;
  reasoning?: string;
  target_user_id?: number;
};

export function postDebugDispatch(
  serverUrl: string,
  bearerToken: string,
  body: DebugDispatchBody,
): Promise<ActiveDispatch> {
  return authedPost<ActiveDispatch>(serverUrl, bearerToken, '/debug/dispatch', body);
}
