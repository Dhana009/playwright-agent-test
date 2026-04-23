PRAGMA foreign_keys = OFF;

DROP TABLE IF EXISTS llm_calls;
DROP TABLE IF EXISTS cache_records;
DROP TABLE IF EXISTS learned_repairs;
DROP TABLE IF EXISTS compiled_memory;
DROP TABLE IF EXISTS step_graph;
DROP TABLE IF EXISTS checkpoints;
DROP TABLE IF EXISTS events;
DROP TABLE IF EXISTS runs;

DELETE FROM schema_version WHERE version = 1;

PRAGMA foreign_keys = ON;
