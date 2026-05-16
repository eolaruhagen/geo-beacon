# SAR Coordination App — Project Context

## What we're building
A search-and-rescue coordination app for a 24-hour NVIDIA hackathon at UC Santa Cruz. An autonomous AI agent (OpenClaw + NVIDIA Nemotron, running on a DGX Spark) orchestrates SAR volunteers in the field — telling each person where to search, tracking what's been covered, and coordinating coverage across the team. The agent makes the routing decisions; this iPhone app is the field interface for individual volunteers.

## The architecture
- **DGX Spark (on-site)**: Runs the OpenClaw agent + Nemotron model. The agent decides what each volunteer should do.
- **FastAPI server (on DGX, port 8000)**: Holds a command queue per volunteer. Exposes endpoints for the phone to GET pending commands and POST location updates.
- **ngrok**: Tunnels the FastAPI server to a public URL so phones can reach it.
- **iPhone app (this codebase)**: Streams the volunteer's GPS to the server, polls for new commands from the agent, and renders a map showing where to go, what's been searched, and conditions/notes.

Data flow for an agent → volunteer command:
OpenClaw decides action → calls a local tool → tool POSTs to FastAPI → FastAPI queues command → iPhone polls GET /commands → iPhone renders.

Data flow for location updates:
iPhone reads GPS → POSTs to FastAPI /location every N seconds → agent uses fresh positions for next routing decision.

## Current state of the iPhone app
- Built with **Expo SDK 53** (downgraded from 54 due to Xcode 16.1 / Swift 6 compatibility issues — do not upgrade SDK).
- Bundle identifier: `app.timeslicer.sarapp`
- Apple Developer team: Hotslicer Media LLC (882L5LPT7V)
- Running on a physical iPhone via `npx expo run:ios --device` development build.
- Dev loop: `npx expo start --dev-client` for JS hot reload. Only rebuild native when adding packages or changing permissions.
- TypeScript, Expo Router file-based routing.

## Native dependencies already installed (or about to be)
- `expo-location` — GPS streaming
- `react-native-maps` — Apple Maps rendering on iOS

`app.json` has location permissions set:
- `NSLocationWhenInUseUsageDescription`
- `NSLocationAlwaysAndWhenInUseUsageDescription`
- `UIBackgroundModes: ["location"]`

## What I need to build now
Core screens / features:
1. **Map screen** (primary view): MapView showing the volunteer's current location, assigned search area or waypoint from the agent, polygons/overlays for areas already searched, and any notes from the agent (terrain, conditions, elevation).
2. **Location streaming hook**: Uses `expo-location` with `watchPositionAsync` to push the volunteer's coordinates to the server every ~5 seconds. Must keep working when the screen is locked (background location).
3. **Command polling hook**: Polls `GET {NGROK_URL}/commands/{volunteerId}` every few seconds. When a command arrives, updates the map state (new waypoint, new search area, text alert).
4. **Volunteer identity**: Each phone needs a stable `volunteerId` — generate a UUID on first launch and store in `expo-secure-store` or `AsyncStorage`.
5. **Config**: The ngrok URL should be in one place (env or a constants file) so we can swap it when the tunnel restarts.

## What to ask me before coding
- The ngrok URL (I'll provide once the DGX-side FastAPI is running)
- The exact command JSON schema the agent will emit (TBD — we should propose one and align with the agent team)

## What to NOT do
- Don't suggest Expo Go — we're on a development build.
- Don't try to upgrade Expo SDK.
- Don't use WebSockets yet — agent team is building against polling. We can migrate later if there's time.
- Don't add a navigation library beyond Expo Router. One screen is fine to start.
- Keep it minimal. This needs to run in a demo in <24 hours, not ship to the App Store.
