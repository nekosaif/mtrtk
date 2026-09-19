-- mtrtk schema v2: what a running job is doing, and a usable index for the event filter.
-- Applied atomically by the runner in store/db.py together with its `PRAGMA user_version`
-- bump. Never write BEGIN/COMMIT in a migration.

-- The job runner reports a human-readable step ("converting hour 3 of 6") alongside `progress`,
-- so the UI can say what is happening rather than only how far along it is.
ALTER TABLE jobs ADD COLUMN message TEXT;

-- `GET /api/events?level=` filters on level and pages by id; `events_ts` cannot serve either,
-- so without this every filtered request scans the whole table.
CREATE INDEX IF NOT EXISTS events_level_id ON events(level, id);
