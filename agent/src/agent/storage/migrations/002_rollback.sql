PRAGMA foreign_keys = OFF;

DROP TABLE IF EXISTS raw_evidence;
DELETE FROM schema_version WHERE version = 2;

PRAGMA foreign_keys = ON;
