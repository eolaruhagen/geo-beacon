-- Adds users.is_observer for the demo flow.
--
-- Observer = a user whose phone is in "demo mode": it reads mission state
-- but does NOT generate its own GPS pings. The simulator (sim_searcher.py)
-- writes pings on their behalf. POST /field/ping silently no-ops for
-- observer users so the phone can stay logged in as them without
-- interfering with simulated movement.
--
-- Set via /debug/restore (sets observer=1 on the lone kept user after
-- snapshot restore). Stays at 0 for normal joins.

ALTER TABLE users ADD COLUMN is_observer INTEGER NOT NULL DEFAULT 0;
