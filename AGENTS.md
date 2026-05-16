# geo-beacon — agent notes

## What this is
Hackathon project: an autonomous AI mission commander for land search-and-rescue. openclaw on an NVIDIA DGX Spark ingests live searcher GPS, terrain data, and field findings; reasons about coverage and probability; dispatches teams via a mobile app each searcher carries. See `docs/superpowers/specs/2026-05-15-sar-mission-control-design.md` for the full design.

## Where things run
- **DGX Spark** — the only server. Hosts FastAPI, SQLite+SpatiaLite at `/data/mission.db`, workers in tmux, openclaw.
- **Laptops** — dev machines. SSH into DGX for deploy.
- **Phones (iPhones)** — each searcher carries one running the Expo/Swift app.

## Network reality
**Laptops and the DGX share a phone-hotspot SSID during demo and dev.** Phones reach the DGX via an **ngrok tunnel** fronting the FastAPI server — they do not need to be on the hotspot. Consequences:

- **GitHub Actions / cloud CI cannot reach the DGX directly.** Any deploy automation must run from a laptop already on the hotspot (or be a `git pull` initiated on the DGX itself).
- Deploy = `ssh dgx 'cd geo-beacon && git pull && ./scripts/respawn-workers.sh'` from a laptop on the hotspot.
- The app talks to the DGX through the ngrok public URL, not a LAN IP or domain name. App config needs the ngrok URL (via env / constants file) so it can be swapped when the tunnel restarts.
- ngrok URLs change every time the tunnel restarts. Don't bake a URL into anything; read from env.
- Cellular coverage in real SAR terrain is often zero. For the hackathon demo we pretend it works; for "real" deployment this app would need offline buffering on the phone (out of scope).

## Migrations
Every Python process calls `scripts/apply_migrations.py` at startup. New migration = drop a numbered SQL file into `migrations/`, commit, push, `git pull` on DGX, restart workers. Workers re-apply pending migrations automatically. No SSH-side migration commands to remember.

## Roles (team)
- **Eric** — database layer. Owns the SQLite/SpatiaLite schema, the migration runner, and the Python helpers that expose DB operations to FastAPI handlers and to the agent's skill layer. Makes sure all processes coordinate cleanly on the shared `.db` file.

## What changed from v1
The original plan (`Hack-a-Claw.md`) was a personal life-pattern brief for a single user. We pivoted to **land SAR mission control** because the personal-app version had a weak action surface and the SAR version makes the AI's role concrete and demo-able. The five-layer personal-life model is replaced by the mission/segments/teams/dispatches/findings model in the new spec.
