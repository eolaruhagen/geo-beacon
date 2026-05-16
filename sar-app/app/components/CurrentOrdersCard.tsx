/**
 * CurrentOrdersCard — bottom-of-screen card showing the active dispatch.
 *
 * Three visibility modes:
 *   - hidden:  no active dispatch and `forceOpen=false` → render nothing.
 *   - auto:    active dispatch present → render with the lifecycle button
 *              morphed for current status.
 *   - forced:  no active dispatch but parent flipped `forceOpen` → render
 *              the empty state so the user can verify polling is working.
 *
 * Lifecycle button morph (one button at a time):
 *   pending      → "Acknowledge"   → calls onAck()
 *   acked        → "Start sweep"   → calls onStart()
 *   in_progress  → "Mark complete" → calls onComplete(notes?)
 *
 * The notes field is only revealed when status='in_progress'.
 */
import { useState } from 'react';
import { Pressable, StyleSheet, Text, TextInput, View } from 'react-native';

import type { ActiveDispatch } from '../lib/api';

type Props = {
  active: ActiveDispatch | null;
  /** Segment name (e.g. 'S-r03-c07') — pulled from MeResponse.segment_geojson.properties.name. */
  segmentName: string | null;
  /** Manual toggle from the parent. */
  forceOpen: boolean;
  /** Parent's manual-close handler. */
  onCloseManual: () => void;
  onAck: () => void;
  onStart: () => void;
  onComplete: (notes?: string) => void;
  /** Lock the action button while the network call is in flight. */
  busy: boolean;
};

export default function CurrentOrdersCard({
  active,
  segmentName,
  forceOpen,
  onCloseManual,
  onAck,
  onStart,
  onComplete,
  busy,
}: Props) {
  const [notes, setNotes] = useState('');

  if (!active && !forceOpen) return null;

  if (!active) {
    // Manual mode with no data.
    return (
      <View style={s.card}>
        <View style={s.header}>
          <Text style={s.title}>Current orders</Text>
          <Pressable onPress={onCloseManual} hitSlop={8} style={s.close}>
            <Text style={s.closeText}>×</Text>
          </Pressable>
        </View>
        <Text style={s.empty}>No active dispatch.</Text>
      </View>
    );
  }

  const { status, instruction, reasoning, sweep_type } = active;

  return (
    <View style={s.card}>
      <View style={s.header}>
        <View style={s.headerLeft}>
          <Text style={s.title}>{segmentName ?? `Dispatch #${active.id}`}</Text>
          <Text style={s.sub}>
            {sweep_type ? `${sweep_type} sweep · ` : ''}
            {labelForStatus(status)}
          </Text>
        </View>
        {forceOpen ? (
          <Pressable onPress={onCloseManual} hitSlop={8} style={s.close}>
            <Text style={s.closeText}>×</Text>
          </Pressable>
        ) : null}
      </View>

      <Text style={s.instruction} numberOfLines={3}>
        {instruction}
      </Text>
      {reasoning ? (
        <Text style={s.reasoning} numberOfLines={2}>
          {reasoning}
        </Text>
      ) : null}

      {status === 'in_progress' ? (
        <TextInput
          style={s.notes}
          placeholder="Optional notes (will be saved with completion)…"
          placeholderTextColor="#9ca3af"
          value={notes}
          onChangeText={setNotes}
          multiline
          maxLength={2000}
        />
      ) : null}

      <Pressable
        style={({ pressed }) => [
          s.button,
          { backgroundColor: BUTTON_COLOR[status] ?? '#5a6cf2' },
          (busy || pressed) && s.buttonPressed,
        ]}
        onPress={() => onAction(status, onAck, onStart, onComplete, notes)}
        disabled={busy || !nextAction(status)}
      >
        <Text style={s.buttonText}>{busy ? '…' : labelForButton(status)}</Text>
      </Pressable>
    </View>
  );
}

function nextAction(status: ActiveDispatch['status']): boolean {
  return status === 'pending' || status === 'acked' || status === 'in_progress';
}

function labelForStatus(status: ActiveDispatch['status']): string {
  switch (status) {
    case 'pending': return 'awaiting acknowledgement';
    case 'acked': return 'acknowledged — ready to start';
    case 'in_progress': return 'in progress';
    case 'completed': return 'completed';
    case 'cancelled': return 'cancelled';
    case 'superseded': return 'superseded';
  }
}

function labelForButton(status: ActiveDispatch['status']): string {
  switch (status) {
    case 'pending': return 'Acknowledge';
    case 'acked': return 'Start sweep';
    case 'in_progress': return 'Mark complete';
    default: return '—';
  }
}

function onAction(
  status: ActiveDispatch['status'],
  ack: () => void,
  start: () => void,
  complete: (notes?: string) => void,
  notes: string,
) {
  if (status === 'pending') ack();
  else if (status === 'acked') start();
  else if (status === 'in_progress') complete(notes.trim() || undefined);
}

const BUTTON_COLOR: Partial<Record<ActiveDispatch['status'], string>> = {
  pending: '#5a6cf2',     // blue: acknowledge
  acked: '#d4a017',       // amber: start
  in_progress: '#27ae60', // green: complete
};

const s = StyleSheet.create({
  card: {
    position: 'absolute',
    bottom: 110,
    left: 12,
    right: 12,
    backgroundColor: 'rgba(255,255,255,0.96)',
    borderRadius: 16,
    padding: 14,
    shadowColor: '#000',
    shadowOpacity: 0.14,
    shadowRadius: 18,
    shadowOffset: { width: 0, height: 6 },
    gap: 8,
  },
  header: { flexDirection: 'row', alignItems: 'flex-start' },
  headerLeft: { flex: 1 },
  title: { fontSize: 16, fontWeight: '700', color: '#0b0b0c' },
  sub: { fontSize: 11, color: '#6b6b73', marginTop: 2, textTransform: 'lowercase' },
  close: {
    width: 28,
    height: 28,
    borderRadius: 14,
    backgroundColor: 'rgba(120,120,128,0.14)',
    alignItems: 'center',
    justifyContent: 'center',
  },
  closeText: { fontSize: 18, color: '#3c3c43', lineHeight: 20 },
  instruction: { fontSize: 14, color: '#0b0b0c', lineHeight: 19 },
  reasoning: { fontSize: 11, color: '#6b6b73', fontStyle: 'italic' },
  notes: {
    minHeight: 44,
    maxHeight: 100,
    backgroundColor: '#f5f5f7',
    borderRadius: 10,
    paddingHorizontal: 10,
    paddingVertical: 8,
    fontSize: 13,
    color: '#0b0b0c',
  },
  button: {
    borderRadius: 12,
    paddingVertical: 12,
    alignItems: 'center',
    marginTop: 4,
  },
  buttonPressed: { opacity: 0.7 },
  buttonText: { color: '#fff', fontSize: 15, fontWeight: '700' },
  empty: { fontSize: 13, color: '#6b6b73', fontStyle: 'italic' },
});
