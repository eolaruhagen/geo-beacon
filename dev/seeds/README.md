# dev/seeds

Drop `.sql` files here to pre-populate your local DB with fixtures. They're
applied in lexical order by `dev/seed.sh` right after migrations.

## Conventions

- **Numbered prefix** so order is explicit: `01_users.sql`, `02_mission.sql`, etc.
- **Idempotent inserts** — use `INSERT OR REPLACE`, `ON CONFLICT DO NOTHING`, or
  guard with `WHERE NOT EXISTS`. Seeds run every `reset-db.sh` and every direct
  `seed.sh`; they shouldn't fail on re-run.
- **One concern per file** — separate users from missions from segments. Easier
  to comment out a single file when debugging.
- **`mod_spatialite` is loaded for you** before seeds run, so spatial functions
  like `GeomFromText`, `AddGeometryColumn`, `MakePoint` are available.

## Example seed (placeholder — adapt to current schema)

```sql
-- dev/seeds/01_users.sql
INSERT OR REPLACE INTO users
  (id, display_name, callsign, role, status, bearer_token, created_ts)
VALUES
  (1, 'Eric (dev)',    'Alpha',   'searcher', 'standby', 'dev-token-alpha',   strftime('%s','now')),
  (2, 'Shreyan (dev)', 'Bravo',   'searcher', 'standby', 'dev-token-bravo',   strftime('%s','now')),
  (3, 'Demo Charlie',  'Charlie', 'searcher', 'standby', 'dev-token-charlie', strftime('%s','now'));
```

```sql
-- dev/seeds/02_mission.sql
INSERT OR REPLACE INTO missions
  (id, name, status, subject_description, pls_lat, pls_lon, pls_ts, started_ts)
VALUES
  (1, 'Dev Wilder Ranch', 'active',
   '12yo male hiker in red jacket, last seen 90 min ago',
   36.965, -122.085, strftime('%s','now','-90 minutes'),
   strftime('%s','now'));

UPDATE missions SET area_geom = GeomFromText(
  'POLYGON((-122.10 36.95, -122.05 36.95, -122.05 37.00, -122.10 37.00, -122.10 36.95))',
  4326
) WHERE id = 1;
```

These files are local-only fixtures — keep them committed if your team wants
shared demo data, or `.gitignore` them per developer if everyone seeds
differently.

## Don't put production data here

This is for local dev fixtures. Anything you commit here lands on every
developer's machine and (if the dev folder gets shared) potentially the DGX.
No real names, real phone numbers, or real bearer tokens.
