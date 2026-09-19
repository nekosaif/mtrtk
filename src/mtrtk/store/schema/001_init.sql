-- mtrtk schema v1
-- Applied atomically: the runner in store/db.py wraps this file and its matching
-- `PRAGMA user_version` bump in one transaction, so a crash mid-file rolls back whole and
-- leaves the previous version. Never write BEGIN/COMMIT in a migration.
CREATE TABLE IF NOT EXISTS samples_1s (
    ts REAL PRIMARY KEY,            -- unix seconds (receiver UTC)
    lat REAL, lon REAL, height_m REAL, hmsl_m REAL,
    h_acc_m REAL, v_acc_m REAL,
    fix_type INTEGER, carr_soln INTEGER,
    nsat_used INTEGER, nsat_tracked INTEGER,
    pdop REAL, hdop REAL, vdop REAL,
    cno_mean REAL, jam_ind INTEGER, agc_cnt INTEGER, noise_per_ms INTEGER,
    corr_age_s REAL, baseline_m REAL,
    rtcm_bytes_per_s REAL, ntrip_clients INTEGER,
    cpu_pct REAL, mem_pct REAL, disk_free_gb REAL, temp_c REAL
);

CREATE TABLE IF NOT EXISTS samples_1m (
    ts REAL PRIMARY KEY,            -- minute start, unix seconds
    n INTEGER,
    lat_avg REAL, lon_avg REAL, height_avg REAL,
    h_acc_avg REAL, h_acc_max REAL, v_acc_avg REAL, v_acc_max REAL,
    fix_type_min INTEGER, carr_soln_min INTEGER,
    nsat_used_avg REAL, nsat_used_min INTEGER, nsat_tracked_avg REAL,
    pdop_avg REAL, pdop_max REAL,
    cno_mean_avg REAL, jam_ind_max INTEGER, agc_cnt_avg REAL, noise_per_ms_avg REAL,
    corr_age_max REAL, baseline_avg REAL,
    rtcm_bytes_avg REAL, ntrip_clients_max INTEGER,
    cpu_pct_avg REAL, mem_pct_avg REAL, disk_free_gb_min REAL, temp_c_max REAL
);

CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    x REAL NOT NULL, y REAL NOT NULL, z REAL NOT NULL,
    lat REAL, lon REAL, height_m REAL,
    sigma_x REAL, sigma_y REAL, sigma_z REAL,
    frame TEXT, epoch TEXT,
    source TEXT NOT NULL,
    notes TEXT,
    created_utc TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    start_utc TEXT NOT NULL,
    end_utc TEXT,
    role TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    code TEXT, note TEXT,
    ts_utc TEXT NOT NULL,
    lat REAL, lon REAL, height_m REAL, hmsl_m REAL,
    n_epochs INTEGER,
    sd_n REAL, sd_e REAL, sd_u REAL,
    fix_type INTEGER, carr_soln INTEGER,
    h_acc_m REAL, v_acc_m REAL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    level TEXT NOT NULL,            -- info | warning | error
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    meta TEXT,                      -- JSON
    acked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts_utc);

CREATE TABLE IF NOT EXISTS log_files (
    path TEXT PRIMARY KEY,
    hour_utc TEXT, start_utc TEXT, end_utc TEXT,
    bytes INTEGER,
    keep INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT,
    msg_counts TEXT,                -- JSON
    role TEXT, site TEXT,
    complete INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ntrip_clients_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT, mountpoint TEXT, user_agent TEXT, username TEXT,
    connected_utc TEXT NOT NULL,
    disconnected_utc TEXT,
    bytes_sent INTEGER NOT NULL DEFAULT 0,
    last_lat REAL, last_lon REAL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,             -- export | ppk
    status TEXT NOT NULL,           -- queued | running | done | failed
    created_utc TEXT NOT NULL,
    updated_utc TEXT,
    progress REAL NOT NULL DEFAULT 0,
    params TEXT, result TEXT, error TEXT
);
