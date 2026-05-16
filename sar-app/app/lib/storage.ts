import * as SecureStore from 'expo-secure-store';

export const Keys = {
  ServerUrl: 'serverUrl',
  DisplayName: 'displayName',
  Callsign: 'callsign',
  JoinCode: 'joinCode',
  CurrentMission: 'currentMission',
} as const;

export type StorageKey = (typeof Keys)[keyof typeof Keys];

export type CurrentMission = {
  mission_id: number;
  bearer_token: string;
  user_id: number;
  callsign: string | null;
  mission_name: string;
  server_url: string;
};

export async function getString(key: StorageKey): Promise<string | null> {
  return SecureStore.getItemAsync(key);
}

export async function setString(key: StorageKey, value: string): Promise<void> {
  await SecureStore.setItemAsync(key, value);
}

export async function getJSON<T>(key: StorageKey): Promise<T | null> {
  const raw = await SecureStore.getItemAsync(key);
  if (raw === null) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

export async function setJSON<T>(key: StorageKey, value: T): Promise<void> {
  await SecureStore.setItemAsync(key, JSON.stringify(value));
}

export async function clear(key: StorageKey): Promise<void> {
  await SecureStore.deleteItemAsync(key);
}
