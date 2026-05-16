import { useEffect, useState } from 'react';
import { AppState } from 'react-native';

import { request } from './api';
import { MISSION_STATE_POLL_INTERVAL_MS } from '../config';

export type HexProps = {
  feature_type: 'hex_cell';
  id: number;
  segment_id: number | null;
  center_elev_m: number | null;
  slope_deg: number | null;
  dominant_cover: string | null;
  has_trail: 0 | 1;
  has_road: 0 | 1;
  is_water: 0 | 1;
  is_building: 0 | 1;
  flag_danger: 0 | 1;
  flag_impassable: 0 | 1;
  flag_clue: 0 | 1;
  flag_poi: 0 | 1;
  flag_searched: 0 | 1;
  searched_by_user_id: number | null;
  searched_ts: number | null;
};

export type HexFeature = {
  type: 'Feature';
  geometry: { type: 'Polygon'; coordinates: number[][][] };
  properties: HexProps;
};

export type HexGrid = {
  type: 'FeatureCollection';
  features: HexFeature[];
};

export type SearcherFeature = {
  type: 'Feature';
  geometry: { type: 'Point'; coordinates: [number, number] };
  properties: {
    feature_type: 'searcher';
    user_id: number;
    callsign: string | null;
    status: string | null;
    role: 'searcher' | 'observer';
  };
};

export type TrackFeature = {
  type: 'Feature';
  geometry: { type: 'LineString'; coordinates: [number, number][] };
  properties: {
    feature_type: 'track';
    user_id: number;
  };
};

// Mirrors api/schemas.py FindingKind (CHECK constraint in
// migrations/002_spatial.sql:74). Keep in sync if the enum changes.
export type FindingKind =
  | 'clue'
  | 'subject_found'
  | 'subject_sighting'
  | 'hazard'
  | 'footprint'
  | 'discarded_item'
  | 'note'
  | 'other';

export type FindingFeature = {
  type: 'Feature';
  geometry: { type: 'Point'; coordinates: [number, number] };
  properties: {
    feature_type: 'finding';
    kind: FindingKind;
    description: string | null;
    confidence: number;
    ts: number;
  };
};

// Mirrors api/schemas.py + segments.status CHECK in migrations/002_spatial.sql:48.
export type SegmentStatus =
  | 'unassigned'
  | 'assigned'
  | 'in_progress'
  | 'swept'
  | 'cleared';

// segments.sweep_type CHECK in migrations/002_spatial.sql:50.
export type SweepType = 'hasty' | 'efficient' | 'thorough';

export type SegmentFeature = {
  type: 'Feature';
  geometry: { type: 'Polygon'; coordinates: number[][][] };
  properties: {
    feature_type: 'segment';
    id: number;
    name: string;
    poa: number;        // [0, 1]
    pod: number;        // [0, 1]
    pos: number;        // [0, 1], typically poa * pod
    status: SegmentStatus;
    sweep_type: SweepType | null;
    assigned_user_id: number | null;
  };
};

// hazards.kind CHECK in migrations/002_spatial.sql:97.
export type HazardKind =
  | 'cliff'
  | 'water'
  | 'weather'
  | 'no_comms_zone'
  | 'wildlife'
  | 'other';

// hazards.severity CHECK in migrations/002_spatial.sql:98.
export type HazardSeverity = 'info' | 'caution' | 'critical';

export type HazardFeature = {
  type: 'Feature';
  geometry: { type: 'Polygon'; coordinates: number[][][] };
  properties: {
    feature_type: 'hazard';
    id: number;
    kind: HazardKind;
    severity: HazardSeverity;
    description: string | null;
  };
};

// OSM features mix LineString and Polygon. The renderer narrows per call site.
export type OSMKind = 'building' | 'road' | 'trail' | 'water';

export type OSMFeature = {
  type: 'Feature';
  geometry:
    | { type: 'LineString'; coordinates: [number, number][] }
    | { type: 'Polygon'; coordinates: number[][][] };
  properties: {
    feature_type: 'osm_feature';
    kind: OSMKind;
    name: string | null;
  };
};

// TODO: pull in @types/geojson and tighten `geometry` for the fallback variant
// (currently `any` for forward-compat with feature_types we don't render yet:
// segment, hex_cell, hazard, osm_feature).
export type AnyFeature =
  | SearcherFeature
  | TrackFeature
  | FindingFeature
  | SegmentFeature
  | HazardFeature
  | OSMFeature
  | {
      type: 'Feature';
      geometry: any;
      properties: { feature_type: string; [k: string]: any };
    };

export type MissionState = {
  type: 'FeatureCollection';
  features: AnyFeature[];
};

export async function fetchHexGrid(
  serverUrl: string,
  bearerToken: string,
  missionId: number,
): Promise<HexGrid> {
  return request<HexGrid>(serverUrl, `/mission/${missionId}/hex_grid.geojson`, {
    method: 'GET',
    headers: { 'X-Bearer-Token': bearerToken },
  });
}

export async function fetchMissionState(
  serverUrl: string,
  bearerToken: string,
  missionId: number,
): Promise<MissionState> {
  return request<MissionState>(
    serverUrl,
    `/mission/state.geojson?mission_id=${missionId}`,
    {
      method: 'GET',
      headers: { 'X-Bearer-Token': bearerToken },
    },
  );
}

export function useMissionState(
  serverUrl: string | null,
  bearerToken: string | null,
  missionId: number | null,
): MissionState | null {
  const [state, setState] = useState<MissionState | null>(null);

  useEffect(() => {
    if (!serverUrl || !bearerToken || !missionId) return;
    let cancelled = false;
    let inflight = false;
    let intervalId: ReturnType<typeof setInterval> | null = null;

    const tick = async () => {
      if (cancelled || inflight) return;
      inflight = true;
      try {
        const fresh = await fetchMissionState(serverUrl, bearerToken, missionId);
        if (!cancelled) setState(fresh);
      } catch (e) {
        console.warn('[mission_state] poll failed', e);
      } finally {
        inflight = false;
      }
    };

    const start = () => {
      if (intervalId) return;
      void tick();
      intervalId = setInterval(tick, MISSION_STATE_POLL_INTERVAL_MS);
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
  }, [serverUrl, bearerToken, missionId]);

  return state;
}
