import { router } from 'expo-router';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Pressable, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import MapView, { Marker, Polygon, Polyline, type Region } from 'react-native-maps';

import FindingSheet from './components/FindingSheet';
import { startTracking, stopTracking } from './lib/location';
import {
  fetchHexGrid,
  useMissionState,
  type FindingFeature,
  type FindingKind,
  type HazardFeature,
  type HazardSeverity,
  type HexGrid,
  type HexProps,
  type OSMFeature,
  type SearcherFeature,
  type SegmentFeature,
  type SegmentStatus,
  type TrackFeature,
} from './lib/missionState';
import { Keys, clear, getJSON, CurrentMission } from './lib/storage';

export default function MissionView() {
  const [mission, setMission] = useState<CurrentMission | null>(null);
  const [grid, setGrid] = useState<HexGrid | null>(null);
  const [initialRegion, setInitialRegion] = useState<Region | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);

  const loadHexGrid = useCallback(async () => {
    if (!mission) return;
    try {
      const g = await fetchHexGrid(mission.server_url, mission.bearer_token, mission.mission_id);
      setGrid(g);
      // Only set initialRegion the first time so a refetch doesn't yank the
      // camera back to the bbox after the user has panned around.
      setInitialRegion((prev) => prev ?? regionFromGrid(g));
    } catch (e) {
      console.warn('[hex_grid] fetch failed', e);
    }
  }, [mission]);

  useEffect(() => {
    let cancelled = false;

    void (async () => {
      const m = await getJSON<CurrentMission>(Keys.CurrentMission);
      if (cancelled) return;
      if (!m) {
        router.replace('/');
        return;
      }
      setMission(m);

      try {
        await startTracking(m.server_url, m.bearer_token);
      } catch (e) {
        const msg = e instanceof Error ? e.message : 'Could not start GPS';
        Alert.alert('Location error', msg, [
          {
            text: 'OK',
            onPress: () => {
              void leaveMission();
            },
          },
        ]);
      }
    })();

    return () => {
      cancelled = true;
      void stopTracking();
    };
  }, []);

  useEffect(() => {
    if (!mission?.mission_id) return;
    void loadHexGrid();
  }, [mission?.mission_id, loadHexGrid]);

  const missionState = useMissionState(
    mission?.server_url ?? null,
    mission?.bearer_token ?? null,
    mission?.mission_id ?? null,
  );

  // FIXME: server's searcher query in api/db/geojson.py joins users to pings
  // by mission_id without filtering on users.current_mission_id, so a user who
  // joined this mission and later joined another will still appear here.
  // Remove the cross-mission ghost when that JOIN is tightened.
  const otherSearchers = useMemo<SearcherFeature[]>(() => {
    if (!missionState || !mission) return [];
    return missionState.features.filter(
      (f): f is SearcherFeature =>
        f.properties.feature_type === 'searcher' &&
        f.properties.role === 'searcher' &&
        f.properties.user_id !== mission.user_id,
    );
  }, [missionState, mission?.user_id]);

  const tracks = useMemo<TrackFeature[]>(() => {
    if (!missionState) return [];
    return missionState.features.filter(
      (f): f is TrackFeature => f.properties.feature_type === 'track',
    );
  }, [missionState]);

  const findings = useMemo<FindingFeature[]>(() => {
    if (!missionState) return [];
    return missionState.features.filter(
      (f): f is FindingFeature => f.properties.feature_type === 'finding',
    );
  }, [missionState]);

  const segments = useMemo<SegmentFeature[]>(() => {
    if (!missionState) return [];
    return missionState.features.filter(
      (f): f is SegmentFeature => f.properties.feature_type === 'segment',
    );
  }, [missionState]);

  const hazards = useMemo<HazardFeature[]>(() => {
    if (!missionState) return [];
    return missionState.features.filter(
      (f): f is HazardFeature => f.properties.feature_type === 'hazard',
    );
  }, [missionState]);

  // Live hex flag overrides from the 5s state poll. The hex grid geometry is
  // cached (one fetch per session via /hex_grid.geojson); newly-flagged hexes
  // — including newly-searched cells — ride the state poll and are merged
  // into the rendered grid here. Without this, coverage tints would only
  // appear after a finding submit (the one path that refetches the grid).
  const liveHexFlags = useMemo(() => {
    const m = new Map<number, Partial<HexProps>>();
    if (!missionState) return m;
    for (const f of missionState.features) {
      if (f.properties.feature_type === 'hex_cell') {
        const p = f.properties as unknown as HexProps;
        m.set(p.id, {
          flag_danger: p.flag_danger,
          flag_impassable: p.flag_impassable,
          flag_clue: p.flag_clue,
          flag_poi: p.flag_poi,
          flag_searched: p.flag_searched,
          is_water: p.is_water,
          is_building: p.is_building,
        });
      }
    }
    return m;
  }, [missionState]);

  // Apple Maps base layer already paints buildings/roads/water, so we only
  // pull trails out of osm_features — Apple Maps under-renders backcountry
  // trails and they're high-value SAR context.
  const osmTrails = useMemo<OSMFeature[]>(() => {
    if (!missionState) return [];
    return missionState.features.filter(
      (f): f is OSMFeature =>
        f.properties.feature_type === 'osm_feature' &&
        f.properties.kind === 'trail' &&
        f.geometry.type === 'LineString',
    );
  }, [missionState]);

  // user_id → callsign lookup, sourced from the searcher features in the same
  // state poll. Lets segment-assignee chips render the assignee's initial
  // without a server change.
  const callsignByUserId = useMemo<Record<number, string>>(() => {
    const map: Record<number, string> = {};
    if (!missionState) return map;
    for (const f of missionState.features) {
      if (f.properties.feature_type === 'searcher' && f.properties.callsign) {
        map[f.properties.user_id] = f.properties.callsign;
      }
    }
    return map;
  }, [missionState]);

  async function leaveMission() {
    await stopTracking();
    await clear(Keys.CurrentMission);
    router.replace('/');
  }

  const polygons = useMemo(() => {
    if (!grid) return null;
    return grid.features.map((f) => {
      const overrides = liveHexFlags.get(f.properties.id);
      const effective = overrides ? { ...f.properties, ...overrides } : f.properties;
      return (
        <Polygon
          key={f.properties.id}
          // Outer ring only — hex cells are single-ring. If reused for
          // osm_features (parks, lakes) iterate rings and pass holes={...}.
          coordinates={f.geometry.coordinates[0].map(([lon, lat]) => ({
            latitude: lat,
            longitude: lon,
          }))}
          strokeColor="rgba(0,0,0,0.28)"
          strokeWidth={0.5}
          fillColor={fillFor(effective)}
        />
      );
    });
  }, [grid, liveHexFlags]);

  const trackLines = useMemo(() => {
    if (tracks.length === 0) return null;
    return tracks.map((t) => {
      const isSelf = t.properties.user_id === mission?.user_id;
      return (
        <Polyline
          key={`track-${t.properties.user_id}`}
          coordinates={t.geometry.coordinates.map(([lon, lat]) => ({
            latitude: lat,
            longitude: lon,
          }))}
          strokeColor={isSelf ? SELF_TRACK_COLOR : colorForUser(t.properties.user_id)}
          strokeWidth={3}
        />
      );
    });
  }, [tracks, mission?.user_id]);

  const searcherMarkers = useMemo(() => {
    if (otherSearchers.length === 0) return null;
    return otherSearchers.map((f) => {
      const [lon, lat] = f.geometry.coordinates;
      const color = colorForUser(f.properties.user_id);
      const label = f.properties.callsign ?? `#${f.properties.user_id}`;
      return (
        <Marker
          key={`searcher-${f.properties.user_id}`}
          coordinate={{ latitude: lat, longitude: lon }}
          anchor={{ x: 0.5, y: 0.5 }}
        >
          <View style={s.searcherDotWrap}>
            <View style={[s.searcherDot, { backgroundColor: color }]} />
            <View style={[s.searcherLabel, { backgroundColor: color }]}>
              <Text style={s.searcherLabelText} numberOfLines={1}>
                {label}
              </Text>
            </View>
          </View>
        </Marker>
      );
    });
  }, [otherSearchers]);

  const findingPins = useMemo(() => {
    if (findings.length === 0) return null;
    return findings.map((f, i) => {
      const [lon, lat] = f.geometry.coordinates;
      const color = FINDING_COLORS[f.properties.kind] ?? FINDING_COLORS.other;
      return (
        <Marker
          // Server doesn't currently include finding.id in properties — fall
          // back to ts+index. Findings can't be edited/deleted from the UI so
          // identity stability across polls is good-enough.
          key={`finding-${f.properties.ts}-${i}`}
          coordinate={{ latitude: lat, longitude: lon }}
          anchor={{ x: 0.5, y: 1 }}
        >
          <View style={[s.findingPin, { backgroundColor: color }]}>
            <Text style={s.findingPinGlyph}>{glyphFor(f.properties.kind)}</Text>
          </View>
        </Marker>
      );
    });
  }, [findings]);

  const hazardPolygons = useMemo(() => {
    if (hazards.length === 0) return null;
    return hazards.map((h) => {
      const alpha = HAZARD_SEVERITY_ALPHA[h.properties.severity];
      return (
        <Polygon
          key={`hazard-${h.properties.id}`}
          coordinates={h.geometry.coordinates[0].map(([lon, lat]) => ({
            latitude: lat,
            longitude: lon,
          }))}
          fillColor={`rgba(214,54,47,${alpha})`}
          strokeColor="rgba(214,54,47,0.65)"
          strokeWidth={1.5}
          // Dashed stroke approximates the .cell-untrav hatch from the mockup
          // (react-native-maps can't render SVG pattern fills).
          lineDashPattern={[6, 4]}
        />
      );
    });
  }, [hazards]);

  const trailLines = useMemo(() => {
    if (osmTrails.length === 0) return null;
    return osmTrails.map((t, i) => {
      // LineString narrowed by the filter above.
      const coords = (t.geometry as { type: 'LineString'; coordinates: [number, number][] })
        .coordinates;
      return (
        <Polyline
          key={`trail-${i}-${t.properties.name ?? 'unnamed'}`}
          coordinates={coords.map(([lon, lat]) => ({ latitude: lat, longitude: lon }))}
          strokeColor="rgba(139,111,71,0.55)"
          strokeWidth={2}
          lineDashPattern={[4, 3]}
        />
      );
    });
  }, [osmTrails]);

  const segmentOutlines = useMemo(() => {
    if (segments.length === 0) return null;
    return segments.map((seg) => (
      <Polygon
        key={`segment-outline-${seg.properties.id}`}
        coordinates={seg.geometry.coordinates[0].map(([lon, lat]) => ({
          latitude: lat,
          longitude: lon,
        }))}
        strokeColor={SEGMENT_STATUS_COLOR[seg.properties.status]}
        strokeWidth={1.5}
        // Defensive: "transparent" works on most react-native-maps versions but
        // some older builds fall back to translucent red for non-rgba inputs.
        fillColor="rgba(0,0,0,0)"
      />
    ));
  }, [segments]);

  const segmentLabels = useMemo(() => {
    if (segments.length === 0) return null;
    return segments.map((seg) => {
      const c = polygonCentroid(seg.geometry.coordinates[0]);
      const strokeColor = SEGMENT_STATUS_COLOR[seg.properties.status];
      const assigneeUserId = seg.properties.assigned_user_id;
      const assigneeColor =
        assigneeUserId != null ? colorForUser(assigneeUserId) : null;
      const assigneeInitial =
        assigneeUserId != null
          ? (callsignByUserId[assigneeUserId]?.[0] ?? '?').toUpperCase()
          : null;
      return (
        <Marker
          key={`segment-label-${seg.properties.id}`}
          coordinate={c}
          // y > 0.5 pulls the label down off the centroid so a searcher dot
          // that lands at the centroid peeks above the pill.
          anchor={{ x: 0.5, y: 0.65 }}
        >
          <View style={[s.segmentLabel, { borderColor: strokeColor }]}>
            <View style={s.segmentLabelRow}>
              <Text style={s.segmentLabelName}>{seg.properties.name}</Text>
              {assigneeColor && (
                <View style={[s.segmentAssigneeChip, { backgroundColor: assigneeColor }]}>
                  <Text style={s.segmentAssigneeChipText}>{assigneeInitial}</Text>
                </View>
              )}
            </View>
            <Text style={s.segmentLabelStats}>
              {`POA ${pct(seg.properties.poa)} · POD ${pct(seg.properties.pod)}`}
            </Text>
          </View>
        </Marker>
      );
    });
  }, [segments, callsignByUserId]);

  return (
    <View style={s.root}>
      <MapView
        style={StyleSheet.absoluteFill}
        showsUserLocation
        showsCompass={false}
        initialRegion={initialRegion ?? undefined}
      >
        {polygons}
        {trailLines}
        {hazardPolygons}
        {segmentOutlines}
        {segmentLabels}
        {trackLines}
        {searcherMarkers}
        {findingPins}
      </MapView>

      <SafeAreaView style={s.topInset} pointerEvents="box-none">
        <View style={s.topInsetRow}>
          <View style={s.pill}>
            <Pressable style={s.xButton} onPress={leaveMission} hitSlop={8}>
              <Text style={s.xText}>×</Text>
            </Pressable>

            <View style={s.titleBlock}>
              <Text style={s.title} numberOfLines={1}>
                {mission?.mission_name ?? 'Mission'}
              </Text>
              <View style={s.subRow}>
                <View style={s.statusDot} />
                <Text style={s.sub}>Live</Text>
              </View>
            </View>

            <Text style={s.callsign} numberOfLines={1}>
              {mission?.callsign ?? '—'}
            </Text>
          </View>

          <View style={s.mapctrl}>
            <Pressable
              style={({ pressed }) => [s.mapctrlBtn, pressed && s.mapctrlBtnPressed]}
              onPress={() => setSheetOpen(true)}
              hitSlop={4}
              accessibilityLabel="Log finding"
            >
              <Text style={s.mapctrlGlyph}>🚩</Text>
            </Pressable>
          </View>
        </View>
      </SafeAreaView>

      <Pressable
        style={({ pressed }) => [s.fab, pressed && s.fabPressed]}
        onPress={() => Alert.alert('Chat', 'Coming soon')}
      >
        <Text style={s.fabIcon}>💬</Text>
      </Pressable>

      <MapLegend />

      {mission && (
        <FindingSheet
          visible={sheetOpen}
          serverUrl={mission.server_url}
          bearerToken={mission.bearer_token}
          onClose={() => setSheetOpen(false)}
          onSuccess={() => {
            // Server toggles flag_clue on the containing hex; refetch the grid
            // so the cell tint updates without waiting for a remount.
            void loadHexGrid();
          }}
          onFatalAuthError={() => {
            void leaveMission();
          }}
        />
      )}
    </View>
  );
}

