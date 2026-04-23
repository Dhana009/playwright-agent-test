PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS raw_evidence (
    evidence_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step_id TEXT,
    actor TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    artifact_ref TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_raw_evidence_run_step ON raw_evidence (run_id, step_id);
CREATE INDEX IF NOT EXISTS idx_raw_evidence_type ON raw_evidence (evidence_type);
