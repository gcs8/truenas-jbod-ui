
CREATE TABLE IF NOT EXISTS slot_state_current (
    system_id TEXT NOT NULL,
    system_label TEXT,
    enclosure_key TEXT NOT NULL,
    enclosure_id TEXT,
    enclosure_label TEXT,
    slot INTEGER NOT NULL,
    slot_label TEXT NOT NULL,
    present INTEGER NOT NULL,
    state TEXT,
    identify_active INTEGER NOT NULL,
    device_name TEXT,
    serial TEXT,
    model TEXT,
    gptid TEXT,
    pool_name TEXT,
    vdev_name TEXT,
    health TEXT,
    topology_label TEXT,
    multipath_device TEXT,
    multipath_mode TEXT,
    multipath_state TEXT,
    multipath_lunid TEXT,
    multipath_primary_path TEXT,
    multipath_alternate_path TEXT,
    multipath_active_paths TEXT,
    multipath_passive_paths TEXT,
    multipath_failed_paths TEXT,
    multipath_other_paths TEXT,
    multipath_active_controllers TEXT,
    multipath_passive_controllers TEXT,
    multipath_failed_controllers TEXT,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (system_id, enclosure_key, slot)
);

CREATE TABLE IF NOT EXISTS slot_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    system_id TEXT NOT NULL,
    system_label TEXT,
    enclosure_key TEXT NOT NULL,
    enclosure_id TEXT,
    enclosure_label TEXT,
    slot INTEGER NOT NULL,
    slot_label TEXT NOT NULL,
    event_type TEXT NOT NULL,
    previous_value TEXT,
    current_value TEXT,
    device_name TEXT,
    serial TEXT,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    system_id TEXT NOT NULL,
    system_label TEXT,
    enclosure_key TEXT NOT NULL,
    enclosure_id TEXT,
    enclosure_label TEXT,
    slot INTEGER NOT NULL,
    slot_label TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value_integer INTEGER,
    value_real REAL,
    device_name TEXT,
    serial TEXT,
    model TEXT,
    state TEXT
);

CREATE INDEX IF NOT EXISTS idx_slot_events_scope
    ON slot_events (system_id, enclosure_key, slot, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_metric_samples_scope
    ON metric_samples (system_id, enclosure_key, slot, metric_name, observed_at DESC);