function fillFor(p: HexProps): string {
  // Priority order: safety flags > human-raised signals > coverage > base
  // terrain. flag_searched (mockup blue) sits above terrain because a phone
  // walking through a "water" cell means either OSM is wrong or there's a
  // bridge — the foot is fresher info than the seed-time classification.
  if (p.flag_impassable) return 'rgba(40,40,40,0.22)';
  if (p.flag_danger) return 'rgba(214,54,47,0.20)';
  if (p.flag_clue) return 'rgba(240,200,60,0.26)';
  if (p.flag_poi) return 'rgba(140,90,200,0.22)';
  if (p.flag_searched) return 'rgba(43,108,246,0.18)';  // .cell-searched, wireframes-v2.css:225
  if (p.is_water) return 'rgba(80,150,220,0.20)';
  if (p.is_building) return 'rgba(120,120,120,0.22)';
  return 'rgba(0,0,0,0.04)';
}

function regionFromGrid(g: HexGrid): Region | null {
  let minLat = Infinity;
  let maxLat = -Infinity;
  let minLon = Infinity;
  let maxLon = -Infinity;
  for (const f of g.features) {
    for (const [lon, lat] of f.geometry.coordinates[0]) {
      if (lat < minLat) minLat = lat;
      if (lat > maxLat) maxLat = lat;
      if (lon < minLon) minLon = lon;
      if (lon > maxLon) maxLon = lon;
    }
  }
  if (!isFinite(minLat) || !isFinite(minLon)) return null;
  const latDelta = Math.max((maxLat - minLat) * 1.2, 0.002);
  const lonDelta = Math.max((maxLon - minLon) * 1.2, 0.002);
  return {
    latitude: (minLat + maxLat) / 2,
    longitude: (minLon + maxLon) / 2,
    latitudeDelta: latDelta,
    longitudeDelta: lonDelta,
  };
}

