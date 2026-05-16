import * as Location from 'expo-location';
import { useState } from 'react';
import {
  ActivityIndicator,
  KeyboardAvoidingView,
  Modal,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';

import { postFinding, ServerError } from '../lib/api';
import type { FindingKind } from '../lib/missionState';

type Props = {
  visible: boolean;
  serverUrl: string;
  bearerToken: string;
  onClose: () => void;
  onSuccess: () => void;     // triggered after a 201 — caller can refetch hex_grid
  onFatalAuthError: () => void; // 409 "no active mission" — caller should bounce out
};

// Default confidence baked in — we dropped the UI picker because field
// volunteers don't reliably distinguish 0.3 / 0.6 / 0.9 and the agent's POA
// math is fine with a constant for v1.
const DEFAULT_CONFIDENCE = 0.6;

const PRIMARY_KINDS: FindingKind[] = ['clue', 'footprint', 'discarded_item', 'subject_sighting'];
const SECONDARY_KINDS: FindingKind[] = ['subject_found', 'hazard'];

const KIND_LABEL: Record<FindingKind, string> = {
  clue: 'Clue',
  footprint: 'Footprint',
  discarded_item: 'Item',
  subject_sighting: 'Sighting',
  subject_found: 'Subject Found',
  hazard: 'Hazard',
};

const KIND_COLOR: Record<FindingKind, string> = {
  clue: '#d4a017',
  footprint: '#8b6f47',
  discarded_item: '#e67e22',
  subject_sighting: '#c0392b',
  subject_found: '#27ae60',
  hazard: '#d6362f',
};

export default function FindingSheet({
  visible,
  serverUrl,
  bearerToken,
  onClose,
  onSuccess,
  onFatalAuthError,
}: Props) {
  const [kind, setKind] = useState<FindingKind>('clue');
  const [description, setDescription] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset state every time the sheet opens.
  function reset() {
    setKind('clue');
    setDescription('');
    setError(null);
    setSubmitting(false);
  }

  async function submit() {
    setSubmitting(true);
    setError(null);
    try {
      // watchPositionAsync is already running (started in mission.tsx via
      // startTracking), so a cached fix should be available.
      const pos = await Location.getLastKnownPositionAsync({ maxAge: 30_000 });
      if (!pos) {
        setError('No GPS fix yet — wait a moment and try again.');
        setSubmitting(false);
        return;
      }
      await postFinding(serverUrl, bearerToken, {
        lat: pos.coords.latitude,
        lon: pos.coords.longitude,
        kind,
        description: description.trim() || undefined,
        confidence: DEFAULT_CONFIDENCE,
      });
      onSuccess();
      reset();
      onClose();
    } catch (e) {
      if (e instanceof ServerError && e.status === 422) {
        setError('You appear to be outside the search area. Move into the grid and try again.');
      } else if (e instanceof ServerError && e.status === 409) {
        setError('No active mission. Returning to mission selector…');
        setTimeout(onFatalAuthError, 1500);
      } else {
        setError('Could not save finding. Try again.');
      }
      setSubmitting(false);
    }
  }

  return (
    <Modal
      visible={visible}
      transparent
      animationType="slide"
      onRequestClose={() => {
        if (!submitting) {
          reset();
          onClose();
        }
      }}
    >
      <View style={s.backdrop}>
        <Pressable
          style={StyleSheet.absoluteFill}
          onPress={() => {
            if (!submitting) {
              reset();
              onClose();
            }
          }}
        />
        <KeyboardAvoidingView
          behavior={Platform.OS === 'ios' ? 'padding' : undefined}
          style={s.sheetWrap}
        >
          <View style={s.sheet}>
            <View style={s.grabber} />

            <View style={s.headerRow}>
              <Text style={s.title}>Log finding</Text>
              <Pressable
                onPress={() => {
                  if (!submitting) {
                    reset();
                    onClose();
                  }
                }}
                hitSlop={8}
                style={s.closeBtn}
              >
                <Text style={s.closeText}>×</Text>
              </Pressable>
            </View>

            <KindRow
              kinds={PRIMARY_KINDS}
              selected={kind}
              onSelect={setKind}
            />
            <KindRow
              kinds={SECONDARY_KINDS}
              selected={kind}
              onSelect={setKind}
            />

            <TextInput
              style={s.input}
              placeholder="Notes (optional)"
              placeholderTextColor="#9b9ba1"
              value={description}
              onChangeText={setDescription}
              multiline
              maxLength={200}
              editable={!submitting}
            />

            {error && <Text style={s.error}>{error}</Text>}

            <Pressable
              style={({ pressed }) => [
                s.submit,
                { backgroundColor: KIND_COLOR[kind] },
                pressed && s.submitPressed,
                submitting && s.submitDisabled,
              ]}
              onPress={submit}
              disabled={submitting}
            >
              {submitting ? (
                <ActivityIndicator color="#fff" />
              ) : (
                <Text style={s.submitText}>Drop pin</Text>
              )}
            </Pressable>
          </View>
        </KeyboardAvoidingView>
      </View>
    </Modal>
  );
}

function KindRow({
  kinds,
  selected,
  onSelect,
}: {
  kinds: FindingKind[];
  selected: FindingKind;
  onSelect: (k: FindingKind) => void;
}) {
  return (
    <View style={s.kindRow}>
      {kinds.map((k) => {
        const isSelected = k === selected;
        return (
          <Pressable
            key={k}
            onPress={() => onSelect(k)}
            style={[
              s.kindPill,
              isSelected && { backgroundColor: KIND_COLOR[k], borderColor: KIND_COLOR[k] },
            ]}
          >
            <Text
              style={[s.kindPillText, isSelected && s.kindPillTextSelected]}
              numberOfLines={1}
            >
              {KIND_LABEL[k]}
            </Text>
          </Pressable>
        );
      })}
    </View>
  );
}

const s = StyleSheet.create({
  backdrop: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.35)',
    justifyContent: 'flex-end',
  },
  sheetWrap: {
    width: '100%',
  },
  sheet: {
    backgroundColor: 'rgba(255,255,255,0.98)',
    paddingTop: 8,
    paddingHorizontal: 16,
    paddingBottom: 32,
    borderTopLeftRadius: 18,
    borderTopRightRadius: 18,
    shadowColor: '#000',
    shadowOpacity: 0.18,
    shadowRadius: 24,
    shadowOffset: { width: 0, height: -4 },
  },
  grabber: {
    alignSelf: 'center',
    width: 38,
    height: 4,
    borderRadius: 2,
    backgroundColor: 'rgba(0,0,0,0.18)',
    marginBottom: 12,
  },
  headerRow: {
    flexDirection: 'row',
    alignItems: 'center',
    marginBottom: 12,
  },
  title: { flex: 1, fontSize: 17, fontWeight: '600', color: '#0b0b0c' },
  closeBtn: {
    width: 28,
    height: 28,
    borderRadius: 14,
    backgroundColor: 'rgba(120,120,128,0.16)',
    alignItems: 'center',
    justifyContent: 'center',
  },
  closeText: { fontSize: 18, color: '#3c3c43', lineHeight: 20, fontWeight: '500' },

  kindRow: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: 6,
    marginBottom: 6,
  },
  kindPill: {
    flexGrow: 1,
    flexBasis: '22%',
    paddingVertical: 9,
    paddingHorizontal: 10,
    borderRadius: 10,
    backgroundColor: 'rgba(120,120,128,0.10)',
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: 'rgba(0,0,0,0.06)',
    alignItems: 'center',
  },
  kindPillText: { fontSize: 12, color: '#3c3c43', fontWeight: '500' },
  kindPillTextSelected: { color: '#fff', fontWeight: '600' },

  input: {
    marginTop: 12,
    minHeight: 64,
    maxHeight: 120,
    borderRadius: 12,
    backgroundColor: 'rgba(120,120,128,0.10)',
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: 14,
    color: '#0b0b0c',
    textAlignVertical: 'top',
  },

  error: {
    marginTop: 8,
    color: '#c0392b',
    fontSize: 12,
  },

  submit: {
    marginTop: 14,
    height: 48,
    borderRadius: 14,
    alignItems: 'center',
    justifyContent: 'center',
  },
  submitPressed: { opacity: 0.85 },
  submitDisabled: { opacity: 0.5 },
  submitText: { color: '#fff', fontSize: 15, fontWeight: '600' },
});
