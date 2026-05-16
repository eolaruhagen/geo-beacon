import { router } from 'expo-router';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Pressable, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context';
import MapView, { Callout, Marker, Polygon, Polyline, UrlTile, type Region } from 'react-native-maps';

import FindingCalloutContent from './components/FindingCalloutContent';
import FindingSheet from './components/FindingSheet';
import MissionHud from './components/MissionHud';
import {
  ackDispatch,
  completeDispatch,
  postDebugDispatch,
  startDispatch,
  type RouteWaypoint,
} from './lib/api';
import { startTracking, stopTracking } from './lib/location';
import { useMe } from './lib/useMe';
import { useRoute } from './lib/useRoute';
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
  // Waypoints forwarded up by MissionHud — drawn as a Polyline on the map.
  const [routeWaypoints, setRouteWaypoints] = useState<RouteWaypoint[] | null>(null);
  // Tracked via onRegionChangeComplete — drives segment-label gating below.
  // Starts null and is hydrated from initialRegion once the grid loads, so
  // labels can show on first paint without waiting for the user to pan.
  const [region, setRegion] = useState<Region | null>(null);

  // ─── HUD state (lifted from MissionHud so the top-right button stack and
  //     the panels share one source of truth). ───────────────────────────
  const [broadcastForceOpen, setBroadcastForceOpen] = useState(false);
  const [ordersForceOpen, setOrdersForceOpen] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [debugBusy, setDebugBusy] = useState(false);

  const insets = useSafeAreaInsets();

  // Personal state poll (/field/me). Driver for the dispatch + broadcast UI.
  const { data: me, refresh: refreshMe } = useMe(
    mission?.server_url ?? null,
    mission?.bearer_token ?? null,
  );

  // Snap-to-trail route. Fetched when the active dispatch is acked or
  // in_progress so the user sees the suggested path to the segment entry.
  const { data: routeData, fetch: fetchRoute } = useRoute(
    mission?.server_url ?? null,
    mission?.bearer_token ?? null,
  );

  // Auto-trigger /field/me/route on dispatch-status transitions.
  const lastRouteKeyRef = useRef<string | null>(null);
  useEffect(() => {
    const ad = me?.active_dispatch ?? null;
    const wantsRoute =
      ad != null &&
      ad.segment_id != null &&
      (ad.status === 'acked' || ad.status === 'in_progress');
    const key = wantsRoute ? `${ad!.id}:${ad!.status}` : null;
    if (key === lastRouteKeyRef.current) return;
    lastRouteKeyRef.current = key;
    if (wantsRoute) {
      void fetchRoute(ad!.segment_id!);
    } else {
      void fetchRoute(null);
    }
  }, [me?.active_dispatch, fetchRoute]);

  // Forward route waypoints to the map (rendered as a Polyline).
  useEffect(() => {
    setRouteWaypoints(routeData?.waypoints ?? null);
  }, [routeData]);

  const loadHexGrid = useCallback(async () => {
    if (!mission) return;
    try {
      const g = await fetchHexGrid(mission.server_url, mission.bearer_token, mission.mission_id);
      setGrid(g);
      // Only set initialRegion the first time so a refetch doesn't yank the
      // camera back to the bbox after the user has panned around.
      const r = regionFromGrid(g);
      setInitialRegion((prev) => prev ?? r);
      // Seed `region` from the same source so label-gating logic has something
      // to compare against before the user first pans/zooms (onRegionChange
      // doesn't fire on initial mount in every RN-maps version).
      setRegion((prev) => prev ?? r);
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

  // Wrapper: run a dispatch lifecycle action, then refresh /field/me so the
  // CurrentOrdersCard updates without waiting for the next 5s poll tick.
  const runDispatchAction = useCallback(
    async (action: () => Promise<unknown>) => {
      if (!mission) return;
      setActionBusy(true);
      try {
        await action();
        await refreshMe();
      } catch (e) {
        const msg = e instanceof Error ? e.message : 'Action failed';
        Alert.alert('Dispatch error', msg);
      } finally {
        setActionBusy(false);
      }
    },
    [mission, refreshMe],
  );

  const onAck = useCallback(() => {
    const ad = me?.active_dispatch;
    if (!ad || !mission) return;
    void runDispatchAction(() => ackDispatch(mission.server_url, mission.bearer_token, ad.id));
  }, [me?.active_dispatch, mission, runDispatchAction]);

  const onStart = useCallback(() => {
    const ad = me?.active_dispatch;
    if (!ad || !mission) return;
    void runDispatchAction(() => startDispatch(mission.server_url, mission.bearer_token, ad.id));
  }, [me?.active_dispatch, mission, runDispatchAction]);

  const onComplete = useCallback(
    (notes?: string) => {
      const ad = me?.active_dispatch;
      if (!ad || !mission) return;
      void runDispatchAction(() =>
        completeDispatch(mission.server_url, mission.bearer_token, ad.id, notes),
      );
    },
    [me?.active_dispatch, mission, runDispatchAction],
  );

  // DEV: trigger /debug/dispatch against the highest-POA *unassigned* segment.
  // This is what the real agent will do too (modulo POD/POS weighting), so
  // it's also a sanity check on the POA math. Defined inside the component
  // so it can read `segments` from the missionState closure below.
  // (Function body lives after the segments memo — see onDebugDispatch.)

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
          // Attribution: who marked this cell searched. Drives the per-user
          // coverage tint in fillFor() below.
          searched_by_user_id: p.searched_by_user_id,
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

  // DEV: dispatch the caller to the highest-POA unassigned segment in the
  // current mission. Mirrors what the real agent's dispatch_searcher skill
  // will eventually choose. Calls POST /debug/dispatch and then refreshes
  // /field/me so the orders card appears immediately.
  const onDebugDispatch = useCallback(async () => {
    if (!mission || debugBusy) return;
    if (me?.active_dispatch) {
      Alert.alert('Already dispatched', 'Complete or cancel the current dispatch first.');
      return;
    }
    // Pick highest-POA segment with no current assignee.
    const candidate = [...segments]
      .filter((s) => s.properties.assigned_user_id == null)
      .sort((a, b) => b.properties.poa - a.properties.poa)[0];
    if (!candidate) {
      Alert.alert('No segments available', 'All segments already have an assignee.');
      return;
    }
    setDebugBusy(true);
    try {
      const resp = await postDebugDispatch(mission.server_url, mission.bearer_token, {
        segment_id: candidate.properties.id,
        instruction: `Sweep ${candidate.properties.name} (highest POA)`,
      });
      await refreshMe();
      Alert.alert(
        'Dispatched',
        `Sent you to ${candidate.properties.name} ` +
          `(POA ${(candidate.properties.poa * 100).toFixed(1)}%). Dispatch #${resp.id}.`,
      );
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Debug dispatch failed';
      Alert.alert('Dispatch failed', msg);
    } finally {
      setDebugBusy(false);
    }
  }, [mission, segments, me?.active_dispatch, debugBusy, refreshMe]);

  const selfUserId = mission?.user_id ?? null;
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
          // Bumped from 0.5px/0.28α — CartoDB Positron is nearly grayscale
          // and the previous strokes vanished once many cells filled with
          // coverage tint at α≈0.22. 0.85px / 0.42α reads cleanly without
          // dominating the fill.
          strokeColor="rgba(0,0,0,0.42)"
          strokeWidth={0.85}
          fillColor={fillFor(effective, selfUserId)}
        />
      );
    });
  }, [grid, liveHexFlags, selfUserId]);

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
      const { kind, description, confidence, ts } = f.properties;
      const glyph = glyphFor(kind);
      return (
        <Marker
          // Server doesn't currently include finding.id in properties — fall
          // back to ts+index. Findings can't be edited/deleted from the UI so
          // identity stability across polls is good-enough.
          key={`finding-${ts}-${i}`}
          coordinate={{ latitude: lat, longitude: lon }}
          anchor={{ x: 0.5, y: 1 }}
          // CRITICAL: without this, every 5s state poll re-renders the
          // Marker, and RN-maps closes any open Callout as part of the
          // redraw. Symptom is "tap pin, preview flashes for ~500ms,
          // disappears" — fixed by telling RN-maps to render the marker
          // once and ignore subsequent child View changes.
          tracksViewChanges={false}
        >
          <View style={[s.findingPin, { backgroundColor: color }]}>
            <Text style={s.findingPinGlyph}>{glyph}</Text>
          </View>
          <Callout tooltip>
            <FindingCalloutContent
              kind={kind}
              description={description}
              confidence={confidence}
              ts={ts}
              color={color}
              glyph={glyph}
              timeLabel={formatTime(ts)}
            />
          </Callout>
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
    return segments.map((seg) => {
      const { status, assigned_user_id: uid } = seg.properties;
      // Color attribution: assigned/in_progress segments take the assignee's
      // palette color (or self color), so two dispatches to different
      // searchers visually separate. Unassigned/completed segments fall
      // back to the status-only color — completed coverage is signaled
      // by the per-cell hex tints below.
      const assigneeColor = uid != null
        ? (uid === selfUserId ? SELF_TRACK_COLOR : colorForUser(uid))
        : null;
      const showsAssignee = (status === 'assigned' || status === 'in_progress')
        && assigneeColor != null;
      const strokeColor = showsAssignee
        ? assigneeColor!
        : SEGMENT_STATUS_COLOR[status];
      // in_progress = bold solid; assigned = thinner dashed so the searcher
      // can distinguish "I have it" from "I'm actively on it".
      const strokeWidth = status === 'in_progress' ? 2.5 : 1.5;
      const dashed = status === 'assigned';
      return (
        <Polygon
          key={`segment-outline-${seg.properties.id}`}
          coordinates={seg.geometry.coordinates[0].map(([lon, lat]) => ({
            latitude: lat,
            longitude: lon,
          }))}
          strokeColor={strokeColor}
          strokeWidth={strokeWidth}
          lineDashPattern={dashed ? [8, 4] : undefined}
          // Defensive: "transparent" works on most react-native-maps versions but
          // some older builds fall back to translucent red for non-rgba inputs.
          fillColor="rgba(0,0,0,0)"
        />
      );
    });
  }, [segments, selfUserId]);

  // Snap-to-trail route from the searcher's last ping to the active
  // dispatch's segment entry, fetched on-demand by MissionHud and forwarded
  // up via onRouteChange. Rendered as a dashed polyline.
  const routePolyline = useMemo(() => {
    if (!routeWaypoints || routeWaypoints.length < 2) return null;
    return (
      <Polyline
        coordinates={routeWaypoints.map((w) => ({ latitude: w.lat, longitude: w.lon }))}
        strokeColor="rgba(0,0,0,0.7)"
        strokeWidth={3}
        lineDashPattern={[10, 6]}
      />
    );
  }, [routeWaypoints]);

  const segmentLabels = useMemo(() => {
    if (segments.length === 0 || !region) return null;
    // Zoom gate: when zoomed out further than ~550m vertical view, the labels
    // tile-stack into illegible mush. Hide them entirely until the user
    // zooms in enough that the labels can read.
    if (region.latitudeDelta >= LABEL_ZOOM_LAT_DELTA) return null;

    // Focal gate: even when zoomed in, only label segments whose centroid
    // sits inside the user's fovea — a centered box LABEL_FOCAL_FRACTION
    // of the viewport. Off-center hexes still render as outlines so the
    // map structure stays intact; the names just appear where the eye is.
    const cLat = region.latitude;
    const cLon = region.longitude;
    const halfLat = (region.latitudeDelta * LABEL_FOCAL_FRACTION) / 2;
    const halfLon = (region.longitudeDelta * LABEL_FOCAL_FRACTION) / 2;

    return segments
      .filter((seg) => {
        const c = polygonCentroid(seg.geometry.coordinates[0]);
        return (
          Math.abs(c.latitude - cLat) <= halfLat &&
          Math.abs(c.longitude - cLon) <= halfLon
        );
      })
      .map((seg) => {
      const c = polygonCentroid(seg.geometry.coordinates[0]);
      const assigneeUserId = seg.properties.assigned_user_id;
      const assigneeColor =
        assigneeUserId != null ? colorForUser(assigneeUserId) : null;
      // Subtle text-only label. White text with a dark halo (textShadow) so
      // it reads on any underlying tile colour. No background pill, no
      // border — the segment outline already encodes status.
      // The assignee dot rides next to the name (no chip background).
      return (
        <Marker
          key={`segment-label-${seg.properties.id}`}
          coordinate={c}
          anchor={{ x: 0.5, y: 0.5 }}
          tracksViewChanges={false}
        >
          <View style={s.segmentLabelRow} pointerEvents="none">
            <Text style={s.segmentLabelName}>{seg.properties.name}</Text>
            {assigneeColor && (
              <View style={[s.segmentAssigneeDot, { backgroundColor: assigneeColor }]} />
            )}
          </View>
        </Marker>
      );
    });
  }, [segments, callsignByUserId, region]);

  return (
    <View style={s.root}>
      <MapView
        style={StyleSheet.absoluteFill}
        showsUserLocation
        showsCompass={false}
        initialRegion={initialRegion ?? undefined}
        onRegionChangeComplete={setRegion}
        // mapType="none" hides Apple's default tiles; the UrlTile below
        // paints CartoDB Positron in their place — a near-grayscale
        // basemap designed specifically for data overlays. The hex grid
        // and finding pins become the primary visual signal instead of
        // fighting Apple's saturated greens / waters.
        mapType="none"
        showsPointsOfInterest={false}
        showsBuildings={false}
        showsTraffic={false}
      >
        <UrlTile
          // CartoDB Positron — light, neutral, no POI/road labels at low
          // zoom. Free for non-commercial use (cdn-backed; attribution
          // shown on the legend below). {s} cycles a/b/c subdomains.
          urlTemplate="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"
          maximumZ={19}
          flipY={false}
        />
        {polygons}
        {trailLines}
        {hazardPolygons}
        {segmentOutlines}
        {segmentLabels}
        {trackLines}
        {routePolyline}
        {searcherMarkers}
        {findingPins}
      </MapView>

      <MissionHud
        me={me}
        broadcastForceOpen={broadcastForceOpen}
        ordersForceOpen={ordersForceOpen}
        closeBroadcastManual={() => setBroadcastForceOpen(false)}
        closeOrdersManual={() => setOrdersForceOpen(false)}
        onAck={onAck}
        onStart={onStart}
        onComplete={onComplete}
        actionBusy={actionBusy}
        topOffsetPx={insets.top + 60}
      />

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

          {/* All top-right action buttons live in one column so they don't
              fight the title pill for layout. Order: bell, clipboard, dev
              dispatch, finding. The bell/clipboard buttons toggle the
              banner / orders card open even when there's no data, which
              lets you sanity-check that polling is working. */}
          <View style={s.mapctrl}>
            <ActionButton
              glyph="🔔"
              dot={(me?.recent_broadcasts.length ?? 0) > 0}
              onPress={() => setBroadcastForceOpen((v) => !v)}
              a11y="Toggle broadcasts"
            />
            <ActionButton
              glyph="📋"
              dot={me?.active_dispatch != null}
              onPress={() => setOrdersForceOpen((v) => !v)}
              a11y="Toggle current orders"
            />
            <ActionButton
              glyph={debugBusy ? '…' : '🎯'}
              onPress={onDebugDispatch}
              a11y="Dispatch me (dev)"
              disabled={debugBusy}
            />
            <ActionButton
              glyph="🚩"
              onPress={() => setSheetOpen(true)}
              a11y="Log finding"
            />
          </View>
        </View>
      </SafeAreaView>

      <Pressable
        style={({ pressed }) => [s.fab, pressed && s.fabPressed]}
        onPress={() => Alert.alert('Chat', 'Coming soon')}
      >
        <Text style={s.fabIcon}>💬</Text>
      </Pressable>

      <MapLegend topOffsetPx={insets.top + 60} />

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

function fillFor(p: HexProps, selfUserId: number | null): string {
  // Priority order: safety flags > human-raised signals > coverage > base
  // terrain. flag_searched sits above terrain because a phone walking
  // through a "water" cell means either OSM is wrong or there's a
  // bridge — the foot is fresher info than the seed-time classification.
  if (p.flag_impassable) return 'rgba(40,40,40,0.22)';
  if (p.flag_danger) return 'rgba(214,54,47,0.20)';
  if (p.flag_clue) return 'rgba(240,200,60,0.26)';
  if (p.flag_poi) return 'rgba(140,90,200,0.22)';
  if (p.flag_searched) {
    // Coverage attribution: tint by the searcher who marked the cell.
    // Self gets the SELF_TRACK_COLOR (blue) so the searcher's own
    // coverage reads consistently with their track line. Others get
    // their palette color from colorForUser, alpha-matched.
    const uid = p.searched_by_user_id;
    if (uid != null && selfUserId != null && uid === selfUserId) {
      return rgbaWithAlpha(SELF_TRACK_COLOR, 0.22);
    }
    if (uid != null) {
      return rgbaWithAlpha(colorForUser(uid), 0.22);
    }
    // Searched but no attribution recorded (legacy rows): fall back to
    // the old neutral blue so coverage is still visible.
    return 'rgba(43,108,246,0.18)';
  }
  if (p.is_water) return 'rgba(80,150,220,0.20)';
  if (p.is_building) return 'rgba(120,120,120,0.22)';
  return 'rgba(0,0,0,0.04)';
}

/** Turn a hex color like "#2f80ed" into an rgba(...) string with the given
 *  alpha. Used to apply translucent tints derived from the per-user palette
 *  without authoring a parallel alpha-color table. */
function rgbaWithAlpha(hex: string, alpha: number): string {
  // Strip leading '#'. Supports 3- or 6-digit hex.
  const v = hex.replace('#', '');
  const full = v.length === 3
    ? v.split('').map((ch) => ch + ch).join('')
    : v;
  const r = parseInt(full.slice(0, 2), 16);
  const g = parseInt(full.slice(2, 4), 16);
  const b = parseInt(full.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
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

// Segment-label visibility thresholds. Tuned for the current 105m flat-to-flat
// hex segments — ~5 hex rows visible at the zoom-in threshold below.
// LABEL_ZOOM_LAT_DELTA: hide labels entirely when the camera shows more than
//   this much latitude vertically (~550m for delta=0.005). Below this the
//   labels stack into illegible mush.
// LABEL_FOCAL_FRACTION: even when zoomed in, only label segments whose
//   centroid sits inside this fraction of the viewport centered on the
//   camera. 0.5 = inner 50% box, which is roughly the user's fovea on a
//   handheld phone.
const LABEL_ZOOM_LAT_DELTA = 0.005;
// Wider fovea now that labels are just halo'd text instead of pills — the
// visual noise per label is much lower, so we can show more without the
// map turning into label soup.
const LABEL_FOCAL_FRACTION = 0.8;

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

/** Compact "x min ago" / "Hh:Mm" formatter for the finding-callout meta line.
 *  Falls back to a clock time once an event is over an hour old so the
 *  preview doesn't grow to "184 min ago". */
function formatTime(unixSec: number): string {
  const now = Math.floor(Date.now() / 1000);
  const delta = now - unixSec;
  if (delta < 60) return 'just now';
  if (delta < 3600) return `${Math.floor(delta / 60)} min ago`;
  const d = new Date(unixSec * 1000);
  return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
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

function ActionButton({
  glyph, dot, onPress, a11y, disabled,
}: {
  glyph: string;
  onPress: () => void;
  a11y: string;
  dot?: boolean;
  disabled?: boolean;
}) {
  return (
    <Pressable
      style={({ pressed }) => [
        s.mapctrlBtn,
        (pressed || disabled) && s.mapctrlBtnPressed,
      ]}
      onPress={onPress}
      hitSlop={4}
      accessibilityLabel={a11y}
      disabled={disabled}
    >
      <Text style={s.mapctrlGlyph}>{glyph}</Text>
      {dot ? <View style={s.mapctrlDot} /> : null}
    </Pressable>
  );
}

function MapLegend({ topOffsetPx }: { topOffsetPx: number }) {
  // Top-left mini-legend, collapsed by default so it doesn't crowd the map
  // controls or the broadcast banner. Tap the header to expand.
  const [expanded, setExpanded] = useState(false);
  return (
    <View style={[s.legend, { top: topOffsetPx }]}>
      <Pressable
        onPress={() => setExpanded((v) => !v)}
        style={({ pressed }) => [s.legendHeader, pressed && s.legendHeaderPressed]}
        hitSlop={4}
        accessibilityRole="button"
        accessibilityLabel={expanded ? 'Collapse legend' : 'Expand legend'}
      >
        <Text style={s.legendEyebrow}>LEGEND</Text>
        <Text style={s.legendChevron}>{expanded ? '▾' : '▸'}</Text>
      </Pressable>
      {expanded ? (
        <View style={s.legendBody} pointerEvents="none">
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
      ) : null}
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
  // Vertical column holding all top-right action buttons (broadcasts,
  // orders, dev dispatch, finding). Single rounded container so the
  // visual weight matches the title pill on the left.
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
  mapctrlBtnPressed: { opacity: 0.55 },
  mapctrlGlyph: { fontSize: 18 },
  mapctrlDot: {
    position: 'absolute',
    top: 8,
    right: 8,
    width: 7,
    height: 7,
    borderRadius: 3.5,
    backgroundColor: '#d6362f',
    borderWidth: 1,
    borderColor: '#fff',
  },

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

  // findingCallout* styles moved to components/FindingCalloutContent.tsx
  // when the callout body was extracted for unit testing.

  // Text-only segment labels. White text + dark halo via textShadow so they
  // stay legible on any underlying hex colour. No pill background — the
  // segment outline already encodes status. Tap targets aren't needed
  // because the label is decorative (handler-free).
  segmentLabelRow: { flexDirection: 'row', alignItems: 'center', gap: 4 },
  segmentLabelName: {
    fontSize: 12,
    fontWeight: '700',
    color: '#fff',
    // Halo: zero-offset shadow with a small radius mimics a thin dark stroke
    // on every edge. Strong enough to read over light + dark terrain.
    textShadowColor: 'rgba(0,0,0,0.9)',
    textShadowOffset: { width: 0, height: 0 },
    textShadowRadius: 3,
  },
  segmentAssigneeDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    borderWidth: 1.5,
    borderColor: 'rgba(255,255,255,0.95)',
  },

  // Top-left mini-legend. Collapsed by default — only the header pill shows
  // until the user taps to expand. `top` is set inline from
  // useSafeAreaInsets so the legend tucks below the title pill on any
  // device (notched / non-notched / flat).
  legend: {
    position: 'absolute',
    left: 12,
    width: 130,
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
  legendHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 10,
    paddingVertical: 6,
  },
  legendHeaderPressed: { opacity: 0.6 },
  legendBody: {
    paddingHorizontal: 10,
    paddingBottom: 8,
  },
  legendChevron: { fontSize: 11, color: '#6b6b73', fontWeight: '700' },
  legendEyebrow: {
    fontSize: 9,
    fontWeight: '700',
    letterSpacing: 0.8,
    color: '#6b6b73',
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
