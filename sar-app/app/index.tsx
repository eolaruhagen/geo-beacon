import { router } from 'expo-router';
import { useEffect, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';

import { getDemoCredentials, joinMission, NetworkError, ServerError } from './lib/api';
import { Keys, getString, setString, setJSON, CurrentMission } from './lib/storage';

type FieldErrors = {
  serverUrl?: string;
  displayName?: string;
  joinCode?: string;
  submit?: string;
};

export default function MissionSelector() {
  const [serverUrl, setServerUrl] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [callsign, setCallsign] = useState('');
  const [joinCode, setJoinCode] = useState('');
  const [errors, setErrors] = useState<FieldErrors>({});
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void (async () => {
      const [u, n, c, j] = await Promise.all([
        getString(Keys.ServerUrl),
        getString(Keys.DisplayName),
        getString(Keys.Callsign),
        getString(Keys.JoinCode),
      ]);
      if (u) setServerUrl(u);
      if (n) setDisplayName(n);
      if (c) setCallsign(c);
      if (j) setJoinCode(j);
    })();
  }, []);

  async function onDemoMode() {
    console.log('[demo] onDemoMode tapped, serverUrl=', JSON.stringify(serverUrl));
    if (!serverUrl.trim()) {
      console.log('[demo] empty serverUrl, bailing');
      setErrors({ serverUrl: 'Required' });
      return;
    }
    setBusy(true);
    try {
      await setString(Keys.ServerUrl, serverUrl.trim());
      console.log('[demo] calling getDemoCredentials');
      const creds = await getDemoCredentials(serverUrl.trim());
      console.log('[demo] got creds', JSON.stringify(creds));
      const mission: CurrentMission = {
        mission_id: creds.mission_id,
        bearer_token: creds.bearer_token,
        user_id: creds.user_id,
        callsign: creds.callsign,
        mission_name: `Demo #${creds.mission_id}`,
        server_url: serverUrl.trim(),
      };
      await setJSON(Keys.CurrentMission, mission);
      console.log('[demo] navigating to /mission');
      router.replace('/mission');
    } catch (e) {
      const msg = e instanceof Error ? `${e.name}: ${e.message}` : String(e);
      console.log('[demo] ERROR', msg);
      Alert.alert('Demo mode failed', msg);
      if (e instanceof ServerError && e.status === 404) {
        setErrors({ submit: 'No demo snapshot loaded. POST /debug/restore first.' });
      } else if (e instanceof ServerError) {
        setErrors({ submit: `Server error: ${e.detail}` });
      } else if (e instanceof NetworkError) {
        setErrors({ submit: "Could not reach server. Check the URL." });
      } else {
        setErrors({ submit: 'Unexpected error' });
      }
    } finally {
      setBusy(false);
    }
  }

  async function onJoin() {
    const nextErrors: FieldErrors = {};
    if (!serverUrl.trim()) nextErrors.serverUrl = 'Required';
    if (!displayName.trim()) nextErrors.displayName = 'Required';
    if (!joinCode.trim()) nextErrors.joinCode = 'Required';
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;

    setBusy(true);
    try {
      await Promise.all([
        setString(Keys.ServerUrl, serverUrl.trim()),
        setString(Keys.DisplayName, displayName.trim()),
        setString(Keys.Callsign, callsign.trim()),
        setString(Keys.JoinCode, joinCode.trim()),
      ]);

      const resp = await joinMission(serverUrl.trim(), {
        join_code: joinCode.trim(),
        display_name: displayName.trim(),
        callsign: callsign.trim() || undefined,
      });

      const mission: CurrentMission = {
        mission_id: resp.mission_id,
        bearer_token: resp.bearer_token,
        user_id: resp.user_id,
        callsign: resp.callsign,
        mission_name: `Mission #${resp.mission_id}`,
        server_url: serverUrl.trim(),
      };
      await setJSON(Keys.CurrentMission, mission);

      router.replace('/mission');
    } catch (e) {
      if (e instanceof ServerError && e.status === 404) {
        setErrors({ joinCode: 'Join code not found' });
      } else if (e instanceof ServerError) {
        setErrors({ submit: `Server error: ${e.detail}` });
      } else if (e instanceof NetworkError) {
        setErrors({
          submit: "Could not reach server. Check the URL and that you're on the same network.",
        });
      } else {
        setErrors({ submit: 'Unexpected error' });
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <KeyboardAvoidingView
      style={s.flex}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <ScrollView contentContainerStyle={s.scroll} keyboardShouldPersistTaps="handled">
        <Text style={s.title}>OpenSAR</Text>
        <Text style={s.subtitle}>Join a search-and-rescue mission</Text>

        <Field
          label="Server URL"
          value={serverUrl}
          onChangeText={setServerUrl}
          placeholder="https://abc123.ngrok.io"
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
          error={errors.serverUrl}
        />
        <Field
          label="Display name"
          value={displayName}
          onChangeText={setDisplayName}
          placeholder="Alice"
          autoCapitalize="words"
          error={errors.displayName}
        />
        <Field
          label="Callsign (optional)"
          value={callsign}
          onChangeText={setCallsign}
          placeholder="A1"
          autoCapitalize="characters"
          autoCorrect={false}
        />
        <Field
          label="Join code"
          value={joinCode}
          onChangeText={setJoinCode}
          placeholder="6-character code"
          autoCapitalize="none"
          autoCorrect={false}
          error={errors.joinCode}
        />

        {errors.submit ? <Text style={s.submitError}>{errors.submit}</Text> : null}

        <Pressable
          onPress={onJoin}
          disabled={busy}
          style={({ pressed }) => [
            s.button,
            (busy || pressed) && s.buttonPressed,
          ]}
        >
          {busy ? (
            <View style={s.buttonInner}>
              <ActivityIndicator color="#fff" />
              <Text style={s.buttonText}>Joining…</Text>
            </View>
          ) : (
            <Text style={s.buttonText}>Join mission</Text>
          )}
        </Pressable>

        {/* Demo mode: skips join entirely. Hits /debug/demo-credentials to
            grab the observer user's bearer token (after /debug/restore has
            been called server-side), stashes it as if it were a fresh join,
            and jumps straight to the mission. Phone's pings get muted
            server-side; the sim writes them instead. */}
        <Pressable
          onPress={onDemoMode}
          disabled={busy}
          style={({ pressed }) => [
            s.demoButton,
            (busy || pressed) && s.buttonPressed,
          ]}
        >
          <Text style={s.demoButtonText}>Demo mode (observer)</Text>
        </Pressable>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

type FieldProps = React.ComponentProps<typeof TextInput> & {
  label: string;
  error?: string;
};

function Field({ label, error, ...inputProps }: FieldProps) {
  return (
    <View style={s.field}>
      <Text style={s.label}>{label}</Text>
      <TextInput
        {...inputProps}
        style={[s.input, error ? s.inputError : null]}
        placeholderTextColor="#a3a3aa"
      />
      {error ? <Text style={s.fieldError}>{error}</Text> : null}
    </View>
  );
}

const ACCENT = '#d6362f';

const s = StyleSheet.create({
  flex: { flex: 1, backgroundColor: '#eef0f2' },
  scroll: { padding: 24, paddingTop: 80 },
  title: { fontSize: 32, fontWeight: '700', color: '#0b0b0c' },
  subtitle: { fontSize: 15, color: '#6b6b73', marginTop: 4, marginBottom: 32 },
  field: { marginBottom: 16 },
  label: { fontSize: 12, fontWeight: '600', color: '#3c3c43', marginBottom: 6, textTransform: 'uppercase', letterSpacing: 0.4 },
  input: {
    backgroundColor: '#fff',
    borderRadius: 12,
    paddingHorizontal: 14,
    paddingVertical: 14,
    fontSize: 16,
    color: '#0b0b0c',
    borderWidth: 1,
    borderColor: 'rgba(60,60,67,0.18)',
  },
  inputError: { borderColor: ACCENT },
  fieldError: { color: ACCENT, fontSize: 12, marginTop: 4 },
  submitError: { color: ACCENT, fontSize: 13, marginTop: 8, marginBottom: 8 },
  button: {
    marginTop: 16,
    backgroundColor: ACCENT,
    borderRadius: 14,
    paddingVertical: 16,
    alignItems: 'center',
  },
  buttonPressed: { opacity: 0.7 },
  buttonInner: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  buttonText: { color: '#fff', fontSize: 16, fontWeight: '600' },
  demoButton: {
    marginTop: 10,
    borderRadius: 14,
    paddingVertical: 14,
    alignItems: 'center',
    borderWidth: 1,
    borderColor: 'rgba(60,60,67,0.25)',
    backgroundColor: '#fff',
  },
  demoButtonText: { color: '#3c3c43', fontSize: 14, fontWeight: '500' },
});
