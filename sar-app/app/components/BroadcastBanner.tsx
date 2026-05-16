/**
 * BroadcastBanner — top-of-screen overlay for recent broadcasts.
 *
 * Two visibility modes (parent passes `forceOpen` for the manual mode):
 *   - auto:   shows when there's at least one broadcast and the latest one
 *             is newer than what the user has dismissed.
 *   - forced: always rendered (parent's icon toggle opens this).
 *
 * Visual is intentionally minimal — color by kind, one-line message,
 * dismiss button on the right. Polish later.
 */
import { useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';

import type { BroadcastDTO, BroadcastKind } from '../lib/api';

type Props = {
  broadcasts: BroadcastDTO[];
  /** Manual toggle from the parent — force the banner open even with no data. */
  forceOpen: boolean;
  /** Parent closes the manual-open mode. */
  onCloseManual: () => void;
};

const KIND_COLOR: Record<BroadcastKind, string> = {
  info: '#5a6cf2',
  warning: '#e67e22',
  recall: '#d6362f',
  finding_alert: '#27ae60',
  route_correction: '#d4a017',
};

export default function BroadcastBanner({ broadcasts, forceOpen, onCloseManual }: Props) {
  // Latest dismissed broadcast ts — anything newer than this auto-shows.
  // Stored locally; persistence across reloads is a follow-up if needed.
  const [dismissedTs, setDismissedTs] = useState<number>(0);

  const latest = broadcasts.length > 0 ? broadcasts[0] : null;
  const hasNew = latest != null && latest.ts > dismissedTs;

  if (!forceOpen && !hasNew) return null;

  return (
    <View style={s.wrap} pointerEvents="box-none">
      {forceOpen && broadcasts.length === 0 ? (
        <View style={[s.banner, { borderLeftColor: '#9ca3af' }]}>
          <View style={s.content}>
            <Text style={s.empty}>No broadcasts yet.</Text>
          </View>
          <Pressable onPress={onCloseManual} style={s.dismiss} hitSlop={8}>
            <Text style={s.dismissText}>×</Text>
          </Pressable>
        </View>
      ) : (
        <ScrollView
          style={s.list}
          contentContainerStyle={s.listContent}
          showsVerticalScrollIndicator={false}
        >
          {(forceOpen ? broadcasts : [latest!]).map((b) => (
            <View
              key={b.id}
              style={[s.banner, { borderLeftColor: KIND_COLOR[b.kind] ?? '#9ca3af' }]}
            >
              <View style={s.content}>
                <Text style={s.kind}>{b.kind.toUpperCase()}</Text>
                <Text style={s.message} numberOfLines={forceOpen ? undefined : 2}>
                  {b.message}
                </Text>
              </View>
              {!forceOpen ? (
                <Pressable
                  onPress={() => setDismissedTs(b.ts)}
                  style={s.dismiss}
                  hitSlop={8}
                  accessibilityLabel="Dismiss broadcast"
                >
                  <Text style={s.dismissText}>×</Text>
                </Pressable>
              ) : null}
            </View>
          ))}
          {forceOpen ? (
            <Pressable onPress={onCloseManual} style={s.closeAll} hitSlop={4}>
              <Text style={s.closeAllText}>Close inbox</Text>
            </Pressable>
          ) : null}
        </ScrollView>
      )}
    </View>
  );
}

const s = StyleSheet.create({
  wrap: {
    position: 'absolute',
    top: 70,
    left: 12,
    right: 12,
    zIndex: 10,
  },
  list: {
    maxHeight: 280,
  },
  listContent: {
    gap: 6,
  },
  banner: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: 'rgba(255,255,255,0.96)',
    borderRadius: 12,
    borderLeftWidth: 4,
    paddingVertical: 10,
    paddingHorizontal: 12,
    shadowColor: '#000',
    shadowOpacity: 0.12,
    shadowRadius: 12,
    shadowOffset: { width: 0, height: 4 },
  },
  content: { flex: 1, gap: 2 },
  kind: { fontSize: 9, fontWeight: '700', letterSpacing: 0.6, color: '#6b6b73' },
  message: { fontSize: 13, color: '#0b0b0c' },
  empty: { fontSize: 13, color: '#6b6b73', fontStyle: 'italic' },
  dismiss: {
    width: 28,
    height: 28,
    borderRadius: 14,
    backgroundColor: 'rgba(120,120,128,0.16)',
    alignItems: 'center',
    justifyContent: 'center',
    marginLeft: 8,
  },
  dismissText: { fontSize: 18, color: '#3c3c43', lineHeight: 20 },
  closeAll: {
    alignSelf: 'center',
    paddingVertical: 6,
    paddingHorizontal: 12,
  },
  closeAllText: { fontSize: 12, color: '#3c3c43', fontWeight: '600' },
});
