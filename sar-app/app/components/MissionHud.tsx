/**
 * MissionHud — single owner of useMe + useRoute + the toggle icons that let
 * the user force-open the Broadcast and Current-Orders surfaces.
 *
 * Responsibilities:
 *   - Drive the /field/me poll via useMe.
 *   - Trigger /field/me/route via useRoute when the active dispatch is
 *     acked/in_progress, refreshable on demand.
 *   - Expose two manual toggle icons (bell, clipboard) so the user can
 *     verify polling is working even when there's no data to auto-show.
 *   - Forward dispatch action callbacks to api.ts and call refresh() so
 *     the UI updates without waiting for the next poll tick.
 *
 * Doesn't own the map — `mission.tsx` consumes the route waypoints
 * exposed via the `onRouteChange` callback to render the polyline.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import BroadcastBanner from './BroadcastBanner';
import CurrentOrdersCard from './CurrentOrdersCard';
import {
  ackDispatch,
  completeDispatch,
  startDispatch,
  type ActiveDispatch,
  type BroadcastDTO,
  type RouteWaypoint,
} from '../lib/api';
import { useMe } from '../lib/useMe';
import { useRoute } from '../lib/useRoute';

type Props = {
  serverUrl: string | null;
  bearerToken: string | null;
  /** Notified when route waypoints change so the parent can draw a polyline. */
  onRouteChange?: (waypoints: RouteWaypoint[] | null) => void;
};

export default function MissionHud({ serverUrl, bearerToken, onRouteChange }: Props) {
  const { data: me, refresh: refreshMe } = useMe(serverUrl, bearerToken);
  const { data: route, fetch: fetchRoute } = useRoute(serverUrl, bearerToken);

  // Manual visibility toggles. When data is present these are ignored
  // (the auto-show path runs). When data is absent, flipping these forces
  // the empty-state view so the user can verify the hook is alive.
  const [broadcastForceOpen, setBroadcastForceOpen] = useState(false);
  const [ordersForceOpen, setOrdersForceOpen] = useState(false);

  // Track whether an action is in flight so the lifecycle button locks.
  const [actionBusy, setActionBusy] = useState(false);

  // When the active dispatch transitions into a state where a route makes
  // sense (acked / in_progress), fetch it. Clear when it drops out.
  const lastFetchedKey = useRef<string | null>(null);
  useEffect(() => {
    const ad = me?.active_dispatch ?? null;
    const wantsRoute =
      ad != null && ad.segment_id != null &&
      (ad.status === 'acked' || ad.status === 'in_progress');

    const key = wantsRoute ? `${ad!.id}:${ad!.status}` : null;
    if (key === lastFetchedKey.current) return;
    lastFetchedKey.current = key;

    if (wantsRoute) {
      void fetchRoute(ad!.segment_id!);
    } else {
      void fetchRoute(null);
    }
  }, [me?.active_dispatch, fetchRoute]);

  // Forward route changes to the parent for map rendering.
  useEffect(() => {
    onRouteChange?.(route?.waypoints ?? null);
  }, [route, onRouteChange]);

  // ─── action handlers ────────────────────────────────────────────────────
  // Each one: call the endpoint, then refresh /field/me so the card updates
  // without waiting for the next 5s tick.
  const runAction = useCallback(
    async (action: () => Promise<unknown>) => {
      if (!serverUrl || !bearerToken) return;
      setActionBusy(true);
      try {
        await action();
        await refreshMe();
      } catch (e) {
        // Surface in console for now — a toast would be nicer once we have one.
        console.warn('[dispatch action] failed', e);
      } finally {
        setActionBusy(false);
      }
    },
    [serverUrl, bearerToken, refreshMe],
  );

  const ad = me?.active_dispatch ?? null;
  const onAck = useCallback(
    () => ad && runAction(() => ackDispatch(serverUrl!, bearerToken!, ad.id)),
    [ad, serverUrl, bearerToken, runAction],
  );
  const onStart = useCallback(
    () => ad && runAction(() => startDispatch(serverUrl!, bearerToken!, ad.id)),
    [ad, serverUrl, bearerToken, runAction],
  );
  const onComplete = useCallback(
    (notes?: string) =>
      ad && runAction(() => completeDispatch(serverUrl!, bearerToken!, ad.id, notes)),
    [ad, serverUrl, bearerToken, runAction],
  );

  // ─── derived ────────────────────────────────────────────────────────────
  const broadcasts: BroadcastDTO[] = me?.recent_broadcasts ?? [];
  const segmentName =
    me?.segment_geojson?.properties?.name ??
    (ad ? `Dispatch #${ad.id}` : null);

  const hasUnreadBroadcast = broadcasts.length > 0;
  const hasActiveDispatch = ad !== null;

  return (
    <>
      <BroadcastBanner
        broadcasts={broadcasts}
        forceOpen={broadcastForceOpen}
        onCloseManual={() => setBroadcastForceOpen(false)}
      />

      <CurrentOrdersCard
        active={ad}
        segmentName={segmentName}
        forceOpen={ordersForceOpen}
        onCloseManual={() => setOrdersForceOpen(false)}
        onAck={onAck}
        onStart={onStart}
        onComplete={onComplete}
        busy={actionBusy}
      />

      {/* Toggle stack on the right side — bell + clipboard. */}
      <View style={s.toggles} pointerEvents="box-none">
        <ToggleButton
          label="🔔"
          dot={hasUnreadBroadcast}
          onPress={() => setBroadcastForceOpen((v) => !v)}
          a11y="Broadcasts"
        />
        <ToggleButton
          label="📋"
          dot={hasActiveDispatch}
          onPress={() => setOrdersForceOpen((v) => !v)}
          a11y="Current orders"
        />
      </View>
    </>
  );
}

function ToggleButton({
  label, dot, onPress, a11y,
}: { label: string; dot: boolean; onPress: () => void; a11y: string }) {
  return (
    <Pressable
      style={({ pressed }) => [s.toggleBtn, pressed && s.toggleBtnPressed]}
      onPress={onPress}
      hitSlop={6}
      accessibilityLabel={a11y}
    >
      <Text style={s.toggleGlyph}>{label}</Text>
      {dot ? <View style={s.toggleDot} /> : null}
    </Pressable>
  );
}

const s = StyleSheet.create({
  toggles: {
    position: 'absolute',
    top: 70,
    right: 12,
    gap: 8,
    zIndex: 5,
  },
  toggleBtn: {
    width: 40,
    height: 40,
    borderRadius: 20,
    backgroundColor: 'rgba(255,255,255,0.94)',
    alignItems: 'center',
    justifyContent: 'center',
    shadowColor: '#000',
    shadowOpacity: 0.12,
    shadowRadius: 8,
    shadowOffset: { width: 0, height: 4 },
  },
  toggleBtnPressed: { opacity: 0.7 },
  toggleGlyph: { fontSize: 18 },
  toggleDot: {
    position: 'absolute',
    top: 4,
    right: 4,
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: '#d6362f',
    borderWidth: 1,
    borderColor: '#fff',
  },
});
