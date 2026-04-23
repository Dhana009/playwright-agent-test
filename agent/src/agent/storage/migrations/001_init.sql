PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL,
    ended_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step_id TEXT,
    actor TEXT NOT NULL,
    type TEXT NOT NULL,
    ts TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_events_run_step ON events (run_id, step_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (type);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    current_step_id TEXT NOT NULL,
    event_offset INTEGER NOT NULL,
    browser_session_id TEXT NOT NULL,
    tab_id TEXT NOT NULL,
    frame_path_json TEXT NOT NULL DEFAULT '[]',
    storage_state_ref TEXT,
    paused_recovery_state_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_run_created ON checkpoints (run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS step_graph (
    run_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    graph_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS compiled_memory (
    entry_id TEXT PRIMARY KEY,
    entry_type TEXT NOT NULL,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    version INTEGER NOT NULL,
    raw_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    confidence_score REAL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_compiled_memory_type_key ON compiled_memory (entry_type, key);

CREATE TABLE IF NOT EXISTS learned_repairs (
    repair_id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    state TEXT NOT NULL,
    domain TEXT NOT NULL,
    normalized_route_template TEXT NOT NULL,
    frame_context_json TEXT NOT NULL DEFAULT '[]',
    target_semantic_key TEXT,
    app_version TEXT,
    source_run_id TEXT NOT NULL,
    source_step_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    confidence_score REAL NOT NULL,
    validation_success_count INTEGER NOT NULL DEFAULT 0,
    validation_failure_count INTEGER NOT NULL DEFAULT 0,
    last_validated_at TEXT,
    expires_at TEXT,
    rollback_ref TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_learned_repairs_scope ON learned_repairs (scope_key);
CREATE INDEX IF NOT EXISTS idx_learned_repairs_state ON learned_repairs (state);

CREATE TABLE IF NOT EXISTS cache_records (
    cache_record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    route_template TEXT NOT NULL,
    dom_hash TEXT NOT NULL,
    frame_hash TEXT NOT NULL,
    modal_state TEXT NOT NULL,
    decision TEXT NOT NULL,
    decision_reasons_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_cache_records_run_step ON cache_records (run_id, step_id);

CREATE TABLE IF NOT EXISTS llm_calls (
    call_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step_id TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    call_purpose TEXT NOT NULL,
    context_tier TEXT NOT NULL,
    escalation_path_json TEXT NOT NULL DEFAULT '[]',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    preflight_input_tokens INTEGER NOT NULL DEFAULT 0,
    preflight_output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0,
    prompt_cache_hit INTEGER,
    est_cost REAL NOT NULL DEFAULT 0.0,
    actual_cost REAL NOT NULL DEFAULT 0.0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    no_progress_retry INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_run_step ON llm_calls (run_id, step_id);
