import type { FindingKind } from './missionState';

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