const ACCENT = '#d6362f';
const SELF_TRACK_COLOR = '#5a6cf2';
// 8-entry palette — users 1..8 each get a distinct color. User 9 collides with
// user 1, user 10 with user 2, etc. Not a bug; expand the palette when the
// demo grows past 8 concurrent searchers.
const SEARCHER_COLORS = [
  '#2f80ed',
  '#27ae60',
  '#e67e22',
  '#9b59b6',
  '#16a085',
  '#c0392b',
  '#d4a017',
  '#1abc9c',
];
function colorForUser(userId: number): string {
  return SEARCHER_COLORS[userId % SEARCHER_COLORS.length];
}

// Keep in sync with FindingSheet.tsx KIND_COLOR. Different concerns (one is
// pill UI, the other is dropped-pin fill) so we don't share a constant yet —
// will if we end up with a third surface using these colors.
const FINDING_COLORS: Record<FindingKind, string> = {
  clue: '#d4a017',
  footprint: '#8b6f47',
  discarded_item: '#e67e22',
  subject_sighting: '#c0392b',
  subject_found: '#27ae60',
  hazard: '#d6362f',
  note: '#5a6cf2',
  other: '#7f8c8d',
};

// Approximates the .cell-untrav darkening from the mockup. We can't pattern-
// fill in react-native-maps, so severity is encoded as fill opacity instead.
const HAZARD_SEVERITY_ALPHA: Record<HazardSeverity, number> = {
  info: 0.10,
  caution: 0.18,
  critical: 0.28,
};

