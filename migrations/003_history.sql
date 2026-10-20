-- Review only. Use application migration for backup and integrity check.

CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v3_products(cabinet_id TEXT,nm_id TEXT,payload TEXT NOT NULL,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,PRIMARY KEY(cabinet_id,nm_id));
CREATE TABLE IF NOT EXISTS v3_events(cabinet_id TEXT,source TEXT,event_key TEXT,day TEXT,nm_id TEXT,payload TEXT NOT NULL,version_at TEXT NOT NULL,run_id TEXT NOT NULL,PRIMARY KEY(cabinet_id,source,event_key));
CREATE INDEX IF NOT EXISTS v3_events_period ON v3_events(cabinet_id,source,day,nm_id);
CREATE TABLE IF NOT EXISTS v3_coverage(cabinet_id TEXT,source TEXT,day TEXT,status TEXT NOT NULL,run_id TEXT NOT NULL,observed_at TEXT NOT NULL,PRIMARY KEY(cabinet_id,source,day));
CREATE TABLE IF NOT EXISTS v3_sync(cabinet_id TEXT,source TEXT,payload TEXT NOT NULL,PRIMARY KEY(cabinet_id,source));
CREATE TABLE IF NOT EXISTS v3_sources(run_id TEXT,cabinet_id TEXT,source TEXT,payload TEXT NOT NULL,PRIMARY KEY(run_id,cabinet_id,source));
CREATE TABLE IF NOT EXISTS v3_observations(cabinet_id TEXT,source TEXT,observation_key TEXT,snapshot_at TEXT,payload TEXT NOT NULL,PRIMARY KEY(cabinet_id,source,observation_key,snapshot_at));
CREATE TABLE IF NOT EXISTS v3_funnel(cabinet_id TEXT,nm_id TEXT,day TEXT,payload TEXT NOT NULL,run_id TEXT,observed_at TEXT,PRIMARY KEY(cabinet_id,nm_id,day));
CREATE TABLE IF NOT EXISTS v3_manual(kind TEXT,row_key TEXT,payload TEXT NOT NULL,imported_at TEXT NOT NULL,PRIMARY KEY(kind,row_key));
CREATE TABLE IF NOT EXISTS v3_evidence(evidence_id TEXT PRIMARY KEY,payload TEXT NOT NULL,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v3_actions(action_id TEXT PRIMARY KEY,evidence_id TEXT,status TEXT NOT NULL,owner TEXT,due_date TEXT,completed_at TEXT,payload TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v3_action_history(id INTEGER PRIMARY KEY AUTOINCREMENT,action_id TEXT,changed_at TEXT,payload TEXT);
CREATE TABLE IF NOT EXISTS v3_preferences(user_id INTEGER PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v3_reports(run_id TEXT PRIMARY KEY,report_date TEXT,revision INTEGER,created_at TEXT,payload TEXT,UNIQUE(report_date,revision));
CREATE TABLE IF NOT EXISTS v3_deliveries(delivery_key TEXT PRIMARY KEY,status TEXT NOT NULL,created_at TEXT,updated_at TEXT,message_id TEXT);
CREATE TABLE IF NOT EXISTS v3_leases(name TEXT PRIMARY KEY,owner TEXT NOT NULL,expires_at TEXT NOT NULL);
