/**
 * Pure presentation for the finding-pin Callout — extracted from
 * mission.tsx so it can be unit-tested without a real react-native-maps
 * Marker mounted around it.
 *
 * The Marker that wraps this component sets tracksViewChanges={false},
 * which is the actual fix for the "tap pin, callout flashes for half a
 * second, disappears" bug — RN-maps was closing the open callout every
 * time the 5s state poll triggered a parent re-render. This component
 * is intentionally side-effect-free so the re-render behavior is
 * driven entirely by the input props.
 */
import { StyleSheet, Text, View } from 'react-native';

import type { FindingKind } from '../lib/missionState';

type Props = {
  kind: FindingKind;
  description: string | null;
  confidence: number;
  ts: number;
  /** Fill color for the kind-swatch and (matching) parent pin. */
  color: string;
  /** Single-character glyph for the swatch. Mirror of the parent pin. */
  glyph: string;
  /** Pre-formatted relative time string ("3 min ago" / "2:14 PM"). */
  timeLabel: string;
};

export default function FindingCalloutContent({
  kind, description, confidence, color, glyph, timeLabel,
}: Props) {
  return (
    <View style={s.card} testID="finding-callout">
      <View style={s.row}>
        <View style={[s.swatch, { backgroundColor: color }]}>
          <Text style={s.swatchGlyph}>{glyph}</Text>
        </View>
        <Text style={s.kind}>{kind.replace('_', ' ')}</Text>
      </View>
      {description ? (
        <Text style={s.desc} numberOfLines={4}>
          {description}
        </Text>
      ) : (
        <Text style={s.descMuted}>No description.</Text>
      )}
      <Text style={s.meta}>
        {`Confidence ${Math.round(confidence * 100)}% · ${timeLabel}`}
      </Text>
    </View>
  );
}

const s = StyleSheet.create({
  card: {
    backgroundColor: '#fff',
    borderRadius: 12,
    paddingVertical: 8,
    paddingHorizontal: 10,
    minWidth: 180,
    maxWidth: 240,
    shadowColor: '#000',
    shadowOpacity: 0.18,
    shadowRadius: 8,
    shadowOffset: { width: 0, height: 3 },
    gap: 4,
  },
  row: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  swatch: {
    width: 18, height: 18, borderRadius: 9,
    alignItems: 'center', justifyContent: 'center',
  },
  swatchGlyph: { color: '#fff', fontSize: 10, fontWeight: '700' },
  kind: {
    fontSize: 13, fontWeight: '700', color: '#0b0b0c',
    textTransform: 'capitalize',
  },
  desc: { fontSize: 12, color: '#0b0b0c', lineHeight: 16 },
  descMuted: { fontSize: 12, color: '#6b6b73', fontStyle: 'italic' },
  meta: {
    fontSize: 10, color: '#6b6b73', marginTop: 2,
    fontVariant: ['tabular-nums'],
  },
});
