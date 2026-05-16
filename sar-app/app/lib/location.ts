import * as Location from 'expo-location';

import { postPing } from './api';

let subscription: Location.LocationSubscription | null = null;

export async function startTracking(serverUrl: string, bearerToken: string): Promise<void> {
  await stopTracking();

  const perm = await Location.requestForegroundPermissionsAsync();
  if (perm.status !== 'granted') {
    throw new Error('Location permission denied');
  }

  subscription = await Location.watchPositionAsync(
    {
      accuracy: Location.Accuracy.High,
      timeInterval: 3000,
      distanceInterval: 5,
    },
    (location) => {
      const { latitude, longitude, accuracy, speed } = location.coords;
      const body: Parameters<typeof postPing>[2] = {
        lat: latitude,
        lon: longitude,
        ts: Math.floor(Date.now() / 1000),
      };
      if (typeof accuracy === 'number' && accuracy >= 0) body.accuracy_m = accuracy;
      if (typeof speed === 'number' && speed >= 0) body.speed_mps = speed;

      postPing(serverUrl, bearerToken, body).catch((e: unknown) => {
        console.warn('postPing failed', e);
      });
    },
  );
}

export async function stopTracking(): Promise<void> {
  if (subscription) {
    subscription.remove();
    subscription = null;
  }
}