const SEGMENT_STATUS_COLOR: Record<SegmentStatus, string> = {
  unassigned: 'rgba(0,0,0,0.45)',
  assigned: '#2f80ed',
  in_progress: '#d4a017',
  swept: '#7dcea0',
  cleared: '#27ae60',
};

function pct(v: number): string {
  return `${Math.round(v * 100)}%`;
}

function polygonCentroid(ring: number[][]): { latitude: number; longitude: number } {
  // Vertex-average over the outer ring with the closing duplicate removed.
  // Adequate for our roughly-convex Voronoi-like segments. Switch to the
  // signed-area centroid formula if a non-convex segment ever appears.
  const pts = ring.slice(0, -1);
  let sLat = 0;
  let sLon = 0;
  for (const [lon, lat] of pts) {
    sLat += lat;
    sLon += lon;
  }
  return { latitude: sLat / pts.length, longitude: sLon / pts.length };
}

function MapLegend() {
  return (
    <View style={s.legend} pointerEvents="none">
      <Text style={s.legendEyebrow}>LEGEND</Text>
      <LegendRow
        swatch={<View style={[s.legendDot, { backgroundColor: '#007AFF' }]} />}
        label="You"
      />
      <LegendRow
        swatch={<View style={[s.legendDot, { backgroundColor: SEARCHER_COLORS[2] }]} />}
        label="Teammate"
      />
      <LegendRow
        swatch={<View style={[s.legendLine, { backgroundColor: SEARCHER_COLORS[2] }]} />}
        label="Track"
      />
      <LegendRow swatch={<View style={s.legendZone} />} label="Zone" />
      <LegendRow swatch={<View style={s.legendSearched} />} label="Searched" />
      <LegendRow
        swatch={
          <View style={[s.legendFinding, { backgroundColor: FINDING_COLORS.clue }]}>
            <Text style={s.legendFindingGlyph}>?</Text>
          </View>
        }
        label="Finding"
      />
      <LegendRow swatch={<View style={s.legendHazard} />} label="Hazard" />
      <LegendRow
        swatch={<View style={[s.legendLine, { backgroundColor: 'rgba(139,111,71,0.55)' }]} />}
        label="Trail"
      />
      <LegendRow
        swatch={<View style={[s.legendLine, { backgroundColor: 'rgba(0,0,0,0.45)' }]} />}
        label="Route"
      />
    </View>
  );
}

