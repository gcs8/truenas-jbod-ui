
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
    persistent_id_label TEXT,
    disk_identity_key TEXT,
    logical_unit_id TEXT,
    sas_address TEXT,
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
    details_json TEXT NOT NULL,
    gptid TEXT,
    persistent_id_label TEXT,
    disk_identity_key TEXT,
    logical_unit_id TEXT,
    sas_address TEXT
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
    state TEXT,
    gptid TEXT,
    persistent_id_label TEXT,
    disk_identity_key TEXT,
    logical_unit_id TEXT,
    sas_address TEXT
);

CREATE TABLE IF NOT EXISTS metric_rollups (
    bucket_start TEXT NOT NULL,
    bucket_seconds INTEGER NOT NULL,
    system_id TEXT NOT NULL,
    system_label TEXT,
    enclosure_key TEXT NOT NULL,
    enclosure_id TEXT,
    enclosure_label TEXT,
    slot INTEGER NOT NULL,
    slot_label TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    value_sum REAL NOT NULL,
    value_min REAL NOT NULL,
    value_max REAL NOT NULL,
    last_value REAL NOT NULL,
    last_observed_at TEXT NOT NULL,
    device_name TEXT,
    serial TEXT,
    model TEXT,
    state TEXT,
    gptid TEXT,
    persistent_id_label TEXT,
    disk_identity_key TEXT NOT NULL DEFAULT '',
    logical_unit_id TEXT,
    sas_address TEXT,
    PRIMARY KEY (
        bucket_seconds,
        bucket_start,
        system_id,
        enclosure_key,
        slot,
        metric_name,
        disk_identity_key
    )
);

CREATE TABLE IF NOT EXISTS history_table_counts (
    table_name TEXT PRIMARY KEY,
    row_count INTEGER NOT NULL CHECK (row_count >= 0)
);

CREATE TABLE IF NOT EXISTS history_maintenance_state (
    name TEXT PRIMARY KEY,
    backup_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ready', 'claimed', 'consumed'))
);

CREATE TRIGGER IF NOT EXISTS count_slot_events_insert
AFTER INSERT ON slot_events BEGIN
    UPDATE history_table_counts
    SET row_count = row_count + 1
    WHERE table_name = 'slot_events';
END;

CREATE TRIGGER IF NOT EXISTS count_slot_events_delete
AFTER DELETE ON slot_events BEGIN
    UPDATE history_table_counts
    SET row_count = row_count - 1
    WHERE table_name = 'slot_events';
END;

CREATE TRIGGER IF NOT EXISTS count_metric_samples_insert
AFTER INSERT ON metric_samples BEGIN
    UPDATE history_table_counts
    SET row_count = row_count + 1
    WHERE table_name = 'metric_samples';
END;

CREATE TRIGGER IF NOT EXISTS count_metric_samples_delete
AFTER DELETE ON metric_samples BEGIN
    UPDATE history_table_counts
    SET row_count = row_count - 1
    WHERE table_name = 'metric_samples';
END;

CREATE TRIGGER IF NOT EXISTS count_metric_rollups_insert
AFTER INSERT ON metric_rollups BEGIN
    UPDATE history_table_counts
    SET row_count = row_count + 1
    WHERE table_name = 'metric_rollups';
END;

CREATE TRIGGER IF NOT EXISTS count_metric_rollups_delete
AFTER DELETE ON metric_rollups BEGIN
    UPDATE history_table_counts
    SET row_count = row_count - 1
    WHERE table_name = 'metric_rollups';
END;

CREATE INDEX IF NOT EXISTS idx_slot_events_scope
    ON slot_events (system_id, enclosure_key, slot, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_metric_samples_scope
    ON metric_samples (system_id, enclosure_key, slot, metric_name, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_slot_events_observed_at
    ON slot_events (observed_at, id);

CREATE INDEX IF NOT EXISTS idx_metric_samples_observed_at
    ON metric_samples (observed_at, id);

CREATE INDEX IF NOT EXISTS idx_metric_rollups_scope
    ON metric_rollups (
        system_id,
        enclosure_key,
        slot,
        metric_name,
        bucket_seconds,
        bucket_start DESC
    );

CREATE INDEX IF NOT EXISTS idx_metric_rollups_retention
    ON metric_rollups (bucket_seconds, bucket_start);