function LegendRow({ swatch, label }: { swatch: React.ReactNode; label: string }) {
  return (
    <View style={s.legendRow}>
      <View style={s.legendSwatchCell}>{swatch}</View>
      <Text style={s.legendLabel}>{label}</Text>
    </View>
  );
}

function glyphFor(kind: FindingKind): string {
  switch (kind) {
    case 'clue': return '?';
    case 'footprint': return '👣';
    case 'discarded_item': return '!';
    case 'subject_sighting': return '👁';
    case 'subject_found': return '✓';
    case 'hazard': return '⚠';
    case 'note': return '•';
    case 'other': return '·';
  }
}

const s = StyleSheet.create({
  root: { flex: 1, backgroundColor: '#f0ece1' },

  topInset: { position: 'absolute', top: 0, left: 0, right: 0 },
  topInsetRow: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    paddingHorizontal: 12,
    paddingTop: 10,
    gap: 10,
  },
  pill: {
    flex: 1,
    height: 50,
    borderRadius: 16,
    backgroundColor: 'rgba(255,255,255,0.92)',
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: 'rgba(255,255,255,0.6)',
    shadowColor: '#000',
    shadowOpacity: 0.08,
    shadowRadius: 18,
    shadowOffset: { width: 0, height: 6 },
    flexDirection: 'row',
    alignItems: 'center',
    paddingLeft: 6,
    paddingRight: 14,
  },
  xButton: {
    width: 32,
    height: 32,
    marginLeft: 6,
    borderRadius: 16,
    backgroundColor: 'rgba(120,120,128,0.16)',
    alignItems: 'center',
    justifyContent: 'center',
  },
  xText: { fontSize: 20, color: '#3c3c43', lineHeight: 22, fontWeight: '500' },
  titleBlock: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  title: { fontSize: 15, fontWeight: '600', color: '#0b0b0c' },
  subRow: { flexDirection: 'row', alignItems: 'center', gap: 4, marginTop: 2 },
  statusDot: { width: 5, height: 5, borderRadius: 2.5, backgroundColor: ACCENT },
  sub: { fontSize: 11, color: '#6b6b73' },
  callsign: { fontSize: 13, color: '#6b6b73', fontVariant: ['tabular-nums'] },

  fab: {
    position: 'absolute',
    bottom: 36,
    left: '50%',
    transform: [{ translateX: -28 }],
    width: 56,
    height: 56,
    borderRadius: 28,
    backgroundColor: 'rgba(255,255,255,0.92)',
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: 'rgba(255,255,255,0.6)',
    shadowColor: '#000',
    shadowOpacity: 0.12,
    shadowRadius: 24,
    shadowOffset: { width: 0, height: 8 },
    alignItems: 'center',
    justifyContent: 'center',
  },
  fabPressed: { opacity: 0.75 },
  fabIcon: { fontSize: 22 },

  searcherDotWrap: { alignItems: 'center' },
  searcherDot: {
    width: 14,
    height: 14,
    borderRadius: 7,
    borderWidth: 2,
    borderColor: '#fff',
    shadowColor: '#000',
    shadowOpacity: 0.2,
    shadowRadius: 2,
    shadowOffset: { width: 0, height: 1 },
  },
  searcherLabel: {
    marginTop: 2,
    paddingHorizontal: 6,
    paddingVertical: 1,
    borderRadius: 6,
    maxWidth: 80,
  },
  searcherLabelText: {
    color: '#fff',
    fontSize: 10,
    fontWeight: '600',
    fontVariant: ['tabular-nums'],
  },

  // .mapctrl from wireframes-v2.css:254 — top-right floating control stack.
  // Single button here; future controls (compass, layers) stack vertically.
  mapctrl: {
    width: 44,
    borderRadius: 12,
    backgroundColor: 'rgba(255,255,255,0.92)',
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: 'rgba(255,255,255,0.6)',
    shadowColor: '#000',
    shadowOpacity: 0.08,
    shadowRadius: 18,
    shadowOffset: { width: 0, height: 6 },
    overflow: 'hidden',
  },
  mapctrlBtn: {
    width: 44,
    height: 44,
    alignItems: 'center',
    justifyContent: 'center',
  },
  mapctrlBtnPressed: { opacity: 0.65 },
  mapctrlGlyph: { fontSize: 18 },

  findingPin: {
    minWidth: 22,
    height: 22,
    paddingHorizontal: 6,
    borderRadius: 11,
    borderWidth: 2,
    borderColor: '#fff',
    alignItems: 'center',
    justifyContent: 'center',
    shadowColor: '#000',
    shadowOpacity: 0.25,
    shadowRadius: 3,
    shadowOffset: { width: 0, height: 1 },
  },
  findingPinGlyph: {
    color: '#fff',
    fontSize: 12,
    fontWeight: '700',
    lineHeight: 14,
  },

  // Translucent pill (no native backdrop-filter in RN — pure rgba white). Lower
  // alpha than the top pill so flagged hex cells underneath bleed through.
  segmentLabel: {
    backgroundColor: 'rgba(255,255,255,0.75)',
    borderWidth: StyleSheet.hairlineWidth,
    paddingHorizontal: 8,
    paddingVertical: 4,
    borderRadius: 8,
    alignItems: 'center',
    shadowColor: '#000',
    shadowOpacity: 0.10,
    shadowRadius: 6,
    shadowOffset: { width: 0, height: 2 },
  },
  segmentLabelRow: { flexDirection: 'row', alignItems: 'center', gap: 4 },
  segmentLabelName: { fontSize: 13, fontWeight: '600', color: '#0b0b0c' },
  segmentLabelStats: {
    fontSize: 10,
    fontWeight: '600',
    fontVariant: ['tabular-nums'],
    marginTop: 1,
    color: '#3c3c43',  // neutral, decoupled from status (one signal per channel)
  },
  segmentAssigneeChip: {
    width: 12,
    height: 12,
    borderRadius: 6,
    alignItems: 'center',
    justifyContent: 'center',
  },
  segmentAssigneeChipText: {
    color: '#fff',
    fontSize: 8,
    fontWeight: '700',
    lineHeight: 10,
  },

  // Floating mini-legend. Bottom-left, above the chat FAB visually. Decorative
  // only — pointerEvents="none" on the parent so map taps pass through. Listed
  // entries include layers we haven't shipped yet (Hazard polygon fill, Route
  // line) so the key already reads correctly when those land.
  legend: {
    position: 'absolute',
    bottom: 100,
    left: 12,
    width: 130,
    paddingHorizontal: 10,
    paddingTop: 8,
    paddingBottom: 8,
    borderRadius: 12,
    backgroundColor: 'rgba(255,255,255,0.92)',
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: 'rgba(255,255,255,0.6)',
    shadowColor: '#000',
    shadowOpacity: 0.08,
    shadowRadius: 18,
    shadowOffset: { width: 0, height: 6 },
  },
  legendEyebrow: {
    fontSize: 9,
    fontWeight: '700',
    letterSpacing: 0.8,
    color: '#6b6b73',
    marginBottom: 6,
  },
  legendRow: {
    flexDirection: 'row',
    alignItems: 'center',
    marginVertical: 2,
  },
  legendSwatchCell: {
    width: 18,
    alignItems: 'center',
    justifyContent: 'center',
    marginRight: 8,
  },
  legendLabel: { fontSize: 11, color: '#0b0b0c' },
  legendDot: {
    width: 9,
    height: 9,
    borderRadius: 4.5,
    borderWidth: 1.5,
    borderColor: '#fff',
  },
  legendLine: {
    width: 14,
    height: 2.5,
    borderRadius: 1,
  },
  legendZone: {
    width: 14,
    height: 10,
    borderRadius: 2,
    borderWidth: 1.5,
    borderColor: 'rgba(0,0,0,0.45)',
    backgroundColor: 'rgba(0,0,0,0)',
  },
  legendSearched: {
    width: 14,
    height: 10,
    borderRadius: 2,
    borderWidth: 1.5,
    borderColor: 'rgba(43,108,246,0.40)',
    backgroundColor: 'rgba(43,108,246,0.18)',
  },
  legendFinding: {
    width: 12,
    height: 12,
    borderRadius: 6,
    borderWidth: 1.5,
    borderColor: '#fff',
    alignItems: 'center',
    justifyContent: 'center',
  },
  legendFindingGlyph: {
    color: '#fff',
    fontSize: 8,
    fontWeight: '700',
    lineHeight: 9,
  },
  legendHazard: {
    width: 14,
    height: 10,
    borderRadius: 2,
    borderWidth: 1.5,
    borderColor: '#d6362f',
    backgroundColor: 'rgba(214,54,47,0.20)',
  },
});
